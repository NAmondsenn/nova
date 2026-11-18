# Installs Nova's satellite on this Windows PC, for the user running it, under
# %LOCALAPPDATA%\nova: app (the code, and its venv), config and data. Run it over ssh
# by scripts/deploy_pc.sh, or by hand from the folder the code is in:
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 -Source <folder>
#
# It's safe to run again, which is how an update goes: the code is copied over, the
# packages are installed again only if requirements-pc.txt changed, the settings file
# is left alone if there is one, and the logon task is replaced. It never writes .env:
# the password the brain's `add-satellite pc` gave goes in config\.env by hand, as
# NOVA_BACKEND_TOKEN=<password>. It doesn't start or stop the satellite either.
#
# Plain ASCII, and nothing newer than Windows PowerShell 5.1. The `python` on this PC's
# PATH is the Microsoft Store stub, so only the `py` launcher is used.

param(
    # The folder holding the code to install: the root of a checkout, or of what
    # `git archive` made.
    [Parameter(Mandatory = $true)]
    [string]$Source
)

$ErrorActionPreference = 'Stop'

$base = Join-Path $env:LOCALAPPDATA 'nova'
$app = Join-Path $base 'app'
$config = Join-Path $base 'config'
$data = Join-Path $base 'data'
$venv = Join-Path $app 'venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$venvPythonw = Join-Path $venv 'Scripts\pythonw.exe'
$launcher = Join-Path $app 'scripts\windows\launcher.pyw'
$taskName = 'Nova satellite'

if (-not (Test-Path (Join-Path $Source 'satellite.py'))) {
    throw "$Source has no satellite.py, so it isn't Nova's code"
}

foreach ($folder in @($app, $config, $data)) {
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
}

# The code. /MIR drops files which are gone from the source, but not the venv, which
# isn't in it. Exit codes below 8 are robocopy's ways of saying it worked.
Write-Host "Copying the code to $app"
robocopy $Source $app /MIR /XD $venv /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed (exit code $LASTEXITCODE)" }

# The environment, made once.
if (-not (Test-Path $venvPython)) {
    Write-Host 'Making the Python environment'
    & py -3 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "py -3 -m venv failed (exit code $LASTEXITCODE)" }
}

# The packages, only when the list changed since they were last installed. The mark
# is kept in the venv, so a new venv has none.
$requirements = Join-Path $app 'requirements-pc.txt'
$mark = Join-Path $venv 'requirements-pc.sha256'
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $requirements).Hash
$installed = ''
if (Test-Path $mark) { $installed = (Get-Content -LiteralPath $mark -Raw).Trim() }
if ($hash -ne $installed) {
    Write-Host 'Installing the packages'
    & $venvPython -m pip install --disable-pip-version-check --no-deps -r $requirements
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit code $LASTEXITCODE)" }
    Set-Content -LiteralPath $mark -Value $hash -Encoding ASCII
}
else {
    Write-Host 'The packages are as requirements-pc.txt says'
}

# This install's own settings, only if there are none: what differs from config.yaml,
# which is the brain's address and name for this satellite, a key press instead of a
# wake word, and the echo canceller off (its alignment across the speaker stream's
# gaps, which close when nothing is playing, is untested).
$settings = Join-Path $config 'config.yaml'
if (-not (Test-Path $settings)) {
    Write-Host "Writing $settings"
    $text = @'
backend:
  address: "nova:10400"
  satellite: "pc"
satellite:
  trigger: "push_to_talk"
audio:
  echo_cancellation: false
'@
    [System.IO.File]::WriteAllText($settings, $text + "`n", (New-Object System.Text.UTF8Encoding($false)))
}
else {
    Write-Host "Leaving $settings as it is"
}

# Started at his logon, in his own session, so it has his microphone and speakers;
# the launcher runs the satellite and starts it again whenever it exits. One copy
# only, and no time limit.
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $venvPythonw -Argument ('"' + $launcher + '"') -WorkingDirectory $app
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Write-Host "Registering the task '$taskName' for $user"
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $taskSettings -Description "Nova's satellite, started at logon" -Force | Out-Null

if (-not (Test-Path (Join-Path $config '.env'))) {
    Write-Host "No password yet: put NOVA_BACKEND_TOKEN=<the password add-satellite pc gave> in $(Join-Path $config '.env')"
}
Write-Host 'Installed'
