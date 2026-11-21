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
# (a test holds it to that). Everything running is stopped first, since the running
# satellite holds numpy's and the sound libraries' files open and pip can't replace them,
# nor can Nova.exe be replaced while it runs, and stopping the satellite alone isn't
# enough, as the launcher starts it again within seconds. So both tasks are stopped
# (either may not be there yet), then the launcher and the satellite: Nova.exe processes
# run from the install's venv\Scripts, and, for an install from before Nova.exe, the
# venv's pythonw running app\launcher.pyw or app\satellite.py, found by its command line.
# Any other Python on the PC is left alone. Whether any of them was running, and whether
# app\satellite.py was there yet, is noted before anything is stopped. Then it installs,
# and starts the task "Nova start" (never the logon task, whose start the launcher takes
# for a logon) as above. Windows has no SIGTERM, so ending the satellite is a hard stop,
# which the launcher's next start forgives.
script=$(cat <<'POWERSHELL'
$ErrorActionPreference = 'Stop'
$base = Join-Path $env:LOCALAPPDATA 'nova'
$app = Join-Path $base 'app'
$scripts = (Join-Path $app 'venv\Scripts') + '\'
function Find-Running {
    Get-CimInstance Win32_Process -Filter "Name = 'Nova.exe'" | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($scripts, [StringComparison]::OrdinalIgnoreCase) }
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" | Where-Object {
        $_.CommandLine -and ($_.CommandLine.IndexOf((Join-Path $app 'satellite.py'), [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            $_.CommandLine.IndexOf((Join-Path $app 'scripts\windows\launcher.pyw'), [StringComparison]::OrdinalIgnoreCase) -ge 0) }
}
$firstInstall = -not (Test-Path (Join-Path $app 'satellite.py'))
$wasRunning = @(Find-Running).Count -gt 0
foreach ($task in @('Nova satellite', 'Nova start')) {
    try { Stop-ScheduledTask -TaskName $task -ErrorAction Stop } catch { }
}
for ($try = 0; $try -lt 20 -and @(Find-Running).Count -gt 0; $try++) {
    foreach ($process in @(Find-Running)) {
        Write-Host "Ending process $($process.ProcessId)"
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
}
if (@(Find-Running).Count -gt 0) { throw 'Nova is still running, so the install would fail partway' }
$temp = Join-Path $env:LOCALAPPDATA 'Temp'
$zip = Join-Path $temp 'nova-deploy.zip'
$src = Join-Path $temp 'nova-deploy'
if (Test-Path $src) { Remove-Item $src -Recurse -Force }
Expand-Archive -LiteralPath $zip -DestinationPath $src
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
