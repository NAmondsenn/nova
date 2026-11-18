#!/bin/bash
# Installs, or updates, Nova's satellite on the Windows PC, from CT 109 over `ssh pc`.
#
#   bash scripts/deploy_pc.sh
#
# What it sends is the commit the brain is running (CT 107's checkout), never work in
# progress here, so the protocol always matches the brain's. It reads that commit,
# refuses unless this repository has it, makes an archive of it, copies the archive to
# the PC. There it stops the task, the launcher and the satellite, runs
# scripts/windows/install.ps1, and starts the task again with the new code. The
# PC's password for the brain (config\.env) is never touched, and nothing is sent to
# the PC but the code. See docs/decisions/0024.
#
# Each run is the user's own named job: it replaces the code on the PC and restarts
# the satellite there.

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

# Run on the PC. Kept short: the whole of it goes on ssh's command line, encoded.
# Everything running is stopped first, since the running satellite holds numpy's and
# the sound libraries' files open and pip can't replace them, and stopping the
# satellite alone isn't enough, as the launcher starts it again within seconds. So the
# task is stopped, then the launcher and the satellite, found by their command lines
# (the venv's pythonw running app\launcher.pyw or app\satellite.py), so any other
# Python on the PC is left alone. Then it installs, and starts the task again once
# there is a password for it to use. Windows has no SIGTERM, so ending the satellite
# is a hard stop, which its next start counts as unclean.
script=$(cat <<'POWERSHELL'
$ErrorActionPreference = 'Stop'
$base = Join-Path $env:LOCALAPPDATA 'nova'
$app = Join-Path $base 'app'
function Find-Running {
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" | Where-Object {
        $_.CommandLine -and ($_.CommandLine.IndexOf((Join-Path $app 'satellite.py'), [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            $_.CommandLine.IndexOf((Join-Path $app 'scripts\windows\launcher.pyw'), [StringComparison]::OrdinalIgnoreCase) -ge 0) }
}
# No task yet, on a first install, is fine.
try { Stop-ScheduledTask -TaskName 'Nova satellite' -ErrorAction Stop } catch { }
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
if (Test-Path (Join-Path $base 'config\.env')) {
    Write-Host 'Starting the launcher'
    Start-ScheduledTask -TaskName 'Nova satellite'
}
else {
    Write-Host 'No config\.env yet, so the launcher is not started: add NOVA_BACKEND_TOKEN, then start the task Nova satellite'
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
