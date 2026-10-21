# Runs only from the administrator-owned installation directory. No farm code,
# recipes, workload controls, fan controls or power-plan changes are loaded.
param([switch]$SelfTest, [switch]$Watchdog)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$helperVersion = '3'
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Threading;
using System.Runtime.InteropServices;
public static class CodexFarmAtomicFile {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern bool MoveFileEx(string oldName, string newName, int flags);
    public static Timer ReleaseAfter(IDisposable handle, int milliseconds) {
        return new Timer(delegate(object ignored) { handle.Dispose(); }, null, milliseconds, Timeout.Infinite);
    }
}
'@
function Write-AtomicJson([string]$directory, [string]$name, [string]$json, [int]$attempts = 6) {
    $temporary = Join-Path $directory ('.' + $name + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $output = Join-Path $directory $name
    try {
        $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($json)
        $stream = New-Object IO.FileStream($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        for ($attempt = 0; $attempt -lt $attempts; $attempt++) {
            if ([CodexFarmAtomicFile]::MoveFileEx($temporary, $output, 9)) { return }
            $native = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            if ($native -notin @(5, 32, 33) -or $attempt -eq ($attempts - 1)) {
                throw (New-Object ComponentModel.Win32Exception($native))
            }
            Start-Sleep -Milliseconds ([Math]::Min(400, 50 * [Math]::Pow(2, $attempt)))
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
    }
}
function Invoke-PowerQuery([string]$arguments) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = Join-Path ([Environment]::GetFolderPath('System')) 'powercfg.exe'
    $info.Arguments = $arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $child = New-Object Diagnostics.Process
    $child.StartInfo = $info
    try {
        [void]$child.Start()
        if (-not $child.WaitForExit(2000)) {
            $child.Kill()
            throw 'Cooling status query timed out'
        }
        if ($child.ExitCode -ne 0) { throw 'Cooling status query failed' }
        return $child.StandardOutput.ReadToEnd()
    } finally { $child.Dispose() }
}
function Write-CoolingStatus {
    $stamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    $receipt = [ordered]@{ schema_version = 1; source = 'CodexFarmWindows'; checked_at = $stamp; verified = $false; active_guid = ''; state = 'unavailable' }
    try {
        $active = Invoke-PowerQuery '/getactivescheme'
        $receipt.active_guid = [regex]::Match($active, '[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}').Value
        if (-not $receipt.active_guid) { throw 'Active cooling plan could not be identified' }
        if ($receipt.active_guid -eq '0f5a6bd1-cc7e-4635-8bdd-09d0b13df1a8') {
            $values = @()
            foreach ($setting in @('PROCTHROTTLEMAX', 'PERFBOOSTMODE')) {
                $query = Invoke-PowerQuery ('/qh SCHEME_CURRENT SUB_PROCESSOR ' + $setting)
                $hex = [regex]::Matches($query, '0x([0-9a-fA-F]+)')
                if ($hex.Count -lt 2) { throw 'Cooling setting indices are unavailable' }
                $values += [Convert]::ToInt32($hex[$hex.Count - 2].Groups[1].Value, 16)
                $values += [Convert]::ToInt32($hex[$hex.Count - 1].Groups[1].Value, 16)
            }
            $receipt.verified = ($values[0] -eq 80 -and $values[1] -eq 80 -and $values[2] -eq 0 -and $values[3] -eq 0)
        }
        $receipt.state = 'checked'
    } catch { }
    try { Write-AtomicJson $root 'cooling-status.json' ($receipt | ConvertTo-Json -Compress) } catch { }
}
function Read-BoundedJson([string]$path) {
    $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
    try {
        $buffer = New-Object byte[] 65537
        $count = 0
        while ($count -lt $buffer.Length) {
            $read = $stream.Read($buffer, $count, $buffer.Length - $count)
            if ($read -eq 0) { break }
            $count += $read
        }
        if ($count -gt 65536) { throw 'Oversized thermal helper receipt' }
        return ([Text.Encoding]::UTF8.GetString($buffer, 0, $count).TrimStart([char]0xFEFF) | ConvertFrom-Json)
    } finally { $stream.Dispose() }
}
function Get-RecoveryDecision([double]$now, $sample, $previous, [bool]$enabled, [bool]$running, [double]$lastRun) {
    if (-not $enabled) { return 'disabled' }
    $stamp = 0
    if ($sample -and $sample.checked_at -is [ValueType]) { $stamp = [double]$sample.checked_at }
    if ($running -and $stamp -gt 0 -and $now - $stamp -ge -5 -and $now - $stamp -le 90) { return 'healthy' }
    if ($running -and $now - $lastRun -ge -5 -and $now - $lastRun -le 90) { return 'starting' }
    if ($previous -and $previous.last_attempt_at) {
        $delay = [Math]::Min(900, 60 * [Math]::Pow(2, [Math]::Min(4, [double]$previous.attempts - 1)))
        if ($now - [double]$previous.last_attempt_at -ge -5 -and $now - [double]$previous.last_attempt_at -lt $delay) { return 'cooldown' }
    }
    return 'recover'
}
function Write-WatchdogStatus([string]$state, $previous, [double]$now) {
    $lastAttempt = 0; $attempts = 0
    if ($previous) { $lastAttempt = $previous.last_attempt_at; $attempts = $previous.attempts }
    Write-AtomicJson $root 'watchdog-status.json' (@{ schema_version = 1; checked_at = $now; last_attempt_at = $lastAttempt; attempts = $attempts; state = $state } | ConvertTo-Json -Compress)
}
function Invoke-SensorWatchdog {
    $maintenance = $null
    try { $maintenance = New-Object IO.FileStream((Join-Path $root 'install.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch [IO.IOException] { return }
    try {
    $taskName = 'CodexFarm Thermal Sensors'
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    $scheduler = New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $task = $scheduler.GetFolder('\').GetTask($taskName)
    $sample = $null; $previous = $null
    try { $sample = Read-BoundedJson (Join-Path $root 'sensor-status.json') } catch { }
    try { $previous = Read-BoundedJson (Join-Path $root 'watchdog-status.json') } catch { }
    if (-not $task.Enabled) { Write-WatchdogStatus 'operator-disabled' $previous $now; return }
    $expectedExecutable = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
    $expectedArguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $root + '\sensor.ps1"'
    $definition = $task.Definition
    if ($definition.Actions.Count -ne 1 -or $definition.Actions.Item(1).Path -ne $expectedExecutable -or
        $definition.Actions.Item(1).Arguments -ne $expectedArguments -or
        $definition.Principal.UserId -notin @('SYSTEM', 'S-1-5-18', 'NT AUTHORITY\SYSTEM')) {
        throw 'Sensor task definition changed; operator task preserved'
    }
    $driver = Get-CimInstance Win32_SystemDriver -Filter "Name='PawnIO'" -OperationTimeoutSec 2
    if (-not $driver -or $driver.StartMode -eq 'Disabled') { Write-WatchdogStatus 'driver-unavailable-or-disabled' $previous $now; return }
    $instances = @($task.GetInstances(0))
    if ($instances.Count -gt 1) { throw 'Ambiguous sensor task instances; no recovery attempted' }
    $lastRun = ([DateTimeOffset]$task.LastRunTime).ToUnixTimeMilliseconds() / 1000.0
    $decision = Get-RecoveryDecision $now $sample $previous $task.Enabled ($instances.Count -gt 0) $lastRun
    if ($decision -ne 'recover') {
        if ($decision -eq 'healthy' -and $sample.last_success_at -and $now - [double]$sample.last_success_at -lt 30 -and $now - [double]$sample.started_at -ge 120) {
            $previous = @{ last_attempt_at = 0; attempts = 0 }
        }
        Write-WatchdogStatus $decision $previous $now
        return
    }
    # Revalidate freshness, enabled state and exact task definition immediately
    # before stopping only the held instance of this dedicated sensor task.
    try { $sample = Read-BoundedJson (Join-Path $root 'sensor-status.json') } catch { }
    $fresh = $scheduler.GetFolder('\').GetTask($taskName)
    if ($fresh.Xml -ne $task.Xml -or -not $fresh.Enabled -or
        (Get-RecoveryDecision $now $sample $previous $true ($instances.Count -gt 0) $lastRun) -ne 'recover') { return }
    $attempts = 1
    if ($previous -and $previous.attempts) { $attempts = [Math]::Min(1000, [int]$previous.attempts + 1) }
    Write-AtomicJson $root 'watchdog-status.json' (@{ schema_version = 1; checked_at = $now; last_attempt_at = $now; attempts = $attempts; state = 'recovery-requested' } | ConvertTo-Json -Compress)
    if ($instances.Count -eq 1) { $instances[0].Stop() }
    [void]$fresh.Run($null)
    } finally { if ($maintenance) { $maintenance.Dispose() } }
}
if ($Watchdog -and -not $SelfTest) {
    try { Invoke-SensorWatchdog; exit 0 } catch {
        $prior = $null
        try { $prior = Read-BoundedJson (Join-Path $root 'watchdog-status.json') } catch { }
        try { Write-WatchdogStatus 'failed' $prior ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0) } catch { }
        $failure = $_.Exception.Message
        if ($failure.Length -gt 512) { $failure = $failure.Substring(0, 512) }
        try { Write-AtomicJson $root 'watchdog-error.json' (@{ checked_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0; state = 'failed'; message = $failure } | ConvertTo-Json -Compress) } catch { }
        exit 1
    }
}
if ($SelfTest) {
    $directory = Join-Path ([IO.Path]::GetTempPath()) ('codexfarm-sensor-check-' + [Guid]::NewGuid())
    New-Item -ItemType Directory -Path $directory | Out-Null
    $blocked = $null
    $timer = $null
    try {
        Write-AtomicJson $directory 'temperature.json' '{"selftest":1}'
        Write-AtomicJson $directory 'temperature.json' '{"selftest":2}'
        $path = Join-Path $directory 'temperature.json'
        $blocked = New-Object IO.FileStream($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $denied = $false
        try { Write-AtomicJson $directory 'temperature.json' '{"selftest":3}' 1 } catch { $denied = $true }
        if (-not $denied) { throw 'A non-delete-sharing reader did not block replacement as expected' }
        $blocked.Dispose(); $blocked = $null
        if ([IO.File]::ReadAllText($path) -ne '{"selftest":2}') { throw 'Failed publication damaged the previous sample' }
        $blocked = New-Object IO.FileStream($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $timer = [CodexFarmAtomicFile]::ReleaseAfter($blocked, 150)
        Write-AtomicJson $directory 'temperature.json' '{"selftest":4}'
        $blocked = $null
        if ([IO.File]::ReadAllText($path) -ne '{"selftest":4}') { throw 'Transient publication failure did not recover' }
        if (@(Get-ChildItem -LiteralPath $directory -Filter '*.tmp' -Force).Count -ne 0) { throw 'Temporary files leaked' }
        if ((Get-RecoveryDecision 1000 @{ checked_at = 990 } $null $true $true 0) -ne 'healthy') { throw 'Fresh sensor must not restart' }
        if ((Get-RecoveryDecision 1000 @{ checked_at = 990 } $null $true $false 0) -ne 'recover') { throw 'A fresh sample must not hide a dead sensor task' }
        if ((Get-RecoveryDecision 1000 $null $null $false $false 0) -ne 'disabled') { throw 'Disabled task must not restart' }
        if ((Get-RecoveryDecision 1000 $null $null $true $true 990) -ne 'starting') { throw 'Starting task needs grace' }
        if ((Get-RecoveryDecision 1000 $null @{ last_attempt_at = 990; attempts = 3 } $true $false 0) -ne 'cooldown') { throw 'Retry cooldown missing' }
        if ((Get-RecoveryDecision 1000 $null $null $true $false 0) -ne 'recover') { throw 'Dead task needs recovery' }
        Write-Output 'Atomic replacement, denied-write preservation, transient-lock retries, cleanup and watchdog decisions passed.'

    } finally {
        if ($blocked) { $blocked.Dispose() }
        if ($timer) { $timer.Dispose() }
        Remove-Item -LiteralPath $directory -Recurse -Force
    }
    exit 0
}
$generation = [Guid]::NewGuid().ToString('N')
$started = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
$lastSuccess = $null
$lastCooling = 0
$helperHash = ''
$lastError = $null
$failures = 0
$computer = $null
$operation = 'initialize'
function Write-SensorStatus([string]$state) {
    $status = [ordered]@{
        schema_version = 1; helper_version = $helperVersion; helper_sha256 = $helperHash; generation = $generation
        started_at = $started; checked_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
        last_success_at = $lastSuccess; consecutive_failures = $failures
        state = $state; last_error = $lastError
    }
    # A locked status reader must not stop the hardware loop either.
    try { Write-AtomicJson $root 'sensor-status.json' ($status | ConvertTo-Json -Depth 4 -Compress) } catch { }
}
function Remember-Error($record, [string]$at) {
    $exception = $record.Exception
    while ($exception.InnerException) { $exception = $exception.InnerException }
    $native = $null
    if ($exception -is [ComponentModel.Win32Exception]) { $native = $exception.NativeErrorCode }
    $message = $exception.Message
    if ($message.Length -gt 512) { $message = $message.Substring(0, 512) }
    $script:lastError = [ordered]@{ checked_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0; operation = $at; error_code = $native; message = $message }
    try { [IO.File]::WriteAllText((Join-Path $root 'sensor-error.txt'), ($script:lastError | ConvertTo-Json -Compress)) } catch { }
}
$singleWriter = $null
try {
    $operation = 'single-writer lock'
    try { $singleWriter = New-Object IO.FileStream((Join-Path $root 'sensor.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch [IO.IOException] { exit 0 }
    $helperHash = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $operation = 'load sensor library'
    Add-Type -Path (Join-Path $root 'library\LibreHardwareMonitorLib.dll')
    while ($true) {
        $cpu = $null
        $sensorOk = $false
        try {
            if (-not $computer) {
                $operation = 'open hardware sensors'
                $computer = New-Object LibreHardwareMonitor.Hardware.Computer
                $computer.IsCpuEnabled = $true
                $computer.Open()
            }
            $operation = 'read hardware sensors'
            $temperatures = @()
            foreach ($hardware in $computer.Hardware) {
                if ($hardware.HardwareType.ToString() -ne 'Cpu') { continue }
                $hardware.Update()
                foreach ($sensor in $hardware.Sensors) {
                    if ($sensor.SensorType.ToString() -ne 'Temperature' -or $null -eq $sensor.Value) { continue }
                    $value = [double]$sensor.Value
                    if (-not [double]::IsNaN($value) -and $value -gt 0 -and $value -le 130) { $temperatures += $value }
                }
            }
            if ($temperatures.Count -eq 0) { throw 'No usable CPU temperature sensors' }
            $cpu = ($temperatures | Measure-Object -Maximum).Maximum
            $sensorOk = $true
        } catch {
            Remember-Error $_ $operation
            if ($computer) { try { $computer.Close() } catch { }; $computer = $null }
        }
        try {
            $operation = 'publish temperature sample'
            $stamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
            $sample = [ordered]@{ source = 'LibreHardwareMonitor'; checked_at = $stamp; cpu_celsius = $cpu; helper_version = $helperVersion; helper_sha256 = $helperHash; generation = $generation }
            Write-AtomicJson $root 'temperature.json' ($sample | ConvertTo-Json -Compress)
            if ($sensorOk) {
                $lastSuccess = $stamp; $failures = 0
                Write-SensorStatus 'healthy'
            } else { $failures++; Write-SensorStatus 'unavailable' }
        } catch {
            $failures++
            Remember-Error $_ $operation
            Write-SensorStatus 'degraded'
        }
        $coolingNow = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
        if ($coolingNow - $lastCooling -ge 60 -or $coolingNow -lt $lastCooling) {
            Write-CoolingStatus
            $lastCooling = $coolingNow
        }
        # Persistent failures remain observable without busy-looping. Each retry
        # takes a new reading; an old value is never given a new timestamp.
        Start-Sleep -Seconds ([Math]::Min(60, 5 * [Math]::Max(1, $failures)))
    }
} catch {
    Remember-Error $_ $operation
    Write-SensorStatus 'failed'
    exit 1
} finally {
    if ($computer) { try { $computer.Close() } catch { } }
    if ($singleWriter) { $singleWriter.Dispose() }
}
