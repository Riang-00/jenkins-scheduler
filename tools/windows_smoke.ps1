param(
    [Parameter(Mandatory = $true)][string]$CandidateRoot,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{32}$')][string]$ReservationId,
    [string]$PythonExe = '',
    [ValidateRange(5, 300)][int]$DelaySeconds = 10,
    [ValidateRange(15, 600)][int]$TimeoutSeconds = 90,
    [ValidateSet('success', 'failed', 'attention', 'cancelled', 'expired', 'blocked')][string]$ExpectedStatus = 'success'
)

$ErrorActionPreference = 'Stop'
$sourceRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$candidate = (Resolve-Path -LiteralPath $CandidateRoot).ProviderPath.TrimEnd('\', '/')
$allowed = $false
foreach ($folder in @('.staging', 'artifacts')) {
    $prefix = [IO.Path]::GetFullPath((Join-Path $sourceRoot $folder)).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if ($candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { $allowed = $true }
}
if (-not $allowed -or $TimeoutSeconds -lt ($DelaySeconds + 5)) {
    throw '仅允许源码 .staging/artifacts 内候选，且超时预算必须大于计划延迟。'
}
if (-not $PythonExe) { $PythonExe = Join-Path $candidate 'runtime\python.exe' }
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw 'Python 路径不存在。' }
$pythonWorker = Join-Path (Split-Path -Parent $PythonExe) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWorker -PathType Leaf)) { $pythonWorker = $PythonExe }
$runId = [guid]::NewGuid().ToString('N')
$taskName = "Jenkins-Scheduler-Test-$runId-$($ReservationId.Substring(0, 8))"
$created = $false
$bootstrap = @'
import ipaddress, json, pathlib, sys
from datetime import timedelta

root = pathlib.Path(sys.argv[1]).resolve()
source = pathlib.Path(sys.argv[2]).resolve()
allowed = [source / '.staging', source / 'artifacts']
if not any(root != folder.resolve() and root.is_relative_to(folder.resolve()) and folder.resolve().is_relative_to(source) for folder in allowed):
    raise RuntimeError('candidate path escaped source staging')
sys.path.insert(0, str(root))
import configuration as c, engine as e, scheduler_v2 as s
from urllib.parse import urlsplit
s.runtime_ready(root)
data = root / 'data'
if not data.resolve().is_relative_to(root):
    raise RuntimeError('candidate data escaped staging')
identifier, delay, run_id, python_worker = sys.argv[3], int(sys.argv[4]), sys.argv[5], sys.argv[6]
c.validate_id(identifier)
record_path = data / 'reservations' / (identifier + '.json')
if not record_path.resolve().is_relative_to(data.resolve()):
    raise RuntimeError('reservation path escaped staging')
raw = c.read_json(record_path)
if not isinstance(raw, dict) or raw.get('schema_version') != 2 or raw.get('legacy') or raw.get('status') != 'pending':
    raise RuntimeError('only a synthetic pending v2 reservation may be tested')
record = s.adapt_record(raw)
def loopback(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or parsed.username is not None or parsed.password is not None:
        return False
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return parsed.hostname == 'localhost'
if not loopback(record['instance_snapshot']['base_url']):
    raise RuntimeError('reservation must use a loopback Jenkins')
settings = c.load_settings(data)
for instance in settings['instances']:
    if instance.get('account') and not loopback(instance['base_url']):
        raise RuntimeError('candidate contains an account for a non-loopback service')
for item in record['items']:
    if item['package_policy'] == 'zip_sha256' and not loopback(item['package_url']):
        raise RuntimeError('package must use a loopback service')
instance = s.check_snapshot(record, settings)
c.load_account(instance)
when = e.now() + timedelta(seconds=delay)
with c.planning_lock(data):
    with c.file_lock(record_path.with_suffix('.lock')):
        latest = s.adapt_record(c.read_json(record_path))
        if latest['status'] != 'pending':
            raise RuntimeError('reservation was consumed before the test')
        latest.update(scheduled_at=when.isoformat(), registration='registered', test_run_id=run_id)
        c.atomic_json(record_path, latest)
xml = e.task_xml(identifier, when, python_exe=python_worker, worker=root / 'app.py')
print(json.dumps({'xml': xml, 'scheduled_at': when.isoformat(), 'data': str(data)}, ensure_ascii=True))
'@

try {
    $preflight = & $PythonExe -E -s -c $bootstrap $candidate $sourceRoot $ReservationId $DelaySeconds $runId $pythonWorker
    if ($LASTEXITCODE -ne 0) { throw '隔离预检失败，未注册测试任务。' }
    $plan = ($preflight -join "`n") | ConvertFrom-Json
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw '唯一测试任务名已存在，拒绝覆盖。' }
    Register-ScheduledTask -TaskName $taskName -Xml $plan.xml | Out-Null
    $created = $true
    $recordPath = Join-Path (Join-Path $plan.data 'reservations') "$ReservationId.json"
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $record = $null
    $taskState = $null
    do {
        Start-Sleep -Milliseconds 500
        $record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $taskState = (Get-ScheduledTask -TaskName $taskName).State.ToString()
        if ($record.status -notin @('pending', 'running') -and $taskState -notin @('Running', 'Queued')) { break }
    } while ([DateTime]::UtcNow -lt $deadline)
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    $evidence = [ordered]@{
        run_id = $runId; reservation_id = $ReservationId; task_name = $taskName
        candidate = $candidate; data_directory = $plan.data; scheduled_at = $plan.scheduled_at
        observed_at = [DateTime]::UtcNow.ToString('o'); status = $record.status
        expected_status = $ExpectedStatus; last_task_result = $info.LastTaskResult
        task_state = $taskState
        last_run_time = $info.LastRunTime.ToUniversalTime().ToString('o')
        trigger_mode = 'one-time-planned'; python_worker = $pythonWorker
        passed = ($record.status -eq $ExpectedStatus -and $info.LastRunTime.Year -ge 2020 -and
            $taskState -notin @('Running', 'Queued') -and $info.LastTaskResult -eq 0)
    }
    $evidencePath = Join-Path $plan.data "windows-smoke-$runId.json"
    $evidence | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $evidencePath -Encoding UTF8
    [pscustomobject]$evidence | ConvertTo-Json -Depth 5
    if (-not $evidence.passed) { throw "Windows 测试未达到预期；仅保留隔离证据：$evidencePath" }
} finally {
    if ($created) {
        # Only this exact, loopback-only task is owned by the smoke run.
        $cleanupTask = Get-ScheduledTask -TaskName $taskName
        if ($cleanupTask.State.ToString() -in @('Running', 'Queued')) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
            $cleanupDeadline = [DateTime]::UtcNow.AddSeconds(5)
            do {
                Start-Sleep -Milliseconds 250
                $cleanupTask = Get-ScheduledTask -TaskName $taskName
            } while ($cleanupTask.State.ToString() -in @('Running', 'Queued') -and [DateTime]::UtcNow -lt $cleanupDeadline)
            if ($cleanupTask.State.ToString() -in @('Running', 'Queued')) {
                throw "隔离测试任务未退出，保留精确任务供核对：$taskName"
            }
        }
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
    }
}
