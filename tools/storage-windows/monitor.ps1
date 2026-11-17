param(
    [switch]$Watch,
    [switch]$SelfTest,
    [string]$OutputPath = (Join-Path $env:LOCALAPPDATA 'AgentCliFarm\storage\snapshot.json'),
    [ValidateRange(2,60)][int]$IntervalSeconds = 10,
    [double]$WarningGiB = 60,
    [double]$EmergencyGiB = 10
)
$ErrorActionPreference = 'Stop'

function Publish-StorageJson([string]$Path, $Value) {
    $directory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path))
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    $temporary = Join-Path $directory ([IO.Path]::GetRandomFileName())
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 6 -Compress))
        if ($bytes.Length -gt 65536) { throw 'Storage snapshot exceeds budget' }
        $file = [IO.FileStream]::new($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $file.Write($bytes,0,$bytes.Length); $file.Flush($true) } finally { $file.Dispose() }
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary,$Path,$temporary + '.bak'); [IO.File]::Delete($temporary + '.bak') }
        else { [IO.File]::Move($temporary,$Path) }
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Get-StorageSample {
    $volumes = @{}
    foreach ($disk in [IO.DriveInfo]::GetDrives()) {
        if ($disk.IsReady -and $disk.DriveType -eq [IO.DriveType]::Fixed) {
            $key = $disk.Name.TrimEnd('\').ToUpperInvariant()
            if ($key -match '^[A-Z]:$') {
                $volumes[$key] = @{free=[long]$disk.AvailableFreeSpace;total=[long]$disk.TotalSize}
            }
        }
    }
    $distributions = @{}
    $vhdFiles = @{}
    $registry = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
    if (Test-Path -LiteralPath $registry) {
        foreach ($item in Get-ChildItem -LiteralPath $registry) {
            $entry = Get-ItemProperty -LiteralPath $item.PSPath
            if ($entry.DistributionName -and $entry.BasePath) {
                $drive = [IO.Path]::GetPathRoot($entry.BasePath).TrimEnd('\').ToUpperInvariant()
                if ($drive -match '^[A-Z]:$' -and $volumes.ContainsKey($drive)) {
                    $distributions[$entry.DistributionName] = $drive
                    $vhdName = $(if ($entry.VhdFileName) {$entry.VhdFileName} else {'ext4.vhdx'})
                    $vhdPath = Join-Path $entry.BasePath $vhdName
                    if ([IO.File]::Exists($vhdPath)) {
                        $vhdFiles[$entry.DistributionName] = ([IO.FileInfo]$vhdPath).Length
                    }
                }
            }
        }
    }
    $guardVolumes = @($env:SystemDrive.ToUpperInvariant())
    $config = Join-Path $env:USERPROFILE '.wslconfig'
    if ([IO.File]::Exists($config)) {
        $match = [regex]::Match([IO.File]::ReadAllText($config), '(?im)^\s*swapFile\s*=\s*([A-Z]):')
        if ($match.Success) { $guardVolumes += $match.Groups[1].Value.ToUpperInvariant() + ':' }
    }
    return @{
        guard_volumes=@($guardVolumes | Select-Object -Unique)
        schema='agent-farm-storage-v1'
        sampled_at=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0
        volumes=$volumes
        distributions=$distributions
        vhd_files=$vhdFiles
    }
}

function Add-StorageHistory([string]$Directory, $Sample) {
    # Bounded telemetry contains only drive letters/counters, never commands or paths.
    $path = Join-Path $Directory 'history.jsonl'
    if ([IO.File]::Exists($path) -and ([IO.FileInfo]$path).Length -gt 1MB) {
        $old = Join-Path $Directory 'history.previous.jsonl'
        if ([IO.File]::Exists($old)) { [IO.File]::Delete($old) }
        [IO.File]::Move($path,$old)
    }
    [IO.File]::AppendAllText($path, ($Sample | ConvertTo-Json -Depth 6 -Compress) + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false))
}

if ($SelfTest) {
    $directory = Join-Path ([IO.Path]::GetTempPath()) ('farm-storage-test-' + [Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    try {
        $testPath = Join-Path $directory 'snapshot.json'
        Publish-StorageJson $testPath @{schema='agent-farm-storage-v1';volumes=@{'D:'=@{free=10;total=20}}}
        Publish-StorageJson $testPath @{schema='agent-farm-storage-v1';volumes=@{'D:'=@{free=9;total=20}}}
        $read = Get-Content -Raw -LiteralPath $testPath | ConvertFrom-Json
        if ($read.volumes.'D:'.free -ne 9) { throw 'Atomic replacement failed' }
        $bytes = [IO.File]::ReadAllBytes($testPath)
        if ($bytes[0] -ne 123) { throw 'Unexpected UTF8 BOM' }
        Add-StorageHistory $directory $read
        Write-Output 'Storage monitor self-test passed; no tasks or host settings changed.'
    } finally {
        # Every deletion is within the exact directory created above.
        foreach ($file in [IO.Directory]::GetFiles($directory)) { [IO.File]::Delete($file) }
        [IO.Directory]::Delete($directory)
    }
    exit 0
}

$previous = $null
$lastHistory = 0.0
do {
    try {
        $sample = Get-StorageSample
        $sample.warning_volumes = @($sample.volumes.Keys | Where-Object {
            $sample.volumes[$_].free -lt $WarningGiB * 1GB
        })
        $sample.emergency_volumes = @($sample.volumes.Keys | Where-Object {
            $sample.volumes[$_].free -lt $EmergencyGiB * 1GB
        })
        $growth = @{}
        if ($previous) {
            $elapsed = $sample.sampled_at - $previous.sampled_at
            if ($elapsed -gt 0) {
                foreach ($key in $sample.volumes.Keys) {
                    if ($previous.volumes.ContainsKey($key)) {
                        $growth[$key] = [long][Math]::Max(0,($previous.volumes[$key].free-$sample.volumes[$key].free)/$elapsed)
                    }
                }
            }
        }
        $sample.growth_bytes_per_second = $growth
        Publish-StorageJson $OutputPath $sample
        if ($sample.sampled_at - $lastHistory -ge 60) {
            Add-StorageHistory ([IO.Path]::GetDirectoryName($OutputPath)) $sample
            $lastHistory = $sample.sampled_at
        }
        if ($sample.warning_volumes.Count) {
            Write-Warning ('Low storage headroom on ' + ($sample.warning_volumes -join ', '))
        }
        $previous = $sample
    } catch {
        # Old readings age out; never stamp stale good counters with a new time.
        Write-Warning ('Storage sample failed: ' + $_.Exception.GetType().Name)
    }
    if ($Watch) { Start-Sleep -Seconds $IntervalSeconds }
} while ($Watch)
