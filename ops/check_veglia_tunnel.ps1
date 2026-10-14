<#
    check_veglia_tunnel.ps1 - one round of Veglia PUBLIC-path health patrol (hourly).

    Scheduled by task "VegliaTunnelHourlyCheck" (hourly). One invocation = one round, then exit.
    Order of checks: Veglia Core 8513 -> HTTP MCP 8514 -> cloudflared service -> public /health.

    OWNERSHIP (since the 2026-10-14 keep-alive work):
      * VegliaWatchdog (task "VegliaWatchdog", every 5 min) owns 8513 / 8514 recovery.
      * This hourly patrol owns the cloudflared tunnel and the public HTTPS path only.
      Reason: the 2026-10-12 outage lasted ~39.5h partly because 8513 death was reported by an
      hourly patrol that deliberately refused to restart it, while nothing else would. Two
      schedulers restarting the same Windows task inside the same minute is a worse failure mode
      than one clear owner, so the 8514 restart that used to live in CASE 4 was handed over.

    Self-heal policy (deliberately narrow, "check first, act only when broken"):
      maintenance flag on -> observe and log only, take NO action (deliberate downtime window).
      Core 8513 down      -> record + alert only (VegliaWatchdog recovers it).
      MCP 8514 down       -> record + alert only (VegliaWatchdog recovers it).
      public down         -> cloudflared stopped: Start-Service once.
                             cloudflared running: Restart-Service once (2h cooldown guard).
      Never more than one service action per kind per round, no retry loops.

    All requests to loopback use --noproxy so Clash/system proxy settings cannot skew the probe.
    Log: D:\veglia\logs\tunnel-watch.log (gitignored), trimmed to the newest 2 MB past 5 MB.
    Keep this file ASCII-only: Windows PowerShell 5.1 misreads non-BOM UTF-8 non-ASCII text.
#>

$ErrorActionPreference = 'SilentlyContinue'

$CoreUrl    = 'http://127.0.0.1:8513/health'
$McpUrl     = 'http://127.0.0.1:8514/health'
$PublicUrl  = 'https://mcp.zhimaheiye.dpdns.org/health'
$LogFile    = 'D:\veglia\logs\tunnel-watch.log'
$AlertFile  = 'D:\veglia\logs\tunnel-watch.alert'
$MaintenanceFile = 'D:\veglia\logs\maintenance.flag'
$ServiceName = 'cloudflared'
$McpTaskName = 'VegliaMCP'
$ServerTask  = 'VegliaServer'
$MaxLogBytes = 5MB
$KeepBytes   = 2MB
$Enc = New-Object System.Text.UTF8Encoding($false)

function Invoke-HttpProbe {
    param([string]$Url, [int]$TimeoutSec = 12)
    $tmp = [IO.Path]::GetTempFileName()
    $code = '000'
    $body = ''
    try {
        $out = & curl.exe --noproxy '*' -s -o $tmp -w '%{http_code}' --max-time $TimeoutSec $Url
        $code = ('' + $out).Trim()
        if (Test-Path $tmp) { $body = [IO.File]::ReadAllText($tmp) }
    } catch { } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    $brief = ($body -replace '[^\x20-\x7E]', '?') -replace '\s+', ' '
    if ($brief.Length -gt 80) { $brief = $brief.Substring(0, 80) }
    [pscustomobject]@{ Code = $code; Ok = ($code -eq '200'); Body = $brief }
}

# A deliberate maintenance window must never be treated as a fault by any patrol.
# Written by ops\veglia_maintenance.ps1 -On and cleared by -Off (or when it expires).
# NOTE expires_unix is in MILLISECONDS - same unit the watchdog compares against. A unit
# mismatch here would make an expired flag look active forever and silence recovery.
function Test-MaintenanceActive {
    if (-not (Test-Path $MaintenanceFile)) { return $false }
    $txt = [IO.File]::ReadAllText($MaintenanceFile)
    if ($txt -notmatch '(?m)^state=on') { return $false }
    $exp = [int64]0
    if ($txt -match '(?m)^expires_unix=(\d+)') { $exp = [int64]$Matches[1] }
    if ($exp -le 0) { return $true }
    $now = [int64]([DateTimeOffset]::Now.ToUnixTimeMilliseconds())
    return ($now -lt $exp)
}

function Get-CfServiceState {
    $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($s) { return $s.Status.ToString() } else { return 'Missing' }
}

# cloudflared exposes /ready on its metrics port (20241 by default, port may drift):
# 200 = connector registered with the Cloudflare edge, 503 = no usable edge connection.
function Get-ConnectorReady {
    $p = Get-Process -Name $ServiceName -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $p) { return 'n/a' }
    $l = Get-NetTCPConnection -State Listen -OwningProcess $p.Id -ErrorAction SilentlyContinue |
         Where-Object { $_.LocalAddress -like '127.*' } | Select-Object -First 1
    if (-not $l) { return 'n/a' }
    try {
        $r = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/ready" -f $l.LocalPort) -UseBasicParsing -TimeoutSec 5 -Proxy $null
        return [string]$r.StatusCode
    } catch {
        if ($_.Exception.Response) { return [string][int]$_.Exception.Response.StatusCode }
        return 'n/a'
    }
}

function Wait-PublicHealthy {
    param([int]$MaxSec = 25)
    $deadline = (Get-Date).AddSeconds($MaxSec)
    $p = Invoke-HttpProbe -Url $PublicUrl -TimeoutSec 10
    while (-not $p.Ok -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 4
        $p = Invoke-HttpProbe -Url $PublicUrl -TimeoutSec 10
    }
    return $p
}

function Repair-LogSize {
    if (-not (Test-Path $LogFile)) { return }
    $f = Get-Item $LogFile
    if ($f.Length -le $MaxLogBytes) { return }
    $tail = ''
    $fs = [IO.File]::Open($LogFile, 'Open', 'Read', 'ReadWrite')
    try {
        $fs.Seek([Math]::Max(0, $fs.Length - $KeepBytes), 'Begin') | Out-Null
        $sr = New-Object IO.StreamReader($fs, $Enc)
        $tail = $sr.ReadToEnd()
        $sr.Dispose()
    } catch { } finally { $fs.Dispose() }
    $i = $tail.IndexOf("`n")
    if ($i -ge 0) { $tail = $tail.Substring($i + 1) }
    $head = ("[{0}] log rotated, kept newest {1} KB`r`n" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), [int]($KeepBytes/1KB))
    [IO.File]::WriteAllText($LogFile, $head + $tail, $Enc)
}

$Enc = New-Object System.Text.UTF8Encoding($false)
$dir = Split-Path $LogFile -Parent
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
Repair-LogSize

# ---------- probe ----------
$core    = Invoke-HttpProbe -Url $CoreUrl
$mcp     = Invoke-HttpProbe -Url $McpUrl
$svc     = Get-CfServiceState
$public  = Invoke-HttpProbe -Url $PublicUrl
$publicInitial = $public.Code
$maint   = Test-MaintenanceActive

$action = 'NONE'
$result = 'HEALTHY'
$publicAfter = ''
$note = ''

if ($maint) {
    # Maintenance window: origin being down is intentional. Observe, log, act on nothing.
    $action = 'NONE_MAINTENANCE'
    $result = 'MAINTENANCE'
    $note = "core=$($core.Code);mcp=$($mcp.Code);cloudflared=$svc;public=$($public.Code)"
}
elseif (-not $core.Ok) {
    # CASE 5: origin core down -> record and alert; VegliaWatchdog owns the restart.
    $action = 'NONE_WATCHDOG_OWNED'
    $result = 'CORE_8513_DOWN'
    $st = (Get-ScheduledTask -TaskName $ServerTask -ErrorAction SilentlyContinue)
    $stInfo = if ($st) { (Get-ScheduledTaskInfo -InputObject $st).State } else { 'absent' }
    $note = "VegliaServer_task=$stInfo;mcp=$($mcp.Code);public=$($public.Code);handled_by=VegliaWatchdog"
}
elseif (-not $mcp.Ok) {
    # CASE 4: MCP listener down while core is fine -> record and alert; VegliaWatchdog owns it.
    $action = 'NONE_WATCHDOG_OWNED'
    $result = 'MCP_8514_DOWN'
    $st = (Get-ScheduledTask -TaskName $McpTaskName -ErrorAction SilentlyContinue)
    $stInfo = if ($st) { (Get-ScheduledTaskInfo -InputObject $st).State } else { 'absent' }
    $note = "VegliaMCP_task=$stInfo;public=$($public.Code);handled_by=VegliaWatchdog"
}
elseif (-not $public.Ok) {
    # CASE 2 / 3: origin healthy, tunnel not serving. This patrol still owns cloudflared.
    $connBefore = Get-ConnectorReady
    # Cooldown: if a previous round already found this tunnel unrecoverable, a service
    # restart is not the fix (edge path is blocked) - do not restart it every hour.
    # Cooldown applies only to restarting an already-running connector (that pattern means the
    # edge path itself is blocked, which a restart cannot fix). Starting a stopped service is
    # always allowed.
    $inCooldown = $false
    if ($svc -eq 'Running' -and (Test-Path $AlertFile)) {
        $al = [IO.File]::ReadAllText($AlertFile)
        if ($al -match 'UNRECOVERED_PUBLIC_TUNNEL' -and ((Get-Date) - (Get-Item $AlertFile).LastWriteTime).TotalHours -lt 2) {
            $inCooldown = $true
        }
    }
    if ($inCooldown) {
        $action = 'NONE_COOLDOWN'
        $note = "cooldown<2h_since_last_UNRECOVERED;connector_before=$connBefore;public_body=$($public.Body)"
        $result = 'UNRECOVERED_PUBLIC_TUNNEL'
    } else {
        if ($svc -ne 'Running') {
            $action = 'START_CLOUDFLARED'
            Start-Service -Name $ServiceName
        } else {
            $action = 'RESTART_CLOUDFLARED'
            Restart-Service -Name $ServiceName
        }
        $public = Wait-PublicHealthy -MaxSec 30
        $connAfter = Get-ConnectorReady
        $publicAfter = $public.Code
        $note = "connector_before=$connBefore;connector_after=$connAfter;public_body=$($public.Body)"
        if ($public.Ok) { $result = 'RECOVERED_CLOUDFLARED' } else { $result = 'UNRECOVERED_PUBLIC_TUNNEL' }
    }
}

# ---------- alert flag ----------
if ($result -in @('HEALTHY', 'RECOVERED_CLOUDFLARED', 'MAINTENANCE')) {
    Remove-Item $AlertFile -Force -ErrorAction SilentlyContinue
} else {
    ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $result) | Out-File -FilePath $AlertFile -Encoding ascii
}

# ---------- one block per round ----------
$block = @(
    "[{0}]" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    "8513=$($core.Code)"
    "8514=$($mcp.Code)"
    "cloudflared=$svc"
    "public=$publicInitial"
    "action=$action"
)
if ($maint) { $block += "maintenance=on" }
if ($publicAfter) { $block += "public_after=$publicAfter" }
if ($note) { $block += "note=$note" }
$block += ''
[IO.File]::AppendAllText($LogFile, (($block -join "`r`n") + "`r`n"), $Enc)
Write-Output ($block -join ' ')
exit 0
