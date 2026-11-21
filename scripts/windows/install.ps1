# Installs Nova's satellite on this Windows PC, for the user running it, under
# %LOCALAPPDATA%\nova: app (the code, and its venv), config and data. Run it over ssh
# by scripts/deploy_pc.sh, or by hand from the folder the code is in:
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 -Source <folder>
#
# It's safe to run again, which is how an update goes: the code is copied over, the
# packages are installed again only if requirements-pc.txt changed, venv\Scripts\Nova.exe
# is built again only if what it's built from changed, data\lib\speexdsp.dll (the echo
# canceller's library) is fetched again only if it isn't the pinned one, the settings file
# is left alone if there is one, and the tasks and the Start-menu shortcut are replaced.
# Two tasks run the launcher: "Nova satellite" at logon, and "Nova start", with no trigger,
# for a deploy to start; the shortcut runs it too. All three run it through Nova.exe, so
# Windows names the process Nova, or through pythonw.exe if Nova.exe couldn't be built. It
# never writes .env: the password the brain's `add-satellite pc` gave goes in config\.env
# by hand, as NOVA_BACKEND_TOKEN=<password>. It doesn't start or stop the satellite either.
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
$novaExe = Join-Path $app 'venv\Scripts\Nova.exe'
$windows = Join-Path $app 'scripts\windows'
$launcher = Join-Path $windows 'launcher.pyw'
$icon = Join-Path $windows 'icons\nova.ico'

if (-not (Test-Path (Join-Path $Source 'satellite.py'))) {
    throw "$Source has no satellite.py, so it isn't Nova's code"
}

foreach ($folder in @($app, $config, $data)) {
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
}

# The code. /MIR drops files which are gone from the source, but not the venv, which
# isn't in it - left out by its path and by its name, as robocopy matches either. A file
# it can't copy is tried twice, two seconds apart, rather than a million times, 30 s
# apart, which would hang the deploy. Exit codes below 8 are robocopy's ways of saying
# it worked.
Write-Host "Copying the code to $app"
robocopy $Source $app /MIR /XD $venv venv /R:2 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Null
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

# The echo canceller's library, speexdsp.dll, which Windows has no package for: conda-forge's
# speexdsp 1.2.1 (build h5112557_1) for win-64, an MSVC build of xiph/speexdsp. It imports
# kernel32.dll, the Universal C Runtime's api-ms-win-crt-*.dll (Windows' own) and
# vcruntime140.dll, which the satellite's Python has already loaded - python313.dll needs it
# too, and the loader gives a library a module of the name which is loaded already - so
# nothing is shipped with it. Nothing is taken out of the package until its SHA-256 is the
# one written here. A .conda file is a zip holding a zstd-compressed tar, which neither
# PowerShell 5.1 nor Python 3.13 can read by themselves, so unpack_library.py does it with
# the venv's zstandard (requirements-pc.txt), writing only the one DLL, which is checked as
# well, into data\lib, where voice_assistant/echo.py looks for it. It is fetched only when
# the DLL there isn't the pinned one. A failure is a warning, never a failed install:
# without the library the satellite runs with no echo canceller, as it did before there was
# one to load.
$speexUrl = 'https://conda.anaconda.org/conda-forge/win-64/speexdsp-1.2.1-h5112557_1.conda'
$speexPackageHash = '21b24675b22c7863cf9cadbb6eb0f1f8092df8e23f78bd58a0ab8439f2ac5839'
$speexMember = 'Library/bin/speexdsp.dll'
$speexHash = '8d8f90acb6a191f087ec07f80f67b82f296a6e4afad4612f2a4a75e99c6ba21b'
$speexDll = Join-Path $data 'lib\speexdsp.dll'
$speexTemp = Join-Path $env:LOCALAPPDATA 'Temp\nova-speexdsp'
try {
    $speexHave = ''
    if (Test-Path -LiteralPath $speexDll) { $speexHave = (Get-FileHash -Algorithm SHA256 -LiteralPath $speexDll).Hash }
    if ($speexHave -eq $speexHash) {
        Write-Host 'speexdsp.dll is the one pinned'
    }
    else {
        Write-Host 'Fetching speexdsp.dll, for the echo canceller'
        if (Test-Path -LiteralPath $speexTemp) { Remove-Item -LiteralPath $speexTemp -Recurse -Force }
        New-Item -ItemType Directory -Path $speexTemp | Out-Null
        $package = Join-Path $speexTemp 'speexdsp.conda'
        # Windows PowerShell 5.1 offers older TLS than the server takes unless it is told, and
        # draws a progress bar which makes the download many times slower.
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $progress = $ProgressPreference
        $ProgressPreference = 'SilentlyContinue'
        try { Invoke-WebRequest -UseBasicParsing -Uri $speexUrl -OutFile $package }
        finally { $ProgressPreference = $progress }
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $package).Hash
        if ($got -ne $speexPackageHash) { throw "the download's SHA-256 is $got, not the pinned $speexPackageHash" }
        & $venvPython -I (Join-Path $windows 'unpack_library.py') $package $speexMember $speexTemp | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "unpack_library.py failed (exit code $LASTEXITCODE)" }
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $speexTemp 'speexdsp.dll')).Hash
        if ($got -ne $speexHash) { throw "the DLL's SHA-256 is $got, not the pinned $speexHash" }
        # Written beside it and then moved into place, so a failure part way leaves the old
        # DLL whole, as for Nova.exe.
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $speexDll) | Out-Null
        $speexStaged = $speexDll + '.new'
        Copy-Item -LiteralPath (Join-Path $speexTemp 'speexdsp.dll') -Destination $speexStaged -Force
        Move-Item -LiteralPath $speexStaged -Destination $speexDll -Force
        Write-Host "Installed $speexDll"
    }
}
catch {
    Write-Warning "Couldn't fetch speexdsp.dll, so the satellite has no echo canceller: $($_.Exception.Message)"
}
if (Test-Path -LiteralPath $speexTemp) { Remove-Item -LiteralPath $speexTemp -Recurse -Force -ErrorAction SilentlyContinue }

# This install's own settings, only if there are none: what differs from config.yaml,
# which is the brain's address and name for this satellite, and a key press instead of a
# wake word. (The echo canceller is left on, as config.yaml has it: it does nothing until a
# conversation holds the speaker and microphone open.)
$settings = Join-Path $config 'config.yaml'
if (-not (Test-Path $settings)) {
    Write-Host "Writing $settings"
    $text = @'
backend:
  address: "nova:10400"
  satellite: "pc"
satellite:
  trigger: "push_to_talk"
'@
    [System.IO.File]::WriteAllText($settings, $text + "`n", (New-Object System.Text.UTF8Encoding($false)))
}
else {
    # An install from before conversations wrote the echo canceller off, which a conversation
    # needs. Only those two lines as it wrote them are taken out, which leaves config.yaml's
    # setting, on; a file with anything else under audio is left alone, as every file is.
    # (The last part of the pattern is that the next line which isn't blank isn't indented,
    # or there is none: anything indented would then be under the section before.)
    $oldEcho = '(?m)^audio:\r?\n  echo_cancellation: false[ \t]*(?:\r?\n|(?![\s\S]))(?=(?:[ \t]*\r?\n)*(?:[^ \t\r\n]|(?![\s\S])))'
    $text = [System.IO.File]::ReadAllText($settings)
    if ([regex]::IsMatch($text, $oldEcho)) {
        Write-Host "Turning the echo canceller on in $settings"
        [System.IO.File]::WriteAllText($settings, [regex]::Replace($text, $oldEcho, ''), (New-Object System.Text.UTF8Encoding($false)))
    }
    else {
        Write-Host "Leaving $settings as it is"
    }
}

# A program's machine, subsystem (2 windowed, 3 console) and stack reserve, from its PE headers.
function Get-PeInfo([string]$Path) {
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $pe = [BitConverter]::ToInt32($bytes, 0x3C)
    if ($bytes[$pe] -ne 0x50 -or $bytes[$pe + 1] -ne 0x45) { throw "$Path isn't a PE file" }
    $optional = $pe + 24
    $magic = [BitConverter]::ToUInt16($bytes, $optional)
    if ($magic -eq 0x20B) { $stack = [BitConverter]::ToUInt64($bytes, $optional + 72) }
    else { $stack = [BitConverter]::ToUInt32($bytes, $optional + 72) }
    New-Object PSObject -Property @{
        Machine = ('0x{0:X4}' -f [BitConverter]::ToUInt16($bytes, $pe + 4))
        Subsystem = [BitConverter]::ToUInt16($bytes, $optional + 68)
        StackReserve = $stack
        Is64 = ($magic -eq 0x20B)
    }
}

# Copies a 64-bit program, giving it the stack reserve asked for. csc has no /stack
# option, and python.exe and pythonw.exe are linked with a bigger stack than csc's 1 MB,
# so Python's main thread would otherwise have less room than under pythonw.
function Copy-WithStack([string]$From, [string]$To, [UInt64]$Reserve) {
    $bytes = [System.IO.File]::ReadAllBytes($From)
    $optional = [BitConverter]::ToInt32($bytes, 0x3C) + 24
    if ([BitConverter]::ToUInt16($bytes, $optional) -ne 0x20B) { throw "$From isn't a 64-bit program" }
    $new = [BitConverter]::GetBytes([UInt64]$Reserve)
    [Array]::Copy($new, 0, $bytes, $optional + 72, 8)
    [System.IO.File]::WriteAllBytes($To, $bytes)
}

# Compiles $Code (nova_exe.cs) into the program $Built with Windows' own C# compiler, as
# Add-Type runs it, or as csc.exe itself if Add-Type fails, and says which built it.
# /target:winexe makes a windowed program (csc takes the last /target, after the /t:exe
# Add-Type adds for GenerateExecutable); /platform:x64 makes it 64-bit only, which it must
# be to load a 64-bit python3NN.dll. Every option is in the CompilerParameters, as
# PowerShell 5.1 refuses some parameters alongside -CompilerParameters.
function Build-NovaExe([string]$Code, [string]$Icon, [string]$Manifest, [string]$Built) {
    $options = '/target:winexe /platform:x64 /optimize+ /win32icon:"' + $Icon + '" /win32manifest:"' + $Manifest + '"'
    try {
        $parameters = New-Object System.CodeDom.Compiler.CompilerParameters
        $parameters.GenerateExecutable = $true
        $parameters.GenerateInMemory = $false
        $parameters.IncludeDebugInformation = $false
        $parameters.OutputAssembly = $Built
        $parameters.CompilerOptions = $options
        Add-Type -TypeDefinition ([System.IO.File]::ReadAllText($Code)) -CompilerParameters $parameters
    }
    catch {
        Write-Warning "Add-Type: $($_.Exception.Message)"
    }
    # Whether the program was written, rather than whether Add-Type complained, says
    # whether it was built: Add-Type can complain after writing it.
    if (Test-Path -LiteralPath $Built) { return 'Add-Type' }
    $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    Write-Host "Trying $csc directly"
    $output = & $csc /nologo /target:winexe /platform:x64 /optimize+ "/win32icon:$Icon" "/win32manifest:$Manifest" "/out:$Built" $Code
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Built)) {
        throw "csc.exe failed (exit code $LASTEXITCODE): $($output -join ' ')"
    }
    return 'csc.exe'
}

# Nova.exe, which runs Python inside a process Windows names Nova, with Nova's icon (see
# nova_exe.cs). Built only when nova_exe.cs, the icon or nova.manifest changed since it was
# last built, by the mark kept in the venv, as the packages' is, so a new venv has none.
# A build which fails either way is a warning, never a failed install: the tasks and the
# shortcut then run pythonw.exe, as before there was Nova.exe.
$exeMark = Join-Path $venv 'nova-exe.sha256'
try {
    $exeSources = @((Join-Path $windows 'nova_exe.cs'), $icon, (Join-Path $windows 'nova.manifest'))
    $exeHash = ($exeSources | ForEach-Object { (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash }) -join ' '
    $exeBuiltFrom = ''
    if (Test-Path -LiteralPath $exeMark) { $exeBuiltFrom = "$(Get-Content -LiteralPath $exeMark -Raw)".Trim() }
    # A running program's file can't be replaced, so a Nova.exe still running (an install
    # run by hand, as a deploy stops Nova first) is left until the next install.
    $exeRunning = @(Get-CimInstance Win32_Process -Filter "Name = 'Nova.exe'" | Where-Object {
        $_.ExecutablePath -and [string]::Equals($_.ExecutablePath, $novaExe, [StringComparison]::OrdinalIgnoreCase) })
    if ($exeHash -eq $exeBuiltFrom -and (Test-Path -LiteralPath $novaExe)) {
        Write-Host 'Nova.exe is as nova_exe.cs, its icon and nova.manifest say'
    }
    elseif ($exeRunning.Count -gt 0) {
        Write-Host "Nova is running from $novaExe, so it can't be replaced: it is left as it is, and built again at the next install with Nova stopped"
    }
    else {
        # Compiled into a build folder outside the venv, then copied in with the stack set:
        # Add-Type may load what it built into this PowerShell, which keeps that file from
        # being written again until it ends.
        Write-Host 'Building Nova.exe'
        $build = Join-Path $env:LOCALAPPDATA 'Temp\nova-exe-build'
        if (Test-Path -LiteralPath $build) { Remove-Item -LiteralPath $build -Recurse -Force }
        New-Item -ItemType Directory -Path $build | Out-Null
        $built = Join-Path $build 'Nova.exe'
        $compiler = Build-NovaExe (Join-Path $windows 'nova_exe.cs') $icon (Join-Path $windows 'nova.manifest') $built
        # The base install's pythonw.exe gives the stack reserve, found from pyvenv.cfg's
        # "home = <dir>" as Nova.exe finds it.
        $reserve = (Get-PeInfo $built).StackReserve
        $pythonHome = $null
        foreach ($line in [System.IO.File]::ReadAllLines((Join-Path $venv 'pyvenv.cfg'))) {
            $parts = $line.Split([char[]]@('='), 2)
            if ($parts.Count -eq 2 -and $parts[0].Trim() -eq 'home') { $pythonHome = $parts[1].Trim(); break }
        }
        $basePythonw = $null
        if ($pythonHome) { $basePythonw = Join-Path $pythonHome 'pythonw.exe' }
        if ($basePythonw -and (Test-Path -LiteralPath $basePythonw)) {
            $reserve = (Get-PeInfo $basePythonw).StackReserve
        }
        else {
            Write-Warning "There's no base pythonw.exe to take the stack size from, so Nova.exe keeps the compiler's"
        }
        # Written beside it and then moved into place, so a failure part way leaves the old
        # Nova.exe whole rather than a broken one every start would then run. A .new left by
        # such a failure goes first.
        $staged = $novaExe + '.new'
        if (Test-Path -LiteralPath $staged) { Remove-Item -LiteralPath $staged -Force }
        Copy-WithStack $built $staged $reserve
        Move-Item -LiteralPath $staged -Destination $novaExe -Force
        Set-Content -LiteralPath $exeMark -Value $exeHash -Encoding ASCII
        Write-Host "Built $novaExe with $compiler (stack reserve $reserve)"
        Remove-Item -LiteralPath $build -Recurse -Force -ErrorAction SilentlyContinue
    }
}
catch {
    Write-Warning "Couldn't build Nova.exe, so an earlier Nova.exe, or else pythonw.exe, runs Nova: $($_.Exception.Message)"
}

# What the tasks and the shortcut run the launcher with.
$program = $venvPythonw
if (Test-Path -LiteralPath $novaExe) { $program = $novaExe }

# Started in his own session, so it has his microphone and speakers; the launcher runs
# the satellite and starts it again whenever it exits. "Nova satellite" starts it at his
# logon, with --logon so the launcher can tell that start from the others; "Nova start"
# has no trigger and is there for a deploy to start, so a deploy's start is never taken
# for a logon. One copy only, no time limit, and at normal priority (5) rather than the
# below-normal 7 a task is given, so a game doesn't break up its sound.
# The account as Windows knows it. Over ssh USERDOMAIN reads WORKGROUP, which names no
# account, so it's taken from the process's own identity rather than the environment.
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$logonAction = New-ScheduledTaskAction -Execute $program -Argument ('"' + $launcher + '" --logon') -WorkingDirectory $app
$startAction = New-ScheduledTaskAction -Execute $program -Argument ('"' + $launcher + '"') -WorkingDirectory $app
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 5
Write-Host "Registering the tasks 'Nova satellite' and 'Nova start' for $user, running $program"
Register-ScheduledTask -TaskName 'Nova satellite' -Action $logonAction -Trigger $trigger -Principal $principal `
    -Settings $taskSettings -Description "Nova's satellite, started at logon" -Force | Out-Null
Register-ScheduledTask -TaskName 'Nova start' -Action $startAction -Principal $principal `
    -Settings $taskSettings -Description "Nova's satellite, started by a deploy" -Force | Out-Null

# A Start-menu shortcut "Nova", which Windows names the satellite's music after in its
# media controls (docs/decisions/0031): the satellite says it is Nova.Satellite, and
# Windows looks for the shortcut with that AppUserModelID. It runs the launcher as the
# tasks do, with --open, so that while Nova runs it opens Nova's settings page instead,
# and it has Nova's icon. Made again each time, and a shortcut which can't be made only
# leaves the music named after the program, so that is a warning, never a failed install.
try {
    $shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'Nova.lnk'
    if (-not ('NovaInstall.AppUserModel' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace NovaInstall {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey {
        public Guid FormatId;
        public uint PropertyId;
    }

    // A PROPVARIANT holding a string: its type, then a pointer to the text. 24 bytes on
    // 64-bit Windows, as the real one is.
    [StructLayout(LayoutKind.Explicit)]
    public struct PropVariant {
        [FieldOffset(0)] public ushort Type;
        [FieldOffset(8)] public IntPtr Pointer;
        [FieldOffset(16)] public IntPtr Spare;
    }

    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPropertyStore {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int GetAt(uint index, out PropertyKey key);
        [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
        [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
        [PreserveSig] int Commit();
    }

    public static class AppUserModel {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
        private static extern void SHGetPropertyStoreFromParsingName(string path, IntPtr bindContext, int flags,
            ref Guid interfaceId, [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);

        // Gives a shortcut file an AppUserModelID (PKEY_AppUserModel_ID).
        public static void SetId(string path, string id) {
            Guid interfaceId = typeof(IPropertyStore).GUID;
            IPropertyStore store;
            // GPS_READWRITE
            SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 2, ref interfaceId, out store);
            try {
                PropertyKey key = new PropertyKey();
                key.FormatId = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
                key.PropertyId = 5;
                PropVariant value = new PropVariant();
                value.Type = 31; // VT_LPWSTR
                value.Pointer = Marshal.StringToCoTaskMemUni(id);
                try {
                    Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref value));
                    Marshal.ThrowExceptionForHR(store.Commit());
                }
                finally {
                    Marshal.FreeCoTaskMem(value.Pointer);
                }
            }
            finally {
                Marshal.ReleaseComObject(store);
            }
        }
    }
}
'@
    }
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
    $link.TargetPath = $program
    $link.Arguments = '"' + $launcher + '" --open'
    $link.WorkingDirectory = $app
    $link.Description = "Nova's satellite"
    if ($program -eq $novaExe) { $link.IconLocation = $novaExe + ',0' }
    else { $link.IconLocation = $icon + ',0' }
    $link.Save()
    [NovaInstall.AppUserModel]::SetId($shortcut, 'Nova.Satellite')
    Write-Host "Made the Start-menu shortcut $shortcut, so Windows' media controls say Nova"
}
catch {
    Write-Warning "Couldn't make the Start-menu shortcut, so Windows' media controls will say $(Split-Path -Leaf $program): $($_.Exception.Message)"
}

if (-not (Test-Path (Join-Path $config '.env'))) {
    Write-Host "No password yet: put NOVA_BACKEND_TOKEN=<the password add-satellite pc gave> in $(Join-Path $config '.env')"
}
Write-Host 'Installed'
