# Stops Nova's satellite on this PC, for the two scripts which replace its code: deploy_pc.sh's
# (over ssh) and update_pc.ps1 (the helper the update task runs). Dot-source it, then call
# Stop-Nova:
#
#   . .\stop_nova.ps1
#
# Everything running is stopped, since the running satellite holds numpy's and the sound
# libraries' files open and pip can't replace them, nor can Nova.exe be replaced while it
# runs, and stopping the satellite alone isn't enough, as the launcher starts it again within
# seconds. So both tasks are stopped first (either may not be there yet), then the processes:
# Nova.exe processes run from the install's venv\Scripts, and, for an install from before
# Nova.exe, the venv's pythonw running app\launcher.pyw or app\satellite.py, found by its
# command line. Any other Python on the PC is left alone. Windows has no SIGTERM, so ending
# the satellite is a hard stop, which the launcher's next start forgives. The task "Nova
# update" is never stopped here: it is the helper's own.
#
# Plain ASCII, and nothing newer than Windows PowerShell 5.1.

$base = Join-Path $env:LOCALAPPDATA 'nova'
$app = Join-Path $base 'app'
$scripts = (Join-Path $app 'venv\Scripts') + '\'

# The processes which are Nova's: both the new Nova.exe and the old pythonw.
function Find-Running {
    Get-CimInstance Win32_Process -Filter "Name = 'Nova.exe'" | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($scripts, [StringComparison]::OrdinalIgnoreCase) }
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" | Where-Object {
        $_.CommandLine -and ($_.CommandLine.IndexOf((Join-Path $app 'satellite.py'), [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            $_.CommandLine.IndexOf((Join-Path $app 'scripts\windows\launcher.pyw'), [StringComparison]::OrdinalIgnoreCase) -ge 0) }
}

# Stops both tasks and then the processes, up to 20 tries half a second apart, and throws if
# Nova is still running, so an install never goes on over a running satellite.
function Stop-Nova {
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
}
