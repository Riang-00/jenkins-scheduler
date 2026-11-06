"""Task registration regressions. No real tasks or UAC prompts in unit tests."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import engine as e


class RegistrationCommandTests(unittest.TestCase):
    def command(self, elevate=True):
        e.task_command(['/Create', '/TN', 'Jenkins-Scheduler-' + 'a' * 32,
                        '/XML', 'C:/synthetic folder/task.xml'], allow_elevation=elevate)

    def test_uses_structured_helper_and_keeps_paths_as_arguments(self):
        with patch.object(e.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.command()
        command = run.call_args.args[0]
        self.assertEqual(command[0], 'powershell.exe')
        self.assertIn('-AllowElevation', command)
        self.assertIn('C:/synthetic folder/task.xml', command)
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_can_disable_elevation(self):
        with patch.object(e.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.command(elevate=False)
        self.assertNotIn('-AllowElevation', run.call_args.args[0])

    def test_exit_codes_are_classified_without_exposing_output(self):
        for result_code, code in [(5, 'access_denied'), (1223, 'authorization_cancelled'),
                                  (1062, 'service_unavailable'), (13, 'invalid_task'),
                                  (183, 'task_exists'), (1, 'registration_failed')]:
            with self.subTest(code=code):
                result = subprocess.CompletedProcess([], result_code, b'synthetic-secret', b'synthetic-secret')
                with patch.object(e.subprocess, 'run', return_value=result):
                    with self.assertRaises(e.TaskRegistrationError) as caught:
                        self.command()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.result_code, result_code)
                self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_launch_failure_is_sanitized(self):
        with patch.object(e.subprocess, 'run', side_effect=OSError('synthetic-secret')):
            with self.assertRaises(e.TaskRegistrationError) as caught:
                self.command()
        self.assertEqual(caught.exception.code, 'registration_failed')
        self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_delete_does_not_elevate_or_register(self):
        with patch.object(e.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)) as run:
            with self.assertRaises(e.DeployError):
                e.task_command(['/Delete', '/TN', 'Jenkins-Scheduler-' + 'a' * 32, '/F'], allow_elevation=True)
        self.assertEqual(run.call_args.args[0][0], 'schtasks.exe')
        self.assertEqual(run.call_count, 1)


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell helper')
class RegistrationHelperTests(unittest.TestCase):
    def run_helper(self, hresult='00000000', elevation='success', allow=True, name=None, stage='register'):
        with tempfile.TemporaryDirectory(prefix='registration test ') as directory:
            root = Path(directory)
            xml = root / 'synthetic task.xml'
            xml.write_text('<Task/>', encoding='utf-16')
            captured = root / 'elevation.json'
            harness = root / 'harness.ps1'
            harness.write_text(r'''
$ErrorActionPreference = 'Stop'
function Throw-ProbeFailure {
    throw [Runtime.InteropServices.COMException]::new('synthetic-secret', [Convert]::ToInt32($env:PROBE_HRESULT, 16))
}
function Resolve-Path {
    param($LiteralPath)
    if ($env:PROBE_STAGE -eq 'xml') { Throw-ProbeFailure }
    Microsoft.PowerShell.Management\Resolve-Path -LiteralPath $LiteralPath
}
$global:FakeFolder = [pscustomobject]@{}
$global:FakeFolder | Add-Member ScriptMethod RegisterTask {
    param($name, $xml, $flags, $user, $password, $logon, $sddl)
    if ($flags -ne 2 -or $logon -ne 3 -or $null -ne $user -or $null -ne $password -or $null -ne $sddl) {
        throw 'Unexpected task registration contract'
    }
    if ($env:PROBE_STAGE -eq 'register' -and $env:PROBE_HRESULT -ne '00000000') {
        Throw-ProbeFailure
    }
}
function New-Object {
    param($ComObject)
    if ($ComObject -ne 'Schedule.Service') { throw 'Unexpected COM class' }
    $service = [pscustomobject]@{}
    $service | Add-Member ScriptMethod Connect {
        if ($env:PROBE_STAGE -eq 'connect') { Throw-ProbeFailure }
    }
    $service | Add-Member ScriptMethod GetFolder { param($path)
        if ($path -ne '\') { throw 'Unexpected folder' }
        if ($env:PROBE_STAGE -eq 'folder') { Throw-ProbeFailure }
        return $global:FakeFolder
    }
    return $service
}
function Start-Process {
    param($FilePath, $ArgumentList, $Verb, $WindowStyle, [switch]$PassThru)
    @{arguments=$ArgumentList; verb=$Verb; window=$WindowStyle} | ConvertTo-Json | Set-Content -LiteralPath $env:PROBE_CAPTURE
    if ($env:PROBE_ELEVATION -eq 'cancelled') { throw [ComponentModel.Win32Exception]::new(1223) }
    $code = if ($env:PROBE_ELEVATION -eq 'denied') { 5 } else { 0 }
    $process = [pscustomobject]@{Handle=123; ExitCode=$code}
    $process | Add-Member ScriptMethod WaitForExit {}
    return $process
}
& $env:PROBE_HELPER -XmlPath $env:PROBE_XML -TaskName $env:PROBE_NAME -AllowElevation:($env:PROBE_ALLOW -eq 'yes')
exit $LASTEXITCODE
''', encoding='utf-8')
            env = dict(os.environ, PROBE_HELPER=str(e.ROOT / 'register-elevated.ps1'),
                       PROBE_XML=str(xml), PROBE_CAPTURE=str(captured), PROBE_HRESULT=hresult,
                       PROBE_NAME=name or 'Jenkins-Scheduler-' + 'b' * 32,
                       PROBE_ELEVATION=elevation, PROBE_ALLOW='yes' if allow else 'no', PROBE_STAGE=stage)
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                                     'Bypass', '-File', str(harness)], capture_output=True, env=env, timeout=20)
            self.assertNotIn(b'synthetic-secret', result.stdout + result.stderr)
            return result.returncode, json.loads(captured.read_text()) if captured.exists() else None

    def test_standard_registration_does_not_prompt(self):
        self.assertEqual(self.run_helper(), (0, None))

    def test_denial_without_permission_to_elevate_does_not_prompt(self):
        self.assertEqual(self.run_helper('80070005', allow=False), (5, None))

    def test_access_denied_requests_one_hidden_registration_helper(self):
        code, captured = self.run_helper('80070005')
        self.assertEqual(code, 0)
        self.assertEqual(captured['verb'], 'RunAs')
        self.assertEqual(captured['window'], 'Hidden')
        self.assertNotIn('-AllowElevation', captured['arguments'])
        self.assertIn('"' + str(e.ROOT / 'register-elevated.ps1') + '"', captured['arguments'])
        xml_argument = captured['arguments'][captured['arguments'].index('-XmlPath') + 1]
        self.assertTrue(xml_argument.startswith('"') and xml_argument.endswith('"'))

    def test_cancelled_uac_is_distinct_from_denied_elevated_registration(self):
        for outcome, expected in [('cancelled', 1223), ('denied', 5)]:
            with self.subTest(outcome=outcome):
                self.assertEqual(self.run_helper('80070005', elevation=outcome)[0], expected)

    def test_other_failures_do_not_prompt(self):
        for hresult, code in [('80041315', 1062), ('800706BA', 1062), ('8004131A', 13),
                              ('800700B7', 183), ('80004005', 0x80004005)]:
            with self.subTest(hresult=hresult):
                self.assertEqual(self.run_helper(hresult), (code, None))

    def test_invalid_task_name_cannot_register_or_prompt(self):
        self.assertEqual(self.run_helper(name='unrelated-task'), (13, None))

    def test_native_root_path_not_found_uses_elevation_once(self):
        code, captured = self.run_helper('80070003', stage='folder')
        self.assertEqual(code, 0)
        self.assertEqual(captured['verb'], 'RunAs')
        self.assertNotIn('-AllowElevation', captured['arguments'])

    def test_native_root_path_failure_respects_no_elevation(self):
        self.assertEqual(self.run_helper('80070003', stage='folder', allow=False), (0x80070003, None))

    def test_missing_file_or_service_or_registration_path_never_elevates(self):
        for stage in ('xml', 'connect', 'register'):
            with self.subTest(stage=stage):
                self.assertEqual(self.run_helper('80070003', stage=stage), (0x80070003, None))

    def test_native_root_retry_preserves_cancel_and_child_failure(self):
        for outcome, expected in [('cancelled', 1223), ('denied', 5)]:
            with self.subTest(outcome=outcome):
                self.assertEqual(self.run_helper('80070003', stage='folder', elevation=outcome)[0], expected)
