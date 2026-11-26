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
HELPER = os.path.join(WINDOWS, "update_pc.ps1")
STOP = os.path.join(WINDOWS, "stop_nova.ps1")

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


def test_install_never_leaves_a_half_written_nova_exe():
    # Written beside it and moved into place, so a failure part way leaves the old one whole.
    code = "\n".join(statements(read(INSTALL)))
    staged = re.search(r"^\s*(\$\w+) = \$novaExe \+ '\.new'$", code, re.MULTILINE)
    assert staged
    name = re.escape(staged.group(1))
    removed = re.search(r"Remove-Item -LiteralPath " + name, code)
    copied = re.search(r"Copy-WithStack \$built " + name + r" \$reserve", code)
    moved = re.search(r"Move-Item -LiteralPath " + name + r" -Destination \$novaExe -Force", code)
    assert removed and copied and moved
    assert removed.start() < copied.start() < moved.start()
    assert "Copy-WithStack $built $novaExe" not in code
    # The mark which says it's built is written only once it's in place.
    assert moved.start() < code.index("Set-Content -LiteralPath $exeMark")


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
    # Through stop_nova.ps1, which it dot-sources.
    script = read(STOP)
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
    assert noted < script.index("Stop-Nova")
    assert noted < script.index("install.ps1')")
    # Both tasks are stopped, either of which may not be there yet, by stop_nova.ps1.
    assert "'Nova satellite', 'Nova start'" in read(STOP)
    started = script.index("Start-ScheduledTask -TaskName 'Nova start'")
    assert "($wasRunning -or $firstInstall)" in script[:started]
    # And only once the password for the brain is there, which a first install has yet to be given.
    env_check = script.index("Test-Path (Join-Path $base 'config\\.env')")
    assert script.index("$wasRunning -or $firstInstall") < env_check < started
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


def helper_code() -> str:
    """update_pc.ps1 without its comments, one statement per line."""
    return "\n".join(statements(read(HELPER)))


def before(code: str, first: str, second: str):
    """Asserts that first is in the code, and comes before second."""
    assert first in code, first
    assert second in code, second
    assert code.index(first) < code.index(second)


def test_install_registers_the_update_task_except_from_an_update():
    code = "\n".join(statements(read(INSTALL)))
    assert "[switch]$FromUpdate" in code
    update = registration("Nova update")
    assert "-Trigger" not in update
    assert "-Principal $principal" in update and "-Settings $taskSettings" in update
    action = re.search(r"-Action (\$\w+)", update).group(1)
    assert "-Execute 'powershell.exe'" in assigned(action)
    for text in ("-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File",
                 "update_pc.ps1"):
        assert text in assigned(action)
    # Registered inside the only if-not-from-update block, which nothing else is in.
    start = code.index("if (-not $FromUpdate) {")
    assert start < code.index("Register-ScheduledTask -TaskName 'Nova update'")
    # And it comes after the other two, which are registered whatever the switch says.
    assert code.index("Register-ScheduledTask -TaskName 'Nova start'") < start
    assert code.count("$FromUpdate") == 2


def test_the_helper_snapshots_before_it_installs():
    code = helper_code()
    stop = code.index("        Stop-Nova\n")
    saved = code.index("        Save-Snapshot\n")
    installed = code.index("install.ps1')")
    assert stop < saved < installed
    assert "robocopy $From $To /MIR /XD $Leave" in code
    assert "Copy-Tree $app (Join-Path $snapshot 'app') @($venv, 'venv')" in code
    for name in ("Scripts\\Nova.exe", "requirements-pc.sha256", "nova-exe.sha256"):
        assert f"'{name}'" in code
    assert "venv-files" in code
    # The code is only counted as replaced once the snapshot is whole.
    assert saved < code.index("$touched = $true") < installed


def test_the_helper_installs_from_update_and_writes_the_commit():
    code = helper_code()
    assert "$confirmWait = 180" in code
    assert "-Source $source -FromUpdate" in code
    assert "Start-Transcript -Path (Join-Path $folder 'update_pc.log')" in code
    assert ". (Join-Path $folder 'stop_nova.ps1')" in code
    assert "$ErrorActionPreference = 'Stop'" in code
    installed = code.index("-FromUpdate")
    commit = code.index("Set-Content -LiteralPath (Join-Path $app 'COMMIT') -Value $version")
    # A stale confirmed can't pass the wait, so it goes before the new code is started.
    removed = code.index("Remove-Item -LiteralPath $confirmedFile")
    started = code.index("Start-ScheduledTask -TaskName 'Nova start'")
    waited = code.index("Test-Confirmed")
    assert installed < commit < removed < started < code.index("$deadline")
    assert "Remove-Item -LiteralPath (Join-Path $folder 'new')" in code
    assert code.index("finally {") < code.index("Remove-Item -LiteralPath (Join-Path $folder 'new')")
    assert waited > 0


def test_the_helper_restores_and_restarts_without_confirmation():
    code = helper_code()
    assert "The new code didn't reconnect to the brain within 3 minutes, so it was taken back." in code
    assert '"The install failed: $message"' in code
    assert "$message.Substring(0, 200)" in code
    failed = code.index("if ($failure) {")
    after = code[failed:]
    before(after, "Stop-Nova", "Restore-Snapshot")
    before(after, "Restore-Snapshot", "Set-Content -LiteralPath (Join-Path $app 'COMMIT') -Value $previous")
    # The result is written, both files, before the old code starts.
    before(after, "Write-Failure $failure $needsYou", "Start-ScheduledTask -TaskName 'Nova start'")
    assert "Write-Atomic 'failed.json' $text" in code and "Write-Atomic 'result.json' $text" in code
    assert "Move-Item -LiteralPath ($path + '.tmp') -Destination $path -Force" in code
    assert "ok = $false" in code
    # A restore which fails still starts what is there, and says what to run.
    assert "The new code couldn't be taken back." in code
    assert "command = 'bash scripts/deploy_pc.sh'" in code
    # Every path ends with Nova started: the outer catch starts it too.
    assert code.count("Start-ScheduledTask -TaskName 'Nova start'") == 3


def test_the_helper_never_starts_nova_over_one_still_running():
    # A failure before the code was touched - the hold wait, or a Stop-Nova which threw -
    # can leave the old satellite running, and a second start over it would run two.
    code = helper_code()
    assert "$touched -or $wasRunning" not in code
    gone = function_body(read(HELPER), "Test-NovaGone")
    assert "@(Find-Running).Count -eq 0" in gone
    # When it can't tell - stop_nova.ps1 never loaded - it says no.
    assert "catch { $false }" in gone
    failed = code[code.index("if ($failure) {"):]
    assert failed.count("if ($touched -or ($wasRunning -and (Test-NovaGone))) {") == 2


def test_the_helper_snapshots_the_venv_only_when_the_packages_change():
    code = helper_code()
    assert "$wanted -ne $installed" in code
    assert "requirements-pc.txt" in code and "requirements-pc.sha256" in code
    whole = "Copy-Tree $venv (Join-Path $snapshot 'venv')"
    assert code.index("$wanted -ne $installed") < code.index(whole)
    assert code.count(whole) == 1
    # Restoring the whole venv is conditional on it having been kept.
    assert "if (Test-Path -LiteralPath $whole) { Copy-Tree $whole $venv }" in code


def function_body(code: str, name: str) -> str:
    """The text of a function in a PowerShell script, up to its closing brace at line start."""
    found = re.search(r"^function " + re.escape(name) + r"\b.*?^}", code, re.DOTALL | re.MULTILINE)
    assert found, name
    return found.group(0)


def test_deploy_and_the_helper_stop_nova_the_same_way():
    stop = read(STOP)
    # Today's finding and stopping, now defined once.
    assert "function Find-Running" in stop and "function Stop-Nova" in stop
    body = function_body(stop, "Stop-Nova")
    assert "'Nova satellite', 'Nova start'" in body
    assert "$try -lt 20" in body
    assert "throw 'Nova is still running" in body
    assert "Nova update" not in "\n".join(statements(stop))
    for line in ("Name = 'Nova.exe'", "Name = 'pythonw.exe'", "'satellite.py'", "'scripts\\windows\\launcher.pyw'"):
        assert line in stop
    deploy = deploy_powershell()
    helper = read(HELPER)
    for script in (deploy, helper):
        assert "stop_nova.ps1" in script
    # Neither carries a copy of it.
    for script in (deploy, helper):
        assert "function Find-Running" not in script
        assert "Stop-ScheduledTask" not in script
    # Deploy expands the archive first, and notes what was running before it stops anything.
    before(deploy, "Expand-Archive", ". (Join-Path $src 'scripts\\windows\\stop_nova.ps1')")
    before(deploy, "$firstInstall = ", "Stop-Nova")
    before(deploy, "$wasRunning = ", "Stop-Nova")
    before(deploy, "Stop-Nova", "install.ps1")
    assert "-FromUpdate" not in deploy


@pytest.mark.parametrize("path", [HELPER, STOP])
def test_the_new_scripts_are_plain_ascii(path):
    with open(path, "rb") as file:
        assert all(byte < 128 for byte in file.read())


def test_the_deploy_command_still_fits_after_sharing_the_stop():
    script = "$COMMIT = '" + "0" * 40 + "'\n" + deploy_powershell() + "\n"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    command = "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " + encoded
    assert len(command) < CMD_LONGEST


def test_the_helper_waits_while_the_satellite_holds_it_off():
    code = helper_code()
    assert "$holdFile = Join-Path $folder 'hold'" in code
    assert "$holdMost = 600" in code
    wait = code.index("while ((Test-Path -LiteralPath $holdFile) -and $waited -lt $holdMost)")
    assert "Start-Sleep -Seconds 2; $waited += 2" in code[wait:wait + 200]
    # Before the first stop, which is the one that would cut the satellite off.
    assert wait < code.index("        Stop-Nova\n")
