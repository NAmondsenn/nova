#!/bin/bash
# Installs, or updates, Nova's satellite on the Windows PC, from CT 109 over `ssh pc`.
#
#   bash scripts/deploy_pc.sh
#
# What it sends is the commit the brain is running (CT 107's checkout), never work in
# progress here, so the protocol always matches the brain's. It reads that commit,
# refuses unless this repository has it, makes an archive of it, copies the archive to
# the PC. There it notes whether Nova is running (as Nova.exe, or as the venv's pythonw
# before there was Nova.exe) and whether this is a first install, stops both tasks
# ("Nova satellite" and "Nova start"), the launcher and the satellite, and runs
# scripts/windows/install.ps1. Then it starts the task "Nova start" with the new code
# only if Nova was running or this is a first install, and there is a password for it
# to use: Nova which was quit stays quit, with the new code ready for its next start. The
# PC's password for the brain (config\.env) is never touched, and nothing is sent to
# the PC but the code. See docs/decisions/0026.
#
# Each run is the user's own named job: it replaces the code on the PC and restarts
# the satellite there if it was running.

set -euo pipefail

repo=$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)

commit=$(ssh nova 'sudo -n -u nova git -C /home/nova/Home-Assistant rev-parse HEAD' | tr -d '\r\n')
if ! [[ "$commit" =~ ^[0-9a-f]{40}$ ]]; then
    echo "The brain's commit wasn't a commit id: '${commit}'" >&2
    exit 1
fi
if ! git -C "$repo" cat-file -e "${commit}^{commit}" 2>/dev/null; then
    echo "The brain runs ${commit}, which this repository doesn't have: fetch it first (it is not here, so it isn't sent)." >&2
    exit 1
fi
echo "Deploying the brain's commit ${commit:0:10} to the PC"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git -C "$repo" archive --format=zip -o "$work/nova.zip" "$commit"

# Relative to the user's home on the PC, where ssh and scp log in.
scp -q "$work/nova.zip" pc:AppData/Local/Temp/nova-deploy.zip

# Run on the PC. Kept short, with its comments here rather than in it: the whole of it
# goes on ssh's command line, encoded, which cmd.exe there limits to 8191 characters
# (a test holds it to that). The archive is expanded first, and the stop logic comes from
# it: scripts\windows\stop_nova.ps1, which the update helper uses too, stops both tasks
# and then the launcher and the satellite (see that file for why, and for the old pythonw
# Nova). Whether any of them was running, and whether app\satellite.py was there yet, is
# noted before anything is stopped. Then it installs (which registers the task "Nova update"
# the satellite's updates run from), and starts the task "Nova start" (never the logon task,
# whose start the launcher takes for a logon) as above.
script=$(cat <<'POWERSHELL'
$ErrorActionPreference = 'Stop'
$temp = Join-Path $env:LOCALAPPDATA 'Temp'
$zip = Join-Path $temp 'nova-deploy.zip'
$src = Join-Path $temp 'nova-deploy'
if (Test-Path $src) { Remove-Item $src -Recurse -Force }
Expand-Archive -LiteralPath $zip -DestinationPath $src
. (Join-Path $src 'scripts\windows\stop_nova.ps1')
$firstInstall = -not (Test-Path (Join-Path $app 'satellite.py'))
$wasRunning = @(Find-Running).Count -gt 0
Stop-Nova
& (Join-Path $src 'scripts\windows\install.ps1') -Source $src
Set-Content -LiteralPath (Join-Path $app 'COMMIT') -Value $COMMIT -Encoding ASCII
if (-not ($wasRunning -or $firstInstall)) {
    Write-Host "Nova wasn't running, so it is left stopped"
}
elseif (Test-Path (Join-Path $base 'config\.env')) {
    Write-Host 'Starting Nova'
    Start-ScheduledTask -TaskName 'Nova start'
}
else {
    Write-Host 'No config\.env yet, so Nova is not started: add NOVA_BACKEND_TOKEN, then start the task Nova start'
}
Remove-Item $zip -Force
Remove-Item $src -Recurse -Force
Write-Host 'Done'
POWERSHELL
)
script="\$COMMIT = '${commit}'"$'\n'"${script}"

# The user's convention for PowerShell over ssh: encoded, so no quoting survives to
# go wrong. The execution policy is bypassed for this process only, so install.ps1,
# which is a file, can run.
encoded=$(printf '%s\n' "$script" | iconv -f utf-8 -t utf-16le | base64 -w0)
ssh pc "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ${encoded}"

echo "Deployed ${commit:0:10}."
