#!/bin/bash
# Updates the brain's code when the brain asks, and says how it went. Run by
# nova-update.service (started by nova-update.path when the brain drops update/request in
# its own data folder), as the clone's owner, from the clone:
#
#   bash scripts/update_brain.sh
#
# It fast-forwards the clone to GitHub's main, runs update.sh --no-pull --no-restart (the
# packages, models and settings check, with the take-back when a step fails), and writes
# update/brain.json for the brain to read once it is back up: {state, from, to, error?,
# needs_you?: [{why, command}], at}. State is "checking", then "current", "needs_you",
# "failed" or "ready". It never restarts the brain - the brain restarts itself, by
# exiting, once it sees "ready" - and it leaves update/previous holding the commit it
# moved from, which nova-takeback.service moves the code back to if the new code won't
# start. The clone is never left moved without "ready" (and previous) or a take-back.

set -uo pipefail

data="${NOVA_DATA:-${HOME}/.local/share/nova}"
folder="${data}/update"
# First of all, before anything that can fail, so a failure can't leave the request for
# the path unit to start this again for ever - and a second request made while this runs
# starts the next run.
rm -f -- "${folder}/request"
project="$(git rev-parse --show-toplevel)" || exit 1
python="${project}/venv/bin/python"
[ -x "$python" ] || python="python3"

mkdir -p -m 700 "$folder"

from=$(git -C "$project" rev-parse HEAD) || exit 1
to=""

# Writes brain.json atomically (a temporary file moved into place), so the brain never
# reads it half written. The texts are arguments to python, never put in its code, so no
# quote in one can change what runs. Arguments: the state, then optionally an error, then
# any number of a why and a command for something he must do.
write_status() {
    "$python" -I -c '
import datetime, json, os, sys
folder, state, source, target, error = sys.argv[1:6]
result = {"state": state, "from": source, "to": target,
          "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
if error:
    result["error"] = error[:300]
rest = sys.argv[6:]
if rest:
    result["needs_you"] = [{"why": why[:200], "command": command[:300]}
                           for why, command in zip(rest[0::2], rest[1::2])]
path = os.path.join(folder, "brain.json")
with open(path + ".tmp", "w") as file:
    json.dump(result, file)
os.replace(path + ".tmp", path)
' "$folder" "$1" "$from" "$to" "${2:-}" "${@:3}"
}

# Writes update/previous the same way, holding only the commit.
write_previous() {
    "$python" -I -c '
import os, sys
path = os.path.join(sys.argv[1], "previous")
with open(path + ".tmp", "w") as file:
    file.write(sys.argv[2] + "\n")
os.replace(path + ".tmp", path)
' "$folder" "$from"
}

# Ends a run that changed nothing, because something is for him to put right.
refuse() {
    write_status needs_you "$1" "$1" "$2"
    exit 0
}

write_status checking

if ! git -C "$project" fetch -q origin refs/heads/main; then
    write_status failed "Couldn't fetch the update from GitHub."
    exit 1
fi
to=$(git -C "$project" rev-parse FETCH_HEAD)

if [ "$to" = "$from" ]; then
    write_status current
    exit 0
fi

branch=$(git -C "$project" rev-parse --abbrev-ref HEAD)
if [ "$branch" != "main" ]; then
    refuse "The brain's code is on ${branch}, not main." "cd ${project} && git checkout main"
fi
if [ -n "$(git -C "$project" status --porcelain --untracked-files=no)" ]; then
    refuse "The brain's code has local changes, so it wasn't updated." "cd ${project} && git status"
fi
if ! git -C "$project" merge-base --is-ancestor "$from" "$to"; then
    refuse "The brain's code has gone its own way, so it wasn't updated." "cd ${project} && git log --oneline -3"
fi

# Puts the code back at where it was, if it has moved, and says so. Keeps local changes
# rather than throwing them away, so a refusal is his to look at. Arguments: what to
# say if it went back.
take_back() {
    # Ignored from here, so a second signal can't cut the reset short.
    trap '' HUP INT TERM
    if [ "$(git -C "$project" rev-parse HEAD)" != "$from" ] \
        && ! git -C "$project" reset -q --keep "$from"; then
        write_status needs_you "The new code couldn't be taken back." "The new code couldn't be taken back." \
            "cd ${project} && git reset --keep ${from}"
        exit 1
    fi
    rm -f -- "${folder}/previous"
    write_status failed "$1"
    exit 1
}

# From the moment the code moves until "ready" is written, being stopped (the unit
# stopped, a shutdown) takes the code back rather than leaving it moved.
trap 'take_back "The update was stopped part way, so it was taken back."' HUP INT TERM
if ! git -C "$project" merge -q --ff-only FETCH_HEAD; then
    take_back "The update couldn't be applied to the brain's code."
fi

# From /dev/null, so a step that wants a password gets none rather than waiting for one.
# update.sh takes the code back by itself when a step fails.
output=$(NOVA_UPDATED_FROM="$from" bash "${project}/update.sh" --no-pull --no-restart 2>&1 < /dev/null)
status=$?
printf '%s\n' "$output"
if [ "$status" != 0 ]; then
    last=$(printf '%s\n' "$output" | grep -v '^[[:space:]]*$' | tail -n 1)
    take_back "The update failed: ${last:0:250}"
fi

write_previous || take_back "The update couldn't note where to go back to."

# A commit which changes what only root can install still restarts into the new code, and
# says what to run: a unit template, or the system packages setup.sh installs.
needs_units=false
if [ -n "$(git -C "$project" diff --name-only "${from}..HEAD" -- nova-backend.service nova-update.service \
    nova-update.path nova-takeback.service)" ]; then
    needs_units=true
fi
packages() {
    git -C "$project" show "${1}:setup.sh" 2> /dev/null | awk '/^install_system_packages\(\) \{/,/^\}/'
}
needs_packages=false
if [ "$(packages "$from")" != "$(packages HEAD)" ]; then
    needs_packages=true
fi

# update.sh never installs system packages, so they are setup.sh's; the units are
# update.sh's. Ready is written before the trap goes, so a signal in between takes the
# code back rather than leaving it moved with no word said.
notes=()
if [ "$needs_packages" = true ]; then
    notes+=("The update needs system packages installed, which only root can do." "sudo bash ${project}/setup.sh")
fi
if [ "$needs_units" = true ]; then
    notes+=("The update changes the system's own service files, which only root can install." \
        "sudo bash ${project}/update.sh --no-pull")
fi
write_status ready "" "${notes[@]}"
trap - HUP INT TERM
