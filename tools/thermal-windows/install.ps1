param([switch]$Apply)
$ErrorActionPreference = 'Stop'
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
if ($existing -and ($existing.State -eq 'Disabled')) { throw 'Sensor task is disabled by the operator; refusing to enable it' }
if ($existing) {
    if (@($existing.Actions).Count -ne 1) { throw 'Existing sensor task has unexpected actions; refusing to replace it' }
    foreach ($action in $existing.Actions) {
        if ($action.Execute -ne $powershell -or $action.Arguments -ne ('-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $root + '\sensor.ps1"')) {
            throw 'Existing sensor task has different actions; refusing to replace it'
        }
    }
}
$driverService = Get-CimInstance Win32_SystemDriver -Filter "Name='PawnIO'" -ErrorAction SilentlyContinue
if ($driverService -and $driverService.StartMode -eq 'Disabled') {
    throw 'PawnIO driver is disabled by the operator; refusing to enable it'
}
$watchdogName = 'CodexFarm Thermal Watchdog'
$watchdogArguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $root + '\sensor.ps1" -Watchdog'
$watchdog = Get-ScheduledTask -TaskName $watchdogName -ErrorAction SilentlyContinue
if ($watchdog) {
    if ($watchdog.State -eq 'Disabled') { throw 'Sensor watchdog is disabled by the operator; refusing to enable it' }
    if (@($watchdog.Actions).Count -ne 1 -or $watchdog.Actions[0].Execute -ne $powershell -or $watchdog.Actions[0].Arguments -ne $watchdogArguments) {
        throw 'Existing sensor watchdog has different actions; refusing to replace it'
    }
}
Write-Output 'Plan: install verified LibreHardwareMonitor 0.9.6 and signed PawnIO 2.2.0 if absent.'
Write-Output ('Sensor files: ' + $root + '; read CPU temperatures every 5 seconds.')
Write-Output ('Autostart: ' + $taskName + ', SYSTEM at Windows startup, including battery power.')
Write-Output 'A one-minute watchdog may recover only this owned sensor task, with a bounded cooldown; operator disables and task changes are preserved.'
Write-Output 'No fan, firmware, power-plan or workload limit changes are made by this Windows helper.'
if (-not $Apply) { exit 0 }
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator approval is required for hardware sensor access'
}
New-Item -ItemType Directory -Path $root -Force | Out-Null
& (Join-Path $system 'icacls.exe') $root '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not restrict sensor installation directory permissions' }
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
if ($existing -and $existing.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $taskName
    $stopDeadline = [DateTimeOffset]::UtcNow.AddSeconds(15)
    while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running' -and [DateTimeOffset]::UtcNow -lt $stopDeadline) {
        Start-Sleep -Milliseconds 200
    }
    if ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') { throw 'The old sensor task has not stopped; existing library files preserved' }
}

Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $root 'library') -Force
Copy-Item -LiteralPath $bridge -Destination (Join-Path $root 'sensor.ps1') -Force
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
