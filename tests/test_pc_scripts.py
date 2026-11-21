"""
Windows Scripts Test
Checks, by reading them as text (nothing here can run PowerShell), that install.ps1 builds
Nova.exe into the venv from files the repository has, registers the logon task and a second
task for a deploy to start, makes the Start-menu shortcut open Nova, and never touches the
user's logon choice; that deploy_pc.sh finds the old pythonw Nova and the new Nova.exe, notes
what was running before it stops anything, and starts Nova again only then, in a command
short enough for the computer's shell; and that check_app.ps1 only looks. The real proof is
the first deploy on the computer.
"""

import base64
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS = os.path.join(ROOT, "scripts", "windows")
INSTALL = os.path.join(WINDOWS, "install.ps1")
CHECK_APP = os.path.join(WINDOWS, "check_app.ps1")
DEPLOY = os.path.join(ROOT, "scripts", "deploy_pc.sh")

# cmd.exe's longest command line, which Windows' ssh server runs a command through by default.
CMD_LONGEST = 8191


def read(path: str) -> str:
    with open(path, encoding="ascii") as file:
        return file.read()


def statements(script: str) -> list:
    """
    Args:
        script: A PowerShell script's text.

    Returns:
        Its lines with comments left out and each backtick continuation joined to the next.
    """
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    return code.replace("`\n", " ").splitlines()


def deploy_powershell() -> str:
    """The PowerShell deploy_pc.sh runs on the computer, as written between its POWERSHELL marks."""
    return re.search(r"<<'POWERSHELL'\n(.*?)\nPOWERSHELL\n", read(DEPLOY), re.DOTALL).group(1)


def registration(name: str) -> str:
    """The Register-ScheduledTask statement in install.ps1 for the task of this name."""
    found = [line for line in statements(read(INSTALL))
             if "Register-ScheduledTask" in line and f"-TaskName '{name}'" in line]
    assert len(found) == 1
    return found[0]


def assigned(variable: str) -> str:
    """The statement in install.ps1 which gives this variable its value."""
    found = [line for line in statements(read(INSTALL)) if line.strip().startswith(f"{variable} = ")]
    assert len(found) == 1
    return found[0]


def test_install_builds_nova_exe_into_the_venv():
    script = read(INSTALL)
    for name in ("nova_exe.cs", "nova.manifest", "icons\\nova.ico"):
        assert name in script
        assert os.path.exists(os.path.join(WINDOWS, *name.split("\\")))
    assert "venv\\Scripts\\Nova.exe" in script
    # What made it on his computer: a windowed 64-bit program, by Add-Type or by csc itself.
    assert "/target:winexe /platform:x64" in script
    assert "-CompilerParameters" in script
    assert "csc.exe" in script


def test_install_registers_both_tasks():
    logon = registration("Nova satellite")
    start = registration("Nova start")
    assert "-Trigger" in logon
    assert "-Trigger" not in start
    logon_action = re.search(r"-Action (\$\w+)", logon).group(1)
    start_action = re.search(r"-Action (\$\w+)", start).group(1)
    assert "--logon" in assigned(logon_action)
    assert "--logon" not in assigned(start_action)
    # Both run the launcher, through the same program.
    for action in (logon_action, start_action):
        assert "-Execute $program" in assigned(action)
        assert "$launcher" in assigned(action)
    assert "\n".join(statements(read(INSTALL))).count("--logon") == 1


def test_the_shortcut_opens():
    assert "--open" in assigned("$link.Arguments")
    assert "$program" in assigned("$link.TargetPath")
    assert "[NovaInstall.AppUserModel]::SetId($shortcut, 'Nova.Satellite')" in read(INSTALL)


def test_install_never_touches_the_logon_choice():
    assert "no_logon_start" not in read(INSTALL)


def test_deploy_finds_both_old_and_new():
    script = deploy_powershell()
    assert "Name = 'Nova.exe'" in script
    assert "ExecutablePath" in script
    assert "venv\\Scripts" in script
    assert "Name = 'pythonw.exe'" in script
    assert "'satellite.py'" in script
    assert "'scripts\\windows\\launcher.pyw'" in script


def test_deploy_starts_only_what_was_running():
    script = deploy_powershell()
    noted = max(script.index("$wasRunning = "), script.index("$firstInstall = "))
    assert "Test-Path (Join-Path $app 'satellite.py')" in script[:noted + 100]
    assert noted < script.index("Stop-ScheduledTask")
    assert noted < script.index("install.ps1")
    # Both tasks are stopped, either of which may not be there yet.
    assert "'Nova satellite', 'Nova start'" in script
    started = script.index("Start-ScheduledTask -TaskName 'Nova start'")
    assert "($wasRunning -or $firstInstall)" in script[:started]
    assert script.count("Start-ScheduledTask") == 1
    assert "Nova wasn't running, so it is left stopped" in script


def test_the_deploy_command_fits_the_computers_shell():
    script = "$COMMIT = '" + "0" * 40 + "'\n" + deploy_powershell() + "\n"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    command = "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " + encoded
    assert len(command) < CMD_LONGEST


def test_install_is_plain_ascii():
    with open(INSTALL, "rb") as file:
        assert all(byte < 128 for byte in file.read())


@pytest.mark.parametrize("name", ["check_app.ps1", "nova_exe.cs", "nova.manifest"])
def test_the_other_windows_files_are_plain_ascii(name):
    with open(os.path.join(WINDOWS, name), "rb") as file:
        assert all(byte < 128 for byte in file.read())


def test_check_app_only_looks():
    code = "\n".join(statements(read(CHECK_APP)))
    # A cmdlet's noun starts with a capital, which "Start-menu" in its messages doesn't.
    changing = r"\b(Set|Remove|Stop|Start|Register|Unregister|Enable|Disable|Copy|Move|Rename|Clear|Invoke)-[A-Z]"
    assert not re.search(changing, code)
    for call in ("New-Item", "Out-File", "Add-Content", ".Save("):
        assert call not in code
    for name in ("Nova satellite", "Nova start", "Nova.lnk", "no_logon_start", "Nova.exe", "Get-ScheduledTaskInfo",
                 "LastTaskResult"):
        assert name in code


def test_check_app_fits_the_computers_shell():
    # As its header says to send it: without its comment lines.
    code = "".join(line for line in read(CHECK_APP).splitlines(keepends=True) if not re.match(r" *#", line))
    encoded = base64.b64encode(code.encode("utf-16-le")).decode("ascii")
    command = "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " + encoded
    assert len(command) < CMD_LONGEST
