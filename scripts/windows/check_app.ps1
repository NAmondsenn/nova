# Shows how Nova stands on this Windows PC after a deploy: each Nova process (its id, its
# parent's, its description and path), whether venv\Scripts\Nova.exe is there, both tasks'
# states, what they run and how each last ended, whether the Start-menu shortcut Nova.lnk
# points at Nova.exe, and whether "Start with Windows" is off (the file no_logon_start in
# the data folder). It only looks: nothing is started, stopped or changed, and it prints
# no file's contents and nothing of the environment. Run by hand from CT 109, encoded as
# scripts/deploy_pc.sh runs its own PowerShell, with the comments left out so it fits
# cmd.exe's 8191 characters there:
#
#   encoded=$(grep -v '^ *#' scripts/windows/check_app.ps1 | iconv -f utf-8 -t utf-16le | base64 -w0)
#   ssh pc "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ${encoded}"
#
# Plain ASCII, and nothing newer than Windows PowerShell 5.1.

$ErrorActionPreference = 'Stop'

$base = Join-Path $env:LOCALAPPDATA 'nova'
$app = Join-Path $base 'app'
$novaExe = Join-Path $app 'venv\Scripts\Nova.exe'

# Every Nova.exe, wherever it runs from, and the venv's pythonw running Nova's code, as an
# install from before Nova.exe ran it.
Write-Host '--- Nova processes (id, parent, description, path)'
$found = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'Nova.exe' -or ($_.Name -eq 'pythonw.exe' -and $_.CommandLine -and
        $_.CommandLine.IndexOf($app, [StringComparison]::OrdinalIgnoreCase) -ge 0) })
if ($found.Count -eq 0) { Write-Host '(none)' }
foreach ($process in $found) {
    $description = ''
    $running = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
    if ($running) { $description = $running.Description }
    Write-Host ('{0} {1} {2} {3}' -f $process.ProcessId, $process.ParentProcessId, $description, $process.ExecutablePath)
}

Write-Host '--- Nova.exe'
if (Test-Path -LiteralPath $novaExe) {
    $version = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($novaExe)
    Write-Host "There, with the description '$($version.FileDescription)'"
}
else { Write-Host "Not there, so everything runs as pythonw.exe" }

Write-Host '--- Tasks'
foreach ($name in @('Nova satellite', 'Nova start')) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        $action = @($task.Actions)[0]
        # How it last ended, in decimal and hex: Nova.exe's own failures are 120 to 126, and
        # Windows' own (267011, 0x00041303, has not yet run) are known by their hex.
        $last = 'unknown'
        $info = Get-ScheduledTaskInfo -TaskName $name -ErrorAction SilentlyContinue
        if ($info) { $last = '{0} (0x{0:X8})' -f $info.LastTaskResult }
        Write-Host "$($name): $($task.State), runs $($action.Execute) $($action.Arguments), last result $last"
    }
    else { Write-Host "$($name): not registered" }
}

Write-Host '--- Start-menu shortcut'
$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'Nova.lnk'
if (Test-Path -LiteralPath $shortcut) {
    # Reads the shortcut; it is only written by a Save, which this never calls.
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
    $points = [string]::Equals($link.TargetPath, $novaExe, [StringComparison]::OrdinalIgnoreCase)
    Write-Host "Nova.lnk points at Nova.exe: $points (runs $($link.TargetPath) $($link.Arguments), icon $($link.IconLocation))"
}
else { Write-Host 'There is no Nova.lnk' }

Write-Host '--- Start with Windows'
if (Test-Path -LiteralPath (Join-Path $base 'data\no_logon_start')) { Write-Host 'Off: no_logon_start is there' }
else { Write-Host 'On: there is no no_logon_start' }
