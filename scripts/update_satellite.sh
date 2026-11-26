#!/bin/bash
# Installs the update its brain sent, on a satellite, and takes it back if it doesn't
# reconnect. Run detached by voice_assistant/update_install.py (systemd-run --user), so
# it carries on while voice-assistant is stopped and restarted underneath it:
#
#   bash update_satellite.sh <project> <version> <bundle> <folder>
#
# project is the clone Nova runs from, version the commit the brain's bundle should hold,
# bundle the git bundle file, folder the update folder (result.json, notes and confirmed
# are kept there; see voice_assistant/update_state.py). Nothing is written to result.json
# when it goes well - the new code writes that itself once it has reconnected.
#
# Every machine ends on the brain's commit: a clone behind is moved forward, one ahead is
# moved back, one that has gone its own way is left alone and says what to run.

set -uo pipefail

project="${1:?project}"
version="${2:?version}"
bundle="${3:?bundle}"
folder="${4:?folder}"
CONFIRM_WAIT="${NOVA_CONFIRM_WAIT:-180}"
SERVICE_NAME="voice-assistant"

# The bundle is the brain's to send once, so it goes whatever happens.
trap 'rm -f -- "$bundle"' EXIT

# Writes result.json for the brain. The texts are arguments to python, never put in its
# code, so no quote in one can change what runs. Arguments: error, then optionally a why
# and a command for something he must do.
write_failure() {
    python3 -I -c '
import json, sys
version, error, folder = sys.argv[1:4]
result = {"version": version, "ok": False, "error": error[:300]}
if len(sys.argv) > 5:
    result["needs_you"] = [{"why": sys.argv[4][:200], "command": sys.argv[5][:200]}]
tmp = folder + "/result.json.tmp"
with open(tmp, "w") as file:
    json.dump(result, file)
import os
os.replace(tmp, folder + "/result.json")
' "$version" "$1" "$folder" "${@:2}"
}

needs_you() {
    write_failure "$1" "$1" "$2"
    exit 1
}

# Takes the code back to where it was, and starts that code, as Nova may be stopped.
take_back() {
    git -C "$project" reset -q --keep "$previous"
    systemctl --user restart "$SERVICE_NAME"
}

if [ -n "$(git -C "$project" status --porcelain --untracked-files=no)" ]; then
    needs_you "The Pi's code has local changes, so it wasn't updated." "cd ${project} && git status"
fi

if ! git -C "$project" fetch -q "$bundle" HEAD \
    || [ "$(git -C "$project" rev-parse FETCH_HEAD)" != "$version" ]; then
    write_failure "The update's code isn't the commit it said."
    exit 1
fi

previous=$(git -C "$project" rev-parse HEAD)
if [ "$previous" != "$version" ]; then
    if git -C "$project" merge-base --is-ancestor "$previous" "$version"; then
        git -C "$project" merge -q --ff-only "$version" || { write_failure "The update couldn't be applied to the Pi's code."; exit 1; }
    elif git -C "$project" merge-base --is-ancestor "$version" "$previous"; then
        # The Pi is ahead of the brain, and no machine is ever ahead of it.
        git -C "$project" reset -q --keep "$version" || { write_failure "The update couldn't be applied to the Pi's code."; exit 1; }
    else
        needs_you "The Pi's code has gone its own way, so it wasn't updated." "cd ${project} && git log --oneline -3"
    fi
fi

# From /dev/null, so a step that wants a password gets none rather than waiting for one
# nobody can type, and fails - which update.sh takes the code back from itself.
output=$(NOVA_UPDATED_FROM="$previous" NOVA_UPDATE_NOTES="${folder}/notes" \
    bash "${project}/update.sh" --no-pull 2>&1 < /dev/null)
status=$?
if [ "$status" != 0 ]; then
    last=$(printf '%s\n' "$output" | grep -v '^[[:space:]]*$' | tail -n 1)
    # update.sh takes the code back by itself when a step fails; this only does so if it
    # didn't get that far, and then the old code has to be started again.
    if [ "$(git -C "$project" rev-parse HEAD)" = "$version" ] && [ "$previous" != "$version" ]; then
        take_back
    else
        systemctl --user restart "$SERVICE_NAME"
    fi
    write_failure "The update failed and was taken back: ${last:0:200}"
    exit 1
fi

waited=0
while [ "$waited" -lt "$CONFIRM_WAIT" ]; do
    if [ "$(cat "${folder}/confirmed" 2> /dev/null)" = "$version" ]; then
        exit 0
    fi
    sleep 1
    waited=$((waited + 1))
done

take_back
write_failure "The new code didn't reconnect to the brain within 3 minutes, so it was taken back."
exit 1
