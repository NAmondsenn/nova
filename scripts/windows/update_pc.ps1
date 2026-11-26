# Installs the update its brain sent, on this PC, and takes it back if the new code doesn't
# reconnect. Run by the scheduled task "Nova update" (which voice_assistant/update_install.py
# starts), from the update folder, %LOCALAPPDATA%\nova\data\update, where it sits beside:
#
#   job.json       {version, previous, source}: the commit to install, the one running now,
#                  and the folder holding the new code (the folder's own `new`)
#   stop_nova.ps1  the stop shared with deploy_pc.sh
#
# It is a task of its own, with no relation to "Nova start" or "Nova satellite", because it
# stops both and a child of either would be stopped with them. It stops Nova, snapshots what
# it is about to replace (previous\app, the three files which say how the venv was built, and
# the whole venv only if the packages are about to change), runs the new code's install.ps1
# -FromUpdate, notes the commit, starts "Nova start" and waits up to 3 minutes for the file
# `confirmed` to hold the version: the new code writes it itself once it has reconnected to
# the brain (voice_assistant/update_state.py). Nothing is written to result.json when it goes
# well, as the new code writes that too.
#
# Whatever goes wrong, Nova is left started: a failure restores the snapshot, writes the
# failed result (result.json for the brain, failed.json so the same commit isn't offered
# again for ever), and starts the old code. If even the restore fails, whatever is there is
# started, with a result which says what to run. Every write is to a temporary name moved into
# place, so nothing is read half written.
#
# Plain ASCII, and nothing newer than Windows PowerShell 5.1. No parameters.

$ErrorActionPreference = 'Stop'

# How long the new code has to reconnect, in seconds.
$confirmWait = 180

$folder = $PSScriptRoot
$base = Join-Path $env:LOCALAPPDATA 'nova'
$app = Join-Path $base 'app'
$venv = Join-Path $app 'venv'
$snapshot = Join-Path $folder 'previous'
$confirmedFile = Join-Path $folder 'confirmed'

$version = ''
$previous = ''
$source = ''
$wasRunning = $false
$touched = $false
$failure = ''
$needsYou = $null

# Writes a file in the folder through a temporary name, so it is never seen half written.
function Write-Atomic([string]$Name, [string]$Text) {
    $path = Join-Path $folder $Name
    [System.IO.File]::WriteAllText($path + '.tmp', $Text, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath ($path + '.tmp') -Destination $path -Force
}

# Writes the failed result the way update_satellite.sh does: result.json for the brain to be
# told, and failed.json, the same whole result, so the commit is known to have failed.
function Write-Failure([string]$Message, $NeedsYou) {
    if ($Message.Length -gt 300) { $Message = $Message.Substring(0, 300) }
    $result = @{ version = $version; ok = $false; error = $Message }
    if ($NeedsYou) { $result.needs_you = @($NeedsYou) }
    $text = ConvertTo-Json -Compress -Depth 4 -InputObject $result
    Write-Atomic 'failed.json' $text
    Write-Atomic 'result.json' $text
}

# Copies a folder as a mirror, leaving the venv out by its path and by its name, as
# install.ps1 does. Exit codes below 8 are robocopy's ways of saying it worked.
function Copy-Tree([string]$From, [string]$To, [string[]]$Leave) {
    if ($Leave) { robocopy $From $To /MIR /XD $Leave /R:2 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Null }
    else { robocopy $From $To /MIR /R:2 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Null }
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed (exit code $LASTEXITCODE)" }
}

# The files in the venv which say how it was built, and Nova.exe, which a snapshot keeps even
# when it keeps no more of the venv.
$venvFiles = @('Scripts\Nova.exe', 'requirements-pc.sha256', 'nova-exe.sha256')

function Save-Snapshot {
    if (Test-Path -LiteralPath $snapshot) { Remove-Item -LiteralPath $snapshot -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $snapshot | Out-Null
    Copy-Tree $app (Join-Path $snapshot 'app') @($venv, 'venv')
    $kept = Join-Path $snapshot 'venv-files'
    New-Item -ItemType Directory -Force -Path $kept | Out-Null
    foreach ($name in $venvFiles) {
        $file = Join-Path $venv $name
        if (Test-Path -LiteralPath $file) { Copy-Item -LiteralPath $file -Destination $kept -Force }
    }
    # The packages are about to change if the new list isn't the one the venv was built from,
    # and then the whole venv is kept, as pip can leave it in a state nothing else mends.
    $mark = Join-Path $venv 'requirements-pc.sha256'
    $installed = ''
    if (Test-Path -LiteralPath $mark) { $installed = (Get-Content -LiteralPath $mark -Raw).Trim() }
    $wanted = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $source 'requirements-pc.txt')).Hash
    if ($wanted -ne $installed) {
        Write-Host 'The packages are about to change, so the whole venv is kept'
        Copy-Tree $venv (Join-Path $snapshot 'venv')
    }
}

function Restore-Snapshot {
    Copy-Tree (Join-Path $snapshot 'app') $app @($venv, 'venv')
    $whole = Join-Path $snapshot 'venv'
    if (Test-Path -LiteralPath $whole) { Copy-Tree $whole $venv }
    $kept = Join-Path $snapshot 'venv-files'
    foreach ($name in $venvFiles) {
        $file = Join-Path $kept (Split-Path -Leaf $name)
        if (Test-Path -LiteralPath $file) { Copy-Item -LiteralPath $file -Destination (Join-Path $venv $name) -Force }
    }
}

function Test-Confirmed {
    (Test-Path -LiteralPath $confirmedFile) -and ("$(Get-Content -LiteralPath $confirmedFile -Raw)".Trim() -eq $version)
}

try {
    try { Start-Transcript -Path (Join-Path $folder 'update_pc.log') -Force | Out-Null } catch { }
    try {
        $job = Get-Content -LiteralPath (Join-Path $folder 'job.json') -Raw | ConvertFrom-Json
        $version = [string]$job.version
        $previous = [string]$job.previous
        $source = [string]$job.source
        if (-not $version -or -not $source) { throw 'job.json has no version or source' }
        . (Join-Path $folder 'stop_nova.ps1')
        $wasRunning = @(Find-Running).Count -gt 0
        Stop-Nova
        Save-Snapshot
        # From here the code on the PC is being replaced, so a failure restores it.
        $touched = $true
        Write-Host "Installing $version"
        & (Join-Path $source 'scripts\windows\install.ps1') -Source $source -FromUpdate
        Set-Content -LiteralPath (Join-Path $app 'COMMIT') -Value $version -Encoding ASCII
        # An old one would be taken for the new code's own say-so.
        Remove-Item -LiteralPath $confirmedFile -Force -ErrorAction SilentlyContinue
        Start-ScheduledTask -TaskName 'Nova start'
        $deadline = (Get-Date).AddSeconds($confirmWait)
        while (-not (Test-Confirmed) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
        if (-not (Test-Confirmed)) {
            $failure = "The new code didn't reconnect to the brain within 3 minutes, so it was taken back."
        }
    }
    catch {
        $message = "$($_.Exception.Message)"
        if ($message.Length -gt 200) { $message = $message.Substring(0, 200) }
        $failure = "The install failed: $message"
        Write-Host $failure
    }
    if ($failure) {
        if ($touched) {
            try {
                Stop-Nova
                Restore-Snapshot
                if ($previous) { Set-Content -LiteralPath (Join-Path $app 'COMMIT') -Value $previous -Encoding ASCII }
            }
            catch {
                Write-Host "Couldn't take the new code back: $($_.Exception.Message)"
                $failure = "The new code couldn't be taken back."
                $needsYou = @{ why = "The new code couldn't be taken back."; command = 'bash scripts/deploy_pc.sh' }
            }
        }
        # Before the old code starts, so the result is there when it reconnects.
        try { Write-Failure $failure $needsYou }
        catch { Write-Host "Couldn't write the result: $($_.Exception.Message)" }
        if ($touched -or $wasRunning) { Start-ScheduledTask -TaskName 'Nova start' }
    }
}
catch {
    # Whatever else went wrong, a satellite that was running, or was being replaced, is started.
    Write-Host "Update helper failed: $($_.Exception.Message)"
    if ($touched -or $wasRunning) {
        try { Start-ScheduledTask -TaskName 'Nova start' } catch { }
    }
}
finally {
    # The unpacked code is only wanted until it is installed.
    Remove-Item -LiteralPath (Join-Path $folder 'new') -Recurse -Force -ErrorAction SilentlyContinue
    try { Stop-Transcript | Out-Null } catch { }
}
