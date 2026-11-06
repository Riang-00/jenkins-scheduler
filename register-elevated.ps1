param(
    [Parameter(Mandatory=$true)][string]$XmlPath,
    [Parameter(Mandatory=$true)][string]$TaskName,
    [switch]$AllowElevation
)
$ErrorActionPreference = 'Stop'
$phase = 'read_xml'
# Stable exit codes, no raw exceptions or XML in stdout/stderr.
function Get-RegistrationErrorCode($exception) {
    $fallback = 1
    while ($null -ne $exception) {
        $fallback = $exception.HResult
        if ($exception -is [System.ComponentModel.Win32Exception]) {
            if ($exception.NativeErrorCode -eq 1223) { return 1223 }
            if ($exception.NativeErrorCode -eq 5) { return 5 }
        }
        $hresult = '{0:X8}' -f ([int64]$exception.HResult -band 4294967295)
        switch ($hresult) {
            '80070005' { return 5 }
            '800704C7' { return 1223 }
            '80070422' { return 1062 }
            '80070426' { return 1062 }
            '800706BA' { return 1062 }
            '80041315' { return 1062 }
            '800700B7' { return 183 }
            '80070050' { return 183 }
            '80070057' { return 13 }
            '80041316' { return 13 }
            '80041317' { return 13 }
            '80041318' { return 13 }
            '80041319' { return 13 }
            '8004131A' { return 13 }
        }
        $exception = $exception.InnerException
    }
    return $fallback
}

try {
    if ($TaskName -notmatch '^Jenkins-Scheduler-[0-9a-f]{32}$') { exit 13 }
    $resolvedTaskXml = (Resolve-Path -LiteralPath $XmlPath).Path
    if ([IO.Path]::GetExtension($resolvedTaskXml) -ne '.xml') { exit 13 }
    $xmlText = [IO.File]::ReadAllText($resolvedTaskXml)
    $phase = 'connect_service'
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $phase = 'open_task_folder'
    $folder = $service.GetFolder('\')
    $phase = 'register_task'
    # TASK_CREATE (2), TASK_LOGON_INTERACTIVE_TOKEN (3). Never replace an
    # existing task; retain the original user's principal and least privilege.
    $null = $folder.RegisterTask($TaskName, $xmlText, 2, $null, $null, 3, $null)
    exit 0
} catch {
    $code = Get-RegistrationErrorCode $_.Exception
    # From the native desktop launch, opening the existing scheduler root can
    # report ERROR_PATH_NOT_FOUND instead of ACCESS_DENIED. Elevating this exact
    # root lookup recovers it. Missing XML files, connection failures and other
    # registration errors must not be mistaken for this permission condition.
    $rootAccessFailure = $phase -eq 'open_task_folder' -and $code -eq -2147024893
    if (($code -ne 5 -and -not $rootAccessFailure) -or -not $AllowElevation) { exit $code }
}

try {
    # The elevated child does not receive AllowElevation, so denial cannot
    # produce repeated prompts. Only this registration helper is elevated.
    $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                   '-File', ('"' + $PSCommandPath + '"'),
                   '-XmlPath', ('"' + $resolvedTaskXml + '"'), '-TaskName', $TaskName)
    $taskProcess = Start-Process -FilePath "$PSHOME\powershell.exe" -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru
    # Cache the handle before waiting so .NET can reliably read the exit code.
    $null = $taskProcess.Handle
    $taskProcess.WaitForExit()
    if ($null -eq $taskProcess.ExitCode) { exit 1 }
    exit $taskProcess.ExitCode
} catch {
    exit (Get-RegistrationErrorCode $_.Exception)
}
