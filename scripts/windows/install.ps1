# Installs Nova's satellite on this Windows PC, for the user running it, under
# %LOCALAPPDATA%\nova: app (the code, and its venv), config and data. Run it over ssh
# by scripts/deploy_pc.sh, or by hand from the folder the code is in:
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 -Source <folder>
#
# It's safe to run again, which is how an update goes: the code is copied over, the
# packages are installed again only if requirements-pc.txt changed, the settings file
# is left alone if there is one, and the logon task and the Start-menu shortcut are
# replaced. It never writes .env:
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
# only, no time limit, and at normal priority (5) rather than the below-normal 7 a task
# is given, so a game doesn't break up its sound.
# The account as Windows knows it. Over ssh USERDOMAIN reads WORKGROUP, which names no
# account, so it's taken from the process's own identity rather than the environment.
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $venvPythonw -Argument ('"' + $launcher + '"') -WorkingDirectory $app
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 5
Write-Host "Registering the task '$taskName' for $user"
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $taskSettings -Description "Nova's satellite, started at logon" -Force | Out-Null

# A Start-menu shortcut "Nova", which Windows names the satellite's music after in its
# media controls (docs/decisions/0030): the satellite says it is Nova.Satellite, and
# Windows looks for the shortcut with that AppUserModelID. It points at the launcher, as
# the task does. Made again each time, and a shortcut which can't be made only leaves
# the music named "pythonw.exe", so that is a warning, never a failed install.
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
    $link.TargetPath = $venvPythonw
    $link.Arguments = '"' + $launcher + '"'
    $link.WorkingDirectory = $app
    $link.Description = "Nova's satellite"
    $link.Save()
    [NovaInstall.AppUserModel]::SetId($shortcut, 'Nova.Satellite')
    Write-Host "Made the Start-menu shortcut $shortcut, so Windows' media controls say Nova"
}
catch {
    Write-Warning "Couldn't make the Start-menu shortcut, so Windows' media controls will say pythonw.exe: $($_.Exception.Message)"
}

if (-not (Test-Path (Join-Path $config '.env'))) {
    Write-Host "No password yet: put NOVA_BACKEND_TOKEN=<the password add-satellite pc gave> in $(Join-Path $config '.env')"
}
Write-Host 'Installed'
