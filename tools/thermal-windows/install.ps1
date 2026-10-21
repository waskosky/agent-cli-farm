param([switch]$Apply, [switch]$SelfTest)
$ErrorActionPreference = 'Stop'
function Assert-TrustedAcl($acl, [bool]$parentDefaults = $false) {
    $trusted = @('S-1-5-18', 'S-1-5-32-544')
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted) {
        throw 'Existing sensor paths must be owned by Administrators or SYSTEM; existing files preserved'
    }
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        $sid = $rule.IdentityReference.Value
        # Write data/attributes, deletion, owner/DACL changes, generic write/all.
        $write = [long]$rule.FileSystemRights -band 0x500d0156L
        if (-not $write -or $sid -in $trusted) { continue }
        if ($parentDefaults -and $sid -eq 'S-1-3-0' -and
            ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) { continue }
        # Standard ProgramData child-creation rights on our parent are removed on apply.
        if ($parentDefaults -and $sid -eq 'S-1-5-32-545' -and ($write -band (-bnot 0x116L)) -eq 0) { continue }
        throw 'Existing sensor paths grant non-administrator write access; existing files preserved'
    }
}
function Assert-TrustedObject($item, $acl, [bool]$parentDefaults = $false) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Sensor installation paths cannot be reparse points' }
    Assert-TrustedAcl $acl $parentDefaults
}
function Assert-TrustedTree([string]$directory) {
    $parent = Split-Path -Parent $directory
    if (Test-Path -LiteralPath $parent) {
        Assert-TrustedObject (Get-Item -LiteralPath $parent -Force) (Get-Acl -LiteralPath $parent) $true
    }
    $pending = New-Object 'Collections.Generic.Queue[string]'
    if (Test-Path -LiteralPath $directory) { $pending.Enqueue($directory) }
    $seen = 0; $deadline = [DateTimeOffset]::UtcNow.AddSeconds(10)
    while ($pending.Count) {
        if (++$seen -gt 4096 -or [DateTimeOffset]::UtcNow -gt $deadline) { throw 'Sensor path inspection exceeded its bounded budget' }
        $path = $pending.Dequeue()
        $item = Get-Item -LiteralPath $path -Force
        Assert-TrustedObject $item (Get-Acl -LiteralPath $path)
        if ($item.PSIsContainer) {
            Get-ChildItem -LiteralPath $path -Force | ForEach-Object {
                if ($pending.Count + $seen -ge 4096 -or [DateTimeOffset]::UtcNow -gt $deadline) { throw 'Sensor path inspection exceeded its bounded budget' }
                $pending.Enqueue($_.FullName)
            }
        }
    }
}
function Assert-OwnedTask($task, [string]$expectedExecutable, [string]$expectedArguments) {
    if (-not $task) { return }
    if ($task.State -eq 'Disabled') { throw 'Sensor task is disabled by the operator; refusing to enable it' }
    if ($task.Principal.UserId -notin @('SYSTEM', 'NT AUTHORITY\SYSTEM', 'S-1-5-18') -or
        $task.Principal.LogonType -ne 'ServiceAccount' -or $task.Principal.RunLevel -ne 'Highest') {
        throw 'Existing sensor task has a different principal; refusing to replace it'
    }
    if (@($task.Actions).Count -ne 1 -or $task.Actions[0].Execute -ne $expectedExecutable -or $task.Actions[0].Arguments -ne $expectedArguments) {
        throw 'Existing sensor task has different actions; refusing to replace it'
    }
}
if ($SelfTest) {
    if ($Apply) { throw 'SelfTest cannot apply installation changes' }
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $admin = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $users = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-545')
    $acl.SetOwner($admin)
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($admin, 'FullControl', 'Allow')))
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($users, 'ReadAndExecute', 'Allow')))
    Assert-TrustedAcl $acl
    function Expect-Refusal([scriptblock]$test) {
        $refused = $false
        try { & $test } catch { $refused = $true }
        if (-not $refused) { throw 'Unsafe fixture was accepted' }
    }
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($users, 'Write', 'Allow')))
    Expect-Refusal { Assert-TrustedAcl $acl }
    Assert-TrustedAcl $acl $true
    $acl.SetOwner($users)
    Expect-Refusal { Assert-TrustedAcl $acl $true }
    $acl.SetOwner($admin)
    $acl.RemoveAccessRuleAll((New-Object Security.AccessControl.FileSystemAccessRule($users, 'Write', 'Allow')))
    Expect-Refusal { Assert-TrustedObject ([pscustomobject]@{ Attributes = [IO.FileAttributes]::ReparsePoint }) $acl }
    $task = [pscustomobject]@{ State = 'Ready'; Principal = [pscustomobject]@{ UserId = 'SYSTEM'; LogonType = 'ServiceAccount'; RunLevel = 'Highest' }; Actions = @([pscustomobject]@{ Execute = 'fixture.exe'; Arguments = 'fixture arguments' }) }
    Assert-OwnedTask $task 'fixture.exe' 'fixture arguments'
    $task.State = 'Disabled'; Expect-Refusal { Assert-OwnedTask $task 'fixture.exe' 'fixture arguments' }
    $task.State = 'Ready'; $task.Principal.UserId = 'operator'; Expect-Refusal { Assert-OwnedTask $task 'fixture.exe' 'fixture arguments' }
    $task.Principal.UserId = 'SYSTEM'; $task.Actions[0].Arguments = 'changed'; Expect-Refusal { Assert-OwnedTask $task 'fixture.exe' 'fixture arguments' }
    Write-Output 'Installer ACL, reparse-point and operator task ownership checks passed without host changes.'
    exit 0
}
$root = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'CodexFarm\Thermal'
$taskName = 'CodexFarm Thermal Sensors'
$system = [Environment]::GetFolderPath('System')
$powershell = Join-Path $system 'WindowsPowerShell\v1.0\powershell.exe'
$zipHash = '086d9f1b5a99e643edc2cfaaac16051685b551e4c5ac0b32a57c58c0e529c001'
$driverHash = '1f519a22e47187f70a1379a48ca604981c4fcf694f4e65b734aaa74a9fba3032'
$zip = Join-Path $PSScriptRoot 'LibreHardwareMonitor.zip'
$driver = Join-Path $PSScriptRoot 'PawnIO_setup.exe'
$bridge = Join-Path $PSScriptRoot 'sensor.ps1'
if ((Get-FileHash $zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $zipHash) {
    throw 'LibreHardwareMonitor release checksum mismatch'
}
if ((Get-FileHash $driver -Algorithm SHA256).Hash.ToLowerInvariant() -ne $driverHash) {
    throw 'PawnIO release checksum mismatch'
}
$signature = Get-AuthenticodeSignature $driver
if ($signature.Status -ne 'Valid') { throw 'PawnIO installer signature is not valid' }
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Assert-OwnedTask $existing $powershell ('-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $root + '\sensor.ps1"')
$driverService = Get-CimInstance Win32_SystemDriver -Filter "Name='PawnIO'" -ErrorAction SilentlyContinue
if ($driverService -and $driverService.StartMode -eq 'Disabled') {
    throw 'PawnIO driver is disabled by the operator; refusing to enable it'
}
$watchdogName = 'CodexFarm Thermal Watchdog'
$watchdogArguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $root + '\sensor.ps1" -Watchdog'
$watchdog = Get-ScheduledTask -TaskName $watchdogName -ErrorAction SilentlyContinue
Assert-OwnedTask $watchdog $powershell $watchdogArguments
Assert-TrustedTree $root
Write-Output 'Plan: install verified LibreHardwareMonitor 0.9.6 and signed PawnIO 2.2.0 if absent.'
Write-Output ('Sensor files: ' + $root + '; read CPU temperatures every 5 seconds.')
Write-Output ('Autostart: ' + $taskName + ', SYSTEM at Windows startup, including battery power.')
Write-Output 'A one-minute watchdog may recover only this owned sensor task, with a bounded cooldown; operator disables and task changes are preserved.'
Write-Output 'No fan, firmware, power-plan or workload limit changes are made by this Windows helper.'
Write-Output 'The dedicated CodexFarm parent and Thermal directory will be administrator-owned; Users receive read/execute access only.'
if (-not $Apply) { exit 0 }
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator approval is required for hardware sensor access'
}
foreach ($directory in @((Split-Path -Parent $root), $root)) {
    if (Test-Path -LiteralPath $directory) {
        Assert-TrustedObject (Get-Item -LiteralPath $directory -Force) (Get-Acl -LiteralPath $directory) ($directory -ne $root)
    } else {
        # No -Force: a raced, precreated path is not adopted implicitly.
        New-Item -ItemType Directory -Path $directory | Out-Null
    }
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($entry in @(@('S-1-5-18', 'FullControl'), @('S-1-5-32-544', 'FullControl'), @('S-1-5-32-545', 'ReadAndExecute'))) {
        $sid = New-Object Security.Principal.SecurityIdentifier($entry[0])
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid, $entry[1], 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    Set-Acl -LiteralPath $directory -AclObject $acl
}
Assert-TrustedTree $root
$maintenance = $null
$maintenanceDeadline = [DateTimeOffset]::UtcNow.AddSeconds(15)
while (-not $maintenance -and [DateTimeOffset]::UtcNow -lt $maintenanceDeadline) {
    try { $maintenance = New-Object IO.FileStream((Join-Path $root 'install.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch [IO.IOException] { Start-Sleep -Milliseconds 200 }
}
if (-not $maintenance) { throw 'Another sensor maintenance operation holds the lock; no helper files changed' }
try {
Start-Transcript -Path (Join-Path $root 'install.log') -Force | Out-Null
if (-not $driverService) {
    $installed = Start-Process -FilePath $driver -ArgumentList '-install', '-silent' -Wait -PassThru
    if ($installed.ExitCode -eq 3010) { throw 'PawnIO installation requires a Windows restart; no restart was requested' }
    if ($installed.ExitCode -ne 0) { throw ('PawnIO installation failed: ' + $installed.ExitCode) }
}
$current = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Assert-OwnedTask $current $powershell ('-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $root + '\sensor.ps1"')
if ($existing -and -not $current) { throw 'The operator removed the sensor task during installation; task deletion preserved' }
if ($current -and $current.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $taskName
    $stopDeadline = [DateTimeOffset]::UtcNow.AddSeconds(15)
    while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running' -and [DateTimeOffset]::UtcNow -lt $stopDeadline) {
        Start-Sleep -Milliseconds 200
    }
    if ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') { throw 'The old sensor task has not stopped; existing library files preserved' }
}

Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $root 'library') -Force
Copy-Item -LiteralPath $bridge -Destination (Join-Path $root 'sensor.ps1') -Force
& (Join-Path $system 'icacls.exe') $root '/setowner' '*S-1-5-32-544' '/T' '/C' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not secure installed sensor file ownership' }
Assert-TrustedTree $root
$current = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$currentWatchdog = Get-ScheduledTask -TaskName $watchdogName -ErrorAction SilentlyContinue
Assert-OwnedTask $current $powershell ('-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $root + '\sensor.ps1"')
Assert-OwnedTask $currentWatchdog $powershell $watchdogArguments
if (($existing -and -not $current) -or ($watchdog -and -not $currentWatchdog)) { throw 'The operator removed a sensor task during installation; task deletion preserved' }
if ((Get-CimInstance Win32_SystemDriver -Filter "Name='PawnIO'" -ErrorAction SilentlyContinue).StartMode -eq 'Disabled') { throw 'PawnIO was disabled by the operator; disabled state preserved' }
$action = New-ScheduledTaskAction -Execute $powershell -Argument ('-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $root + '\sensor.ps1"')
$trigger = @(New-ScheduledTaskTrigger -AtStartup)
# Re-attempt after exhausted crash retries; IgnoreNew leaves a healthy helper alone.
$trigger += New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
$watchdogAction = New-ScheduledTaskAction -Execute $powershell -Argument $watchdogArguments
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$watchdogSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 30) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $watchdogName -Action $watchdogAction -Trigger $watchdogTrigger -Settings $watchdogSettings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
$deadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
$validCount = 0
$previousStamp = 0
$startStamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
while ([DateTimeOffset]::UtcNow -lt $deadline) {
    Start-Sleep -Seconds 1
    try {
        $sample = Get-Content -LiteralPath (Join-Path $root 'temperature.json') -Raw | ConvertFrom-Json
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 - $sample.checked_at
        if ($sample.source -eq 'LibreHardwareMonitor' -and $age -ge -5 -and $age -lt 15 -and
            $sample.checked_at -ge $startStamp -and $sample.checked_at -gt $previousStamp -and
            $null -ne $sample.cpu_celsius -and $sample.cpu_celsius -gt 0 -and $sample.cpu_celsius -le 130) {
            $previousStamp = $sample.checked_at
            $validCount++
            if ($validCount -ge 2) { break }
        }
    } catch { }
}
if ($validCount -lt 2) { throw 'Sensor task did not produce consecutive fresh CPU readings; see sensor-error.txt and task status' }
$maintenance.Dispose(); $maintenance = $null
Start-ScheduledTask -TaskName $watchdogName
$watchdogDeadline = [DateTimeOffset]::UtcNow.AddSeconds(20)
$watchdogVerified = $false
while ([DateTimeOffset]::UtcNow -lt $watchdogDeadline) {
    Start-Sleep -Seconds 1
    try {
        $watchdogReceipt = Get-Content -LiteralPath (Join-Path $root 'watchdog-status.json') -Raw | ConvertFrom-Json
        $watchdogAge = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 - $watchdogReceipt.checked_at
        if ($watchdogReceipt.schema_version -eq 1 -and $watchdogReceipt.state -eq 'healthy' -and $watchdogAge -ge -5 -and $watchdogAge -le 20) {
            $watchdogVerified = $true; break
        }
    } catch { }
}
if (-not $watchdogVerified) { throw 'Sensor readings work but recovery watchdog did not verify; inspect watchdog-error.json' }
Write-Output 'Installed and verified Windows temperature telemetry and its recovery watchdog.'

Stop-Transcript | Out-Null
} finally { if ($maintenance) { $maintenance.Dispose() } }
