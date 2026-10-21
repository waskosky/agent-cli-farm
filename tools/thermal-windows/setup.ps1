# One-time, explicitly opted-in setup from WSL. Hidden elevation prevents the
# console's text-selection mode from suspending installation halfway through.
param(
    [switch]$InstallSensors,
    [switch]$Cooling,
    [switch]$Elevated,
    [ValidatePattern('^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')]
    [string]$OriginalScheme = '381b4222-f694-41f0-9685-ff5bb260df2e'
)
$ErrorActionPreference = 'Stop'
if (-not $InstallSensors -and -not $Cooling) { throw 'Choose -InstallSensors and/or the separately consented -Cooling option.' }
$resultPath = Join-Path $PSScriptRoot 'setup-result.json'
if (-not $Elevated) {
    if (Test-Path -LiteralPath $resultPath) { throw 'This staging directory was already used. Review its result before retrying in a new directory.' }
    $script = $PSCommandPath.Replace("'", "''")
    $code = "& '$script' -Elevated -OriginalScheme '$OriginalScheme'"
    if ($InstallSensors) { $code += ' -InstallSensors' }
    if ($Cooling) { $code += ' -Cooling' }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
    $powershell = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
    $process = Start-Process -FilePath $powershell -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ArgumentList ('-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ' + $encoded)
    if (-not (Test-Path -LiteralPath $resultPath)) { throw ('Windows setup produced no result; exit code ' + $process.ExitCode) }
    $result = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
    if ($process.ExitCode -ne 0 -or -not $result.success) { throw ('Windows setup failed: ' + $result.error) }
    Write-Output 'The requested Windows safeguards were installed and verified.'
    exit 0
}
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Windows administrator approval is required.' }
$log = Join-Path $PSScriptRoot 'setup-apply.log'
try {
    if ($Cooling) {
        $before = & (Join-Path $PSScriptRoot 'cooling-profile.ps1') -Action status | ConvertFrom-Json
        if ($before.active_guid -ne $OriginalScheme) { throw 'The active power plan changed since preview; no installation attempted.' }
    }
    if ($InstallSensors) { & (Join-Path $PSScriptRoot 'install.ps1') -Apply *>> $log }
    if ($Cooling) { & (Join-Path $PSScriptRoot 'cooling-profile.ps1') -Action apply -OriginalScheme $OriginalScheme *>> $log }
    @{ success = $true; checked_at = [DateTimeOffset]::UtcNow.ToString('o'); sensors = [bool]$InstallSensors; cooling = [bool]$Cooling; original_power_scheme = $OriginalScheme } | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
} catch {
    @{ success = $false; checked_at = [DateTimeOffset]::UtcNow.ToString('o'); error = $_.Exception.Message } | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
    exit 1
}
