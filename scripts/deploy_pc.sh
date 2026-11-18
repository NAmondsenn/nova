#!/bin/bash
# Installs, or updates, Nova's satellite on the Windows PC, from CT 109 over `ssh pc`.
#
#   bash scripts/deploy_pc.sh
#
# What it sends is the commit the brain is running (CT 107's checkout), never work in
# progress here, so the protocol always matches the brain's. It reads that commit,
# refuses unless this repository has it, makes an archive of it, copies the archive to
# the PC and runs scripts/windows/install.ps1 there. Then it ends the running
# satellite, which the launcher starts again within seconds with the new code. The
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
# The satellite is found by its command line (the venv's pythonw running
# app\satellite.py), so the launcher, and any other Python on the PC, are left alone.
# Windows has no SIGTERM, so ending it is a hard stop, which the satellite's next start
# counts as unclean. The launcher is started, through its task, only if it isn't
# running, and only once there is a password for it to use.
script=$(cat <<'POWERSHELL'
$ErrorActionPreference = 'Stop'
$temp = Join-Path $env:LOCALAPPDATA 'Temp'
$zip = Join-Path $temp 'nova-deploy.zip'
$src = Join-Path $temp 'nova-deploy'
if (Test-Path $src) { Remove-Item $src -Recurse -Force }
Expand-Archive -LiteralPath $zip -DestinationPath $src
& (Join-Path $src 'scripts\windows\install.ps1') -Source $src
$base = Join-Path $env:LOCALAPPDATA 'nova'
$app = Join-Path $base 'app'
Set-Content -LiteralPath (Join-Path $app 'COMMIT') -Value $COMMIT -Encoding ASCII
function Find-Running($name) {
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" | Where-Object {
        $_.CommandLine -and $_.CommandLine.IndexOf((Join-Path $app $name), [StringComparison]::OrdinalIgnoreCase) -ge 0 }
}
foreach ($process in @(Find-Running 'satellite.py')) {
    Write-Host "Ending the satellite (process $($process.ProcessId))"
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
if (-not (Test-Path (Join-Path $base 'config\.env'))) {
    Write-Host 'No config\.env yet, so the launcher is not started: add NOVA_BACKEND_TOKEN, then start the task Nova satellite'
}
elseif (-not (Find-Running 'scripts\windows\launcher.pyw')) {
    Write-Host 'Starting the launcher'
    Start-ScheduledTask -TaskName 'Nova satellite'
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

echo "Deployed ${commit:0:10}. The launcher starts the new satellite within seconds."
