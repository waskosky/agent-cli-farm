param([switch]$Apply, [switch]$Uninstall, [switch]$SelfTest)
$ErrorActionPreference = 'Stop'
$taskName = 'AgentCliFarm-StorageMonitor'
$root = Join-Path $env:LOCALAPPDATA 'AgentCliFarm\storage'
$scriptPath = Join-Path $root 'monitor.ps1'
$executable = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $scriptPath + '" -Watch'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalName = $identity.Name

function Test-OwnedTask($Task) {
    if (!$Task) { return $true }
    $actions = @($Task.Actions)
    $owner = $Task.Principal.UserId
    try {
        $ownerSid = $(if ($owner -eq $identity.User.Value) { $owner } else {
            [Security.Principal.NTAccount]::new($owner).Translate([Security.Principal.SecurityIdentifier]).Value
        })
    } catch { return $false }
    return ($ownerSid -eq $identity.User.Value -and
        $Task.Principal.RunLevel -eq 'Limited' -and
        $actions.Count -eq 1 -and
        $actions[0].Execute -eq $executable -and
        $actions[0].Arguments -eq $arguments)
}

if ($SelfTest) {
    $task = [pscustomobject]@{
        Principal=[pscustomobject]@{UserId=$principalName;RunLevel='Limited'}
        Actions=@([pscustomobject]@{Execute=$executable;Arguments=$arguments})
    }
    if (!(Test-OwnedTask $task)) { throw 'Expected exact owned task' }
    $task.Principal.UserId = $env:USERNAME
    if (!(Test-OwnedTask $task)) { throw 'Scheduler-normalized owner name was rejected' }
    $task.Actions[0].Arguments = '-File unrelated.ps1'
    if (Test-OwnedTask $task) { throw 'Accepted a different action' }
    Write-Output 'Storage installer self-test passed; no task changes.'
    exit 0
}
if ($Apply -and $Uninstall) { throw 'Choose apply or uninstall' }
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (!(Test-OwnedTask $existing)) { throw 'Unexpected existing storage task; preserve it for operator review' }
if ($existing -and $existing.State -eq 'Disabled') { throw 'Storage monitor task is disabled by the operator' }
@{
    task=$taskName;principal=$principalName;script=$scriptPath
    snapshot=(Join-Path $root 'snapshot.json')
    interval_seconds=10;history_max_bytes=2097152
    action= $(if ($Uninstall) {'uninstall'} elseif ($Apply) {'install'} else {'plan'})
} | ConvertTo-Json
if (!$Apply -and !$Uninstall) { exit 0 }
if ($Uninstall) {
    if ($existing) {
        Stop-ScheduledTask -TaskName $taskName
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    exit 0
}
if ($existing) { Stop-ScheduledTask -TaskName $taskName }
[IO.Directory]::CreateDirectory($root) | Out-Null
if ((Get-Item -LiteralPath $root).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Storage install root must not be a reparse point'
}
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true,$false)
foreach ($sid in @($identity.User.Value,'S-1-5-18','S-1-5-32-544')) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new($sid),
        'FullControl','ContainerInherit,ObjectInherit','None','Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $root -AclObject $acl
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'monitor.ps1') -Destination $scriptPath -Force
& $executable -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $scriptPath
if ($LASTEXITCODE -ne 0) { throw 'Initial storage measurement failed' }
$action = New-ScheduledTaskAction -Execute $executable -Argument $arguments
$logon = New-ScheduledTaskTrigger -AtLogOn -User $principalName
$retry = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $principalName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($logon,$retry) -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
