<#
    veglia_maintenance.ps1 - tell the Veglia watchdog that a stop is deliberate.

    WHY
      Without this, ops\stop_server.bat or a manual task stop looks like an incident and the
      5 minute watchdog immediately starts everything back up. The watchdog reads the flag
      file and records SKIP_MAINTENANCE instead of acting.

    USAGE
      veglia_maintenance.ps1 -On -Reason "upgrade sensor" [-Hours 8]   enter maintenance
      veglia_maintenance.ps1 -Off                                     clear flag + start tasks
      veglia_maintenance.ps1 -Off -NoStart                              clear flag only
      veglia_maintenance.ps1 -Status                                    show flag + health
      Flag file: D:\veglia\logs\maintenance.flag  (ASCII key=value, gitignored)
      Expired flags are ignored by the watchdog, so an abandoned flag cannot suppress
      recovery forever. Default expiry is 8 hours.
#>

[CmdletBinding()]
param(
    [switch]$On,
    [switch]$Off,
    [switch]$Status,
    [string]$Reason = 'manual maintenance',
    [double]$Hours = 8,
    [switch]$NoStart,
    [string]$MaintenanceFile = 'D:\veglia\logs\maintenance.flag',
    [int]$HealthWaitSec = 60
)

$ErrorActionPreference = 'Stop'
$Enc = New-Object System.Text.UTF8Encoding($false)
$Services = @(
    [pscustomobject]@{ Key = 'core'; Task = 'VegliaServer'; Url = 'http://127.0.0.1:8513/health' },
    [pscustomobject]@{ Key = 'mcp';  Task = 'VegliaMCP';    Url = 'http://127.0.0.1:8514/health' }
)

function Get-Health {
    param([string]$Url)
    $tmp = [IO.Path]::GetTempFileName()
    try {
        $code = ('' + (& curl.exe --noproxy '*' -s -o $tmp -w '%{http_code}' --max-time 8 $Url)).Trim()
        $body = ''
        if (Test-Path $tmp) { $body = [IO.File]::ReadAllText($tmp) }
        return [pscustomobject]@{ Code = $code; Ok = (($code -eq '200') -and ($body -match '"ok"\s*:\s*true')) }
    } catch {
        return [pscustomobject]@{ Code = 'ERR'; Ok = $false }
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Show-Flag {
    if (-not (Test-Path $MaintenanceFile)) { Write-Output 'MAINTENANCE off'; return }
    Write-Output 'MAINTENANCE on:'
    Get-Content $MaintenanceFile | ForEach-Object { Write-Output ('  ' + $_) }
}

$mode = if ($On) { 'on' } elseif ($Off) { 'off' } else { 'status' }
if ($Status) { $mode = 'status' }
if (-not $On -and -not $Off -and -not $Status) {
    Write-Output 'usage: -On [-Reason text] [-Hours 8] | -Off [-NoStart] | -Status'
    Show-Flag
    exit 0
}

if ($mode -eq 'on') {
    $now = [DateTimeOffset]::Now
    $lines = @(
        ('state=on'),
        ('reason=' + ($Reason -replace '[\r\n]', ' ')),
        ('since_unix=' + $now.ToUnixTimeMilliseconds()),
        ('expires_unix=' + $now.AddHours($Hours).ToUnixTimeMilliseconds()),
        ('expires_at=' + $now.AddHours($Hours).LocalDateTime.ToString('yyyy-MM-dd HH:mm:ss')),
        ('requested_by=' + [Security.Principal.WindowsIdentity]::GetCurrent().Name),
        ('note=watchdog records SKIP_MAINTENANCE while this flag is unexpired')
    )
    $adir = Split-Path $MaintenanceFile -Parent
    if (-not (Test-Path $adir)) { New-Item -ItemType Directory -Path $adir -Force | Out-Null }
    [IO.File]::WriteAllText($MaintenanceFile, (($lines -join "`r`n") + "`r`n"), $Enc)
    Write-Output ('MAINTENANCE on reason=' + $Reason + ' expires_in_h=' + $Hours)
    Write-Output ('flag=' + $MaintenanceFile)
    exit 0
}

if ($mode -eq 'off') {
    if (Test-Path $MaintenanceFile) { Remove-Item $MaintenanceFile -Force; Write-Output 'MAINTENANCE off (flag removed)' }
    else { Write-Output 'MAINTENANCE off (no flag was present)' }
    if ($NoStart) { exit 0 }
    foreach ($s in $Services) {
        $h = Get-Health $s.Url
        if ($h.Ok) { Write-Output ("$($s.Key) already healthy HTTP=$($h.Code)"); continue }
        Write-Output ("$($s.Key) starting task=$($s.Task)")
        Start-ScheduledTask -TaskName $s.Task -ErrorAction SilentlyContinue
        $deadline = (Get-Date).AddSeconds($HealthWaitSec)
        $h = Get-Health $s.Url
        while (-not $h.Ok -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 3; $h = Get-Health $s.Url }
        Write-Output ("$($s.Key) HTTP=$($h.Code) ok=$($h.Ok)")
    }
    exit 0
}

Show-Flag
foreach ($s in $Services) {
    $h = Get-Health $s.Url
    $tsk = (Get-ScheduledTask -TaskName $s.Task -ErrorAction SilentlyContinue)
    $st = if ($tsk) { [string]$tsk.State } else { 'absent' }
    Write-Output ("$($s.Key) task=$st http=$($h.Code) ok=$($h.Ok)")
}
exit 0
