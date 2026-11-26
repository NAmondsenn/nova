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

# Writes result.json for the brain, and failed.json the same way voice_assistant/
# update_state.py keeps a failure, so the brain's next offer of this version is known to
# have failed rather than tried again for ever. The texts are arguments to python, never
# put in its code, so no quote in one can change what runs. Arguments: the error, then
# optionally a why and a command for something he must do. Each file is written to a
# temporary name and moved into place, so neither is ever seen half written.
write_failure() {
    python3 -I -c '
import json, os, sys
version, folder, error = sys.argv[1:4]
result = {"version": version, "ok": False, "error": error[:300]}
if len(sys.argv) > 5:
    result["needs_you"] = [{"why": sys.argv[4][:200], "command": sys.argv[5][:200]}]
for name in ("failed.json", "result.json"):
    tmp = os.path.join(folder, name + ".tmp")
    with open(tmp, "w") as file:
        json.dump(result, file)
    os.replace(tmp, os.path.join(folder, name))
' "$version" "$folder" "$@"
}

# Ends a run that changed nothing: says why, and restarts voice-assistant, which waited
# to be free before this began, so the old code reconnects and sends the result at once.
refuse() {
    write_failure "$@"
    systemctl --user restart "$SERVICE_NAME"
    exit 1
}

# Ends a run which moved the code and can't keep it. Moves the code back to where it
# was, if it is still on the new version, and starts it, as Nova may be stopped - a
# running satellite beats a stopped one, so it is started even if the code couldn't be
# moved back. The result is written before the restart, so it is there when the old code
# connects. Arguments: what to say if the code went back, what to say if there was
# nothing to go back to.
fail_back() {
    # Ignored from here, so a second signal can't cut the take-back short.
    trap '' HUP INT TERM
    local error="$2"
    if [ "$previous" != "$version" ]; then
        error="$1"
        if [ "$(git -C "$project" rev-parse HEAD)" = "$version" ] \
            && ! git -C "$project" reset -q --keep "$previous"; then
            write_failure "The new code couldn't be taken back." "The new code couldn't be taken back." \
                "cd ${project} && git reset --keep ${previous}"
            systemctl --user restart "$SERVICE_NAME"
            exit 1
        fi
    fi
    write_failure "$error"
    systemctl --user restart "$SERVICE_NAME"
    exit 1
}

if [ -n "$(git -C "$project" status --porcelain --untracked-files=no)" ]; then
    why="The Pi's code has local changes, so it wasn't updated."
    refuse "$why" "$why" "cd ${project} && git status"
fi

if ! git -C "$project" fetch -q "$bundle" HEAD \
    || [ "$(git -C "$project" rev-parse FETCH_HEAD)" != "$version" ]; then
    refuse "The update's code isn't the commit it said."
fi

previous=$(git -C "$project" rev-parse HEAD)
# From the moment the code moves until the new code is confirmed, being stopped (the
# unit stopped, a shutdown) takes the code back rather than leaving it unconfirmed.
trap 'fail_back "The update was stopped part way, so it was taken back." "The update was stopped part way."' HUP INT TERM
if [ "$previous" != "$version" ]; then
    if git -C "$project" merge-base --is-ancestor "$previous" "$version"; then
        git -C "$project" merge -q --ff-only "$version" || refuse "The update couldn't be applied to the Pi's code."
    elif git -C "$project" merge-base --is-ancestor "$version" "$previous"; then
        # The Pi is ahead of the brain, and no machine is ever ahead of it.
        git -C "$project" reset -q --keep "$version" || refuse "The update couldn't be applied to the Pi's code."
    else
        why="The Pi's code has gone its own way, so it wasn't updated."
        refuse "$why" "$why" "cd ${project} && git log --oneline -3"
    fi
fi

# An old one - from an update before this, which holds this very version, say - would
# be taken for the new code's own say-so.
rm -f -- "${folder}/confirmed"
# From /dev/null, so a step that wants a password gets none rather than waiting for one
# nobody can type, and fails - which update.sh takes the code back from itself.
output=$(NOVA_UPDATED_FROM="$previous" NOVA_UPDATE_NOTES="${folder}/notes" \
    bash "${project}/update.sh" --no-pull 2>&1 < /dev/null)
status=$?
if [ "$status" != 0 ]; then
    last=$(printf '%s\n' "$output" | grep -v '^[[:space:]]*$' | tail -n 1)
    # update.sh takes the code back by itself when a step fails; fail_back only moves it
    # if that didn't get as far.
    fail_back "The update failed and was taken back: ${last:0:200}" "The update failed: ${last:0:200}"
fi

confirmed() { [ "$(cat "${folder}/confirmed" 2> /dev/null)" = "$version" ]; }
waited=0
while [ "$waited" -lt "$CONFIRM_WAIT" ]; do
    if confirmed; then
        trap - HUP INT TERM
        exit 0
    fi
    sleep 1
    waited=$((waited + 1))
done
if confirmed; then
    trap - HUP INT TERM
    exit 0
fi

fail_back "The new code didn't reconnect to the brain within 3 minutes, so it was taken back." \
    "The new code didn't reconnect to the brain within 3 minutes."
