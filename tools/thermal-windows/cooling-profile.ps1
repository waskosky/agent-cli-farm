# Optional whole-machine policy. Always preview first; activation needs explicit
# consent beyond the farm's normal job-only resource enforcement policy.
param(
    [ValidateSet('plan', 'apply', 'restore', 'status')][string]$Action = 'plan',
    [ValidatePattern('^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')]
    [string]$OriginalScheme = '381b4222-f694-41f0-9685-ff5bb260df2e'
)
$ErrorActionPreference = 'Stop'
$powercfg = Join-Path ([Environment]::GetFolderPath('System')) 'powercfg.exe'
$original = $OriginalScheme
$cool = '0f5a6bd1-cc7e-4635-8bdd-09d0b13df1a8'
function Invoke-PowerCfg([string[]]$arguments) {
    $text = & $powercfg @arguments
    if ($LASTEXITCODE -ne 0) { throw ('powercfg failed: ' + ($arguments -join ' ')) }
    return $text
}
$activeText = Invoke-PowerCfg @('/getactivescheme')
$active = [regex]::Match(($activeText -join ' '), '[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}').Value
if (-not $active) { throw 'Could not identify the active Windows power plan.' }
function Get-Indices([string]$setting) {
    $query = (Invoke-PowerCfg @('/qh', $cool, 'SUB_PROCESSOR', $setting)) -join ' '
    # The final two hexadecimal values are AC/DC settings on every Windows
    # display language. Earlier values describe the setting's valid range.
    $values = [regex]::Matches($query, '0x([0-9a-fA-F]+)')
    if ($values.Count -lt 2) { throw 'Unexpected powercfg settings output.' }
    return @([Convert]::ToInt32($values[$values.Count - 2].Groups[1].Value, 16), [Convert]::ToInt32($values[$values.Count - 1].Groups[1].Value, 16))
}
function Test-Cooling {
    $max = Get-Indices 'PROCTHROTTLEMAX'
    $boost = Get-Indices 'PERFBOOSTMODE'
    return ($max[0] -eq 80 -and $max[1] -eq 80 -and $boost[0] -eq 0 -and $boost[1] -eq 0)
}
if ($Action -eq 'status') {
    $exists = ((Invoke-PowerCfg @('/list')) -join ' ') -match $cool
    $verified = $false
    if ($exists) { $verified = Test-Cooling }
    @{ active_guid = $active; cooling_exists = [bool]$exists; verified = [bool]$verified } | ConvertTo-Json -Compress
    exit 0
}
if ($Action -eq 'plan') {
    Write-Output ('Current plan: ' + $active)
    Write-Output ('Proposed plan: CodexFarm Cool (' + $cool + ')')
    Write-Output ('Clone original plan ' + $original + '; maximum processor performance 80%; CPU boost disabled; AC and battery.')
    Write-Output 'This affects all Windows and WSL CPU work. Other power settings remain inherited from the original plan.'
    Write-Output ('Undo: powercfg /setactive ' + $original)
    exit 0
}
if ($Action -eq 'restore') {
    if ($active -ne $cool) { Write-Output 'A different plan is active; preserving the operator choice.'; exit 0 }
    Invoke-PowerCfg @('/setactive', $original) | Write-Output
    exit 0
}
if ($active -eq $cool) {
    if (-not (Test-Cooling)) { throw 'The active cooling profile was modified; operator settings preserved.' }
    Write-Output 'CodexFarm Cool is already active with the approved settings.'
    exit 0
}
if ($active -ne $original) { throw 'Current power plan changed since preview; no settings changed' }
$list = Invoke-PowerCfg @('/list')
if (($list -join ' ') -match $cool) { throw 'Target profile already exists; inspect it before changing or activating it' }
$created = $false
try {
    $backup = Join-Path $PSScriptRoot ('power-plan-before-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.pow')
    Invoke-PowerCfg @('/export', $backup, $original) | Write-Output
    Invoke-PowerCfg @('/duplicatescheme', $original, $cool) | Write-Output
    $created = $true
    foreach ($mode in @('/setacvalueindex', '/setdcvalueindex')) {
        Invoke-PowerCfg @($mode, $cool, 'SUB_PROCESSOR', 'PROCTHROTTLEMAX', '80') | Write-Output
        Invoke-PowerCfg @($mode, $cool, 'SUB_PROCESSOR', 'PERFBOOSTMODE', '0') | Write-Output
    }
    Invoke-PowerCfg @('/changename', $cool, 'CodexFarm Cool') | Write-Output
    Invoke-PowerCfg @('/setactive', $cool) | Write-Output
    if (-not (Test-Cooling)) { throw 'Cooling settings did not verify after activation.' }
    Write-Output ('CodexFarm Cool activated. Original plan preserved: ' + $original)
} catch {
    # Only roll back the profile this invocation created. Never delete an
    # existing plan or replace a later operator selection.
    if ($created) {
        $current = (& $powercfg /getactivescheme) -join ' '
        if ($current -match $cool) { & $powercfg /setactive $original | Out-Null }
        & $powercfg /delete $cool | Out-Null
    }
    throw
}
