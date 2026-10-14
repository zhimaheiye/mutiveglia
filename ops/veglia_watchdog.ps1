<#
    veglia_watchdog.ps1 - Veglia dual-service keeper (Core 8513 + HTTP MCP 8514).

    WHY: on 2026-10-12 VegliaServer and VegliaMCP exited and stayed down ~39.5h. Both tasks
    only had a logon trigger and ops/check_veglia_tunnel.ps1 refuses to restart a dead core.
    This script owns recovery of the two local services; the hourly tunnel patrol keeps
    owning cloudflared and the public endpoint.

    RULES (do not weaken without a reason):
      - Deterministic PowerShell only. No AI in the keep-alive path.
      - Health = HTTP 200 AND body matches "ok": true on /health. A live process or a
        "Running" task state is NOT health.
      - Act only after FailStreakThreshold consecutive failed rounds (default 2 = ~10 min).
      - MaxActionsPerHour per service (default 3); counters live in the state file so every
        fresh process start does not reset the rate limit.
      - Recovery order: Core first, MCP only while Core is currently healthy.
      - Maintenance flag suppresses recovery: a deliberate stop is not an incident.
      - Alerts deduplicate per key with a cooldown; one short notice after recovery.
      - Keep this file ASCII only: Windows PowerShell 5.1 misreads non-BOM UTF-8 non-ASCII.

    USAGE:
      veglia_watchdog.ps1                one patrol round (what the 5 minute task runs)
      veglia_watchdog.ps1 -ProbeOnly     probe + log only, never act or alert
      veglia_watchdog.ps1 -Force         ignore maintenance flag, fail-streak and rate limit
      veglia_watchdog.ps1 -NoAlert       act but never send Feishu messages (for tests)
#>

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$ProbeOnly,
    [switch]$NoAlert,
    [int]$ProbeTimeoutSec = 8,
    [int]$FailStreakThreshold = 2,
    [int]$MaxActionsPerHour = 3,
    [int]$WarmupGraceSec = 120,
    [int]$RecoveryWaitSec = 60,
    [int]$AlertCooldownHours = 6,
    [int]$RotateIntervalMin = 60,
    [string]$StateFile = 'D:\veglia\logs\watchdog-state.json',
    [string]$LogFile = 'D:\veglia\logs\watchdog.log',
    [string]$AlertFile = 'D:\veglia\logs\watchdog.alert',
    [string]$MaintenanceFile = 'D:\veglia\logs\maintenance.flag',
    [string]$RotateScript = 'D:\veglia\ops\veglia_logrotate.ps1'
)

$ErrorActionPreference = 'Stop'
$Enc = New-Object System.Text.UTF8Encoding($false)
$HourMs = 3600000

$Services = @(
    [pscustomobject]@{ Key = 'core'; Label = 'Veglia Core 8513'; Task = 'VegliaServer'; Url = 'http://127.0.0.1:8513/health'; Port = 8513; DependsOn = $null },
    [pscustomobject]@{ Key = 'mcp';  Label = 'Veglia MCP 8514';  Task = 'VegliaMCP';    Url = 'http://127.0.0.1:8514/health'; Port = 8514; DependsOn = 'core' }
)

# ---------------------------------------------------------------- log
function Write-Log {
    param([string]$Tag, [string]$Message)
    $line = ('{0} | {1} | {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Tag, ($Message -replace '\s+', ' '))
    try {
        $dir = Split-Path $LogFile -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        [IO.File]::AppendAllText($LogFile, $line + "`r`n", $Enc)
    } catch { }
    try { [Console]::WriteLine($line) } catch { }
}

function Repair-LogSize {
    if (-not (Test-Path $LogFile)) { return }
    $f = Get-Item $LogFile
    if ($f.Length -le 5MB) { return }
    $tail = ''
    $fs = [IO.File]::Open($LogFile, 'Open', 'Read', 'ReadWrite')
    try {
        $fs.Seek([Math]::Max(0, $fs.Length - 2MB), 'Begin') | Out-Null
        $sr = New-Object IO.StreamReader($fs, $Enc)
        $tail = $sr.ReadToEnd(); $sr.Dispose()
    } catch { } finally { $fs.Dispose() }
    $i = $tail.IndexOf("`n")
    if ($i -ge 0) { $tail = $tail.Substring($i + 1) }
    $head = ('{0} | SELF | watchdog.log trimmed, kept newest 2 MB' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    [IO.File]::WriteAllText($LogFile, $head + "`r`n" + $tail, $Enc)
}

# ---------------------------------------------------------------- state
function Convert-ToMap {
    param($InputObject)
    if ($null -eq $InputObject) { return $null }
    if ($InputObject -is [System.Management.Automation.PSCustomObject]) {
        $h = [ordered]@{}
        foreach ($p in $InputObject.PSObject.Properties) { $h[$p.Name] = Convert-ToMap $p.Value }
        return $h
    }
    if ($InputObject -is [System.Collections.IEnumerable] -and $InputObject -isnot [string]) {
        $a = @(); foreach ($v in $InputObject) { $a += ,(Convert-ToMap $v) }
        return , $a
    }
    return $InputObject
}

function New-ServiceState {
    return [ordered]@{
        failStreak = 0; lastCode = ''; lastOk = $true
        actions = @(); recoveries = @(); totalActions = 0
        lastActionAt = ''; lastAction = ''; lastResult = ''
        alertedThis = $false; pendingRecoveryNotice = $false
    }
}

function Get-State {
    $def = [ordered]@{ version = 1; updatedAt = ''; lastRound = ''; lastRotate = 0; selfErrors = 0; services = [ordered]@{}; alerts = [ordered]@{} }
    foreach ($s in $Services) { $def.services[$s.Key] = New-ServiceState }
    if (-not (Test-Path $StateFile)) { return $def }
    try {
        $loaded = Convert-ToMap (Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json)
        if ($null -eq $loaded) { return $def }
        if (-not $loaded.Contains('services') -or $null -eq $loaded.services) { $loaded.services = [ordered]@{} }
        if (-not $loaded.Contains('alerts') -or $null -eq $loaded.alerts) { $loaded.alerts = [ordered]@{} }
        if (-not $loaded.Contains('lastRotate')) { $loaded['lastRotate'] = 0 }
        if (-not $loaded.Contains('selfErrors')) { $loaded['selfErrors'] = 0 }
        foreach ($s in $Services) {
            if (-not $loaded.services.Contains($s.Key)) { $loaded.services[$s.Key] = New-ServiceState; continue }
            $fresh = New-ServiceState
            foreach ($k in @($fresh.Keys)) { if ($loaded.services[$s.Key].Contains($k)) { $fresh[$k] = $loaded.services[$s.Key][$k] } }
            $loaded.services[$s.Key] = $fresh
        }
        return $loaded
    } catch {
        Write-Log 'STATE' ('unreadable state file, starting fresh: ' + $_.Exception.Message)
        return $def
    }
}

function Save-State {
    param($State)
    $State['updatedAt'] = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    $dir = Split-Path $StateFile -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $tmp = "$StateFile.tmp"
    [IO.File]::WriteAllText($tmp, ($State | ConvertTo-Json -Depth 8), $Enc)
    Move-Item -Path $tmp -Destination $StateFile -Force
}

# ---------------------------------------------------------------- probes
function Invoke-Probe {
    param([string]$Url, [int]$TimeoutSec = 8)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $code = '000'; $body = ''
    $tmp = [IO.Path]::GetTempFileName()
    try {
        $out = & curl.exe --noproxy '*' -s -o $tmp -w '%{http_code}' --max-time $TimeoutSec $Url
        $code = ('' + $out).Trim()
        if (Test-Path $tmp) { $body = [IO.File]::ReadAllText($tmp) }
    } catch { $code = 'ERR' } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    $sw.Stop()
    return [pscustomobject]@{ Code = $code; Ok = (($code -eq '200') -and ($body -match '"ok"\s*:\s*true')); Ms = [int]$sw.Elapsed.TotalMilliseconds }
}

function Get-ListenerInfo {
    param([int]$Port)
    $c = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { return [pscustomobject]@{ Listening = $false; Pid = 0; AgeSec = -1; Address = 'none' } }
    $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    $age = -1
    if ($p) { try { $age = [int]((Get-Date) - $p.StartTime).TotalSeconds } catch { } }
    return [pscustomobject]@{ Listening = $true; Pid = $c.OwningProcess; AgeSec = $age; Address = ($c.LocalAddress + ':' + $c.LocalPort) }
}

function Get-TaskStateName {
    param([string]$TaskName)
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { return 'absent' }
    return [string]$t.State
}

# ---------------------------------------------------------------- maintenance
function Get-Maintenance {
    if (-not (Test-Path $MaintenanceFile)) {
        return [pscustomobject]@{ Active = $false; Expired = $false; Reason = ''; Detail = '' }
    }
    $reason = ''; $expires = $null
    try {
        foreach ($l in (Get-Content $MaintenanceFile -Encoding UTF8)) {
            if ($l -match '^\s*reason\s*=\s*(.+)$') { $reason = $Matches[1].Trim() }
            if ($l -match '^\s*expires_unix\s*=\s*(\d+)$') { $expires = [int64]$Matches[1] }
        }
    } catch { }
    $now = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
    if ($null -eq $expires) { return [pscustomobject]@{ Active = $true; Expired = $false; Reason = $reason; Detail = 'open ended (no expiry)' } }
    if ($now -lt $expires) { return [pscustomobject]@{ Active = $true; Expired = $false; Reason = $reason; Detail = ([Math]::Round((($expires - $now) / 3600000.0), 1).ToString() + 'h left') } }
    return [pscustomobject]@{ Active = $false; Expired = $true; Reason = $reason; Detail = 'expired' }
}

# ---------------------------------------------------------------- alerting
$script:LarkExe = $null
foreach ($cand in @(
        $env:VEGLIA_ALERT_LARK_CLI,
        (Join-Path $env:USERPROFILE '.local\share\qoder\lark-cli\node_modules\@larksuite\cli\bin\lark-cli.exe'),
        (Join-Path $env:USERPROFILE '.qoder-cn\bin\lark-cli.exe'),
        (Join-Path $env:USERPROFILE '.qoder\bin\lark-cli.exe'))) {
    if ($cand -and (Test-Path $cand)) { $script:LarkExe = $cand; break }
}
$script:AlertOpenId = if ($env:VEGLIA_ALERT_OPEN_ID) { $env:VEGLIA_ALERT_OPEN_ID } else { 'ou_cf6ac4d6667116774208ad338ed52a50' }

function Send-Feishu {
    param([string]$Text)
    if (-not $script:LarkExe) { return [pscustomobject]@{ Ok = $false; Detail = 'lark-cli runtime not found' } }
    $env:LARKSUITE_CLI_NO_UPDATE_NOTIFIER = '1'
    $env:LARKSUITE_CLI_NO_SKILLS_NOTIFIER = '1'
    try {
        $out = & $script:LarkExe im '+messages-send' --as bot --user-id $script:AlertOpenId --text $Text --json 2>&1
        $txt = ($out | Out-String)
        if ($LASTEXITCODE -ne 0) { return [pscustomobject]@{ Ok = $false; Detail = "lark-cli exit=$LASTEXITCODE" } }
        $j = $txt | ConvertFrom-Json
        if (-not $j.ok) { return [pscustomobject]@{ Ok = $false; Detail = 'lark-cli returned ok=false' } }
        return [pscustomobject]@{ Ok = $true; Detail = ('msg=' + $j.data.message_id) }
    } catch {
        return [pscustomobject]@{ Ok = $false; Detail = ('threw: ' + $_.Exception.Message) }
    }
}

function Request-Alert {
    param([string]$Key, [string]$Body, [string]$SvcKey)
    if ($ProbeOnly) { Write-Log 'ALERT' "SUPPRESSED_PROBE_ONLY key=$Key"; return $false }
    if ($NoAlert)   { Write-Log 'ALERT' "SUPPRESSED_NOALERT key=$Key";      return $false }
    $now = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
    $cool = [int64](3600000 * $AlertCooldownHours)
    if ($script:State.alerts.Contains($Key)) {
        $prev = $script:State.alerts[$Key]
        if ($prev -and $prev.Contains('at') -and (($now - [int64]$prev['at']) -lt $cool)) {
            Write-Log 'ALERT' ("DEDUPED key=$Key since_last_min=" + [int]((($now - [int64]$prev['at']) / 60000)))
            return $false
        }
    }
    $full = "[Veglia Watchdog] $Key`r`n" + $Body
    try {
        $adir = Split-Path $AlertFile -Parent
        if (-not (Test-Path $adir)) { New-Item -ItemType Directory -Path $adir -Force | Out-Null }
        [IO.File]::WriteAllText($AlertFile, ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + " $Key`r`n" + $full + "`r`n"), $Enc)
    } catch { }
    $r = Send-Feishu -Text ($full -replace "`r`n", "`n")
    $script:State.alerts[$Key] = [ordered]@{ at = $now; sent = $r.Ok; detail = $r.Detail }
    if ($r.Ok) {
        Write-Log 'ALERT' ("SENT key=$Key $($r.Detail)")
        if ($SvcKey -and $script:State.services.Contains($SvcKey)) { $script:State.services[$SvcKey].alertedThis = $true }
        Save-State $script:State
        return $true
    }
    $script:State['selfErrors'] = [int]$script:State['selfErrors'] + 1
    Write-Log 'SELF' ("ALERT_CHANNEL_FAILED key=$Key detail=$($r.Detail) (alert file written: $AlertFile)")
    Save-State $script:State
    return $false
}

function Complete-ServiceRecoveryNotice {
    param($Svc)
    $st = $script:State.services[$Svc.Key]
    if (-not $st.alertedThis) { return }
    $li = Get-ListenerInfo -Port $Svc.Port
    $Body = ('service={0}' -f $Svc.Label)
    $Body += "`r`nstate=HEALTHY again (HTTP 200 on /health)"
    $Body += "`r`ndetected_at=" + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    $Body += "`r`nactions_last_1h=" + (@($st.actions | Where-Object { ([DateTimeOffset]::Now.ToUnixTimeMilliseconds() - [int64]$_) -lt $HourMs }).Count) + "/$MaxActionsPerHour"
    $Body += "`r`nlistener=$($li.Address)"
    $Body += "`r`nlog=$LogFile"
    [void](Request-Alert -Key ("RECOVERED:" + $Svc.Key) -Body $Body -SvcKey $Svc.Key)
    $st.alertedThis = $false
    foreach ($k in @($script:State.alerts.Keys)) { if ($k -like ("*:" + $Svc.Key)) { $script:State.alerts.Remove($k) } }
    Save-State $script:State
}

function Get-ServiceLogContext {
    param($Svc)
    $st = $script:State.services[$Svc.Key]
    $li = Get-ListenerInfo -Port $Svc.Port
    $ctx = ('service={0} task={1} state={2}' -f $Svc.Label, $Svc.Task, (Get-TaskStateName $Svc.Task))
    $ctx += "`r`ndetected_at=" + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    $ctx += "`r`nprobe=HTTP $($st.lastCode) fail_streak=$($st.failStreak) consecutive_rounds"
    $ctx += "`r`nlistener=$($li.Address) pid=$($li.Pid) pid_age_sec=$($li.AgeSec)"
    $ctx += "`r`nactions_last_1h=" + (@($st.actions | Where-Object { ([DateTimeOffset]::Now.ToUnixTimeMilliseconds() - [int64]$_) -lt $HourMs }).Count) + "/$MaxActionsPerHour total=$($st.totalActions)"
    $ctx += "`r`nlast_action=$($st.lastAction)@$($st.lastActionAt) result=$($st.lastResult)"
    $ctx += "`r`nservice_log=D:\veglia\server\logs  patrol_log=$LogFile  state=$StateFile"
    return $ctx
}

# ---------------------------------------------------------------- recovery
function Invoke-ServiceRecovery {
    param($Svc)
    $st = $script:State.services[$Svc.Key]
    $now = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
    $recent = @($st.actions | Where-Object { ($now - [int64]$_) -lt $HourMs })

    if ($recent.Count -ge $MaxActionsPerHour -and -not $Force) {
        $st.lastResult = 'RATE_LIMITED'
        Write-Log 'ACTION' ("$($Svc.Key) SKIP_RATE_LIMIT actions_1h=$($recent.Count)/$MaxActionsPerHour streak=$($st.failStreak)")
        [void](Request-Alert -Key ("LIMIT:" + $Svc.Key) -Body (Get-ServiceLogContext $Svc) -SvcKey $Svc.Key)
        return
    }
    if ($ProbeOnly) { Write-Log 'ACTION' "$($Svc.Key) SKIP_PROBE_ONLY"; return }

    $taskState = Get-TaskStateName $Svc.Task
    if ($taskState -eq 'absent') {
        $st.lastResult = 'TASK_MISSING'
        Write-Log 'ACTION' ("$($Svc.Key) task_absent=$($Svc.Task) - run ops\install_veglia_watchdog.ps1")
        [void](Request-Alert -Key ("TASKMISSING:" + $Svc.Key) -Body (Get-ServiceLogContext $Svc) -SvcKey $Svc.Key)
        return
    }
    if ($taskState -eq 'Disabled') {
        $st.lastResult = 'TASK_DISABLED'
        Write-Log 'ACTION' ("$($Svc.Key) task_disabled - refusing to enable silently")
        [void](Request-Alert -Key ("DISABLED:" + $Svc.Key) -Body (Get-ServiceLogContext $Svc) -SvcKey $Svc.Key)
        return
    }

    $li = Get-ListenerInfo -Port $Svc.Port
    if ($taskState -eq 'Running' -and $li.Listening -and $li.AgeSec -ge 0 -and $li.AgeSec -lt $WarmupGraceSec) {
        Write-Log 'ACTION' ("$($Svc.Key) WARMING_UP pid_age=$($li.AgeSec)s<${WarmupGraceSec}s - no action, not counted")
        return
    }

    $action = 'START_TASK'
    if ($taskState -eq 'Running') {
        $action = 'RESTART_TASK'
        Write-Log 'ACTION' ("$($Svc.Key) task Running but unhealthy (listener=$($li.Address) code=$($st.lastCode)) - stop then start")
        Stop-ScheduledTask -TaskName $Svc.Task -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
    }
    Start-ScheduledTask -TaskName $Svc.Task
    $recent += $now
    $st.actions = $recent
    $st.totalActions = [int]$st.totalActions + 1
    $st.lastActionAt = (Get-Date -Format 'HH:mm:ss')
    $st.lastAction = $action
    Write-Log 'ACTION' ("$($Svc.Key) $action attempt_in_1h=$($recent.Count)/$MaxActionsPerHour task_before=$taskState")
    Save-State $script:State

    $sw = [Diagnostics.Stopwatch]::StartNew()
    $p = Invoke-Probe -Url $Svc.Url -TimeoutSec $ProbeTimeoutSec
    $tries = 1
    $deadline = (Get-Date).AddSeconds($RecoveryWaitSec)
    while (-not $p.Ok -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 4; $p = Invoke-Probe -Url $Svc.Url -TimeoutSec $ProbeTimeoutSec; $tries++ }
    $sw.Stop()
    $liAfter = Get-ListenerInfo -Port $Svc.Port
    $taskNow = Get-TaskStateName $Svc.Task

    if ($p.Ok) {
        $st.lastResult = 'RECOVERED'; $st.failStreak = 0; $st.lastOk = $true; $st.lastCode = $p.Code
        $rec = @($st.recoveries | Where-Object { ($now - [int64]$_) -lt $HourMs }) + $now
        $st.recoveries = $rec
        Write-Log 'ACTION' ("$($Svc.Key) result=RECOVERED code=$($p.Code) probes=$tries wait_ms=$([int]$sw.Elapsed.TotalMilliseconds) task_now=$taskNow listener=$($liAfter.Address)")
        if ((@($rec).Count) -ge $MaxActionsPerHour) {
            [void](Request-Alert -Key ("FLAP:" + $Svc.Key) -Body ((Get-ServiceLogContext $Svc) + "`r`nnote=recovered " + @($rec).Count + " times within 1h - repeated exits, investigate the exit cause") -SvcKey $Svc.Key)
        }
    } else {
        $st.lastResult = 'RECOVERY_FAILED'
        Write-Log 'ACTION' ("$($Svc.Key) result=RECOVERY_FAILED code=$($p.Code) probes=$tries wait_ms=$([int]$sw.Elapsed.TotalMilliseconds) task_now=$taskNow listener=$($liAfter.Address)")
        [void](Request-Alert -Key ("STUCK:" + $Svc.Key) -Body (Get-ServiceLogContext $Svc) -SvcKey $Svc.Key)
    }
    Save-State $script:State
}

function Invoke-PersistentDependencyAlert {
    param($Svc)
    $st = $script:State.services[$Svc.Key]
    # core healthy but this service still failing for ~30 min of rounds despite attempts
    if ($st.lastOk) { return }
    if ($st.failStreak -lt 6) { return }
    if ([int]$st.totalActions -lt 1) { return }
    [void](Request-Alert -Key ("PERSISTENT:" + $Svc.Key) -Body ((Get-ServiceLogContext $Svc) + "`r`nnote=dependency is healthy but this service keeps failing after recovery attempts") -SvcKey $Svc.Key)
}

# ---------------------------------------------------------------- log rotation
function Invoke-RotationIfNeeded {
    $now = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
    if ((([int64]$script:State['lastRotate']) -ne 0) -and (($now - [int64]$script:State['lastRotate']) / 60000.0) -lt $RotateIntervalMin) { return }
    if (-not (Test-Path $RotateScript)) { Write-Log 'ROTATE' "SKIP script_missing=$RotateScript"; return }
    try {
        $out = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $RotateScript 2>&1
        $txt = ($out | Out-String) -replace '\s+', ' '
        Write-Log 'ROTATE' ("exit=$LASTEXITCODE out=$txt")
        $script:State['lastRotate'] = $now
    } catch {
        $script:State['selfErrors'] = [int]$script:State['selfErrors'] + 1
        Write-Log 'SELF' ("ROTATE_FAILED " + $_.Exception.Message)
        [void](Request-Alert -Key 'SELF:rotate' -Body ('rotate script threw: ' + $_.Exception.Message + "`r`nscript=$RotateScript"))
    }
}

# ---------------------------------------------------------------- main
$mutex = $null
try {
    $created = $false
    try { $mutex = New-Object System.Threading.Mutex($false, 'Global\VEGLIA_WATCHDOG', [ref]$created) }
    catch { $mutex = New-Object System.Threading.Mutex($false, 'Local\VEGLIA_WATCHDOG', [ref]$created) }
    if (-not $mutex.WaitOne(0)) { Write-Log 'ROUND' 'SKIP_ALREADY_RUNNING (another patrol holds the lock)'; exit 0 }

    Repair-LogSize
    $script:State = Get-State
    $maint = Get-Maintenance
    if ($maint.Expired) { Write-Log 'MAINT' ("FLAG_EXPIRED reason=$($maint.Reason) - normal patrol resumed") }
    $larkTag = if ($script:LarkExe) { 'ready' } else { 'MISSING' }
    Write-Log 'ROUND' ("START pid=$PID probe_only=$ProbeOnly force=$Force noalert=$NoAlert maintenance=$($maint.Active) alert_channel=$larkTag")

    # phase 1: probe everything
    foreach ($s in $Services) {
        $p = Invoke-Probe -Url $s.Url -TimeoutSec $ProbeTimeoutSec
        $st = $script:State.services[$s.Key]
        $li = Get-ListenerInfo -Port $s.Port
        $tk = Get-TaskStateName $s.Task
        if ($p.Ok) {
            if ($st.failStreak -gt 0) { Write-Log 'PROBE' ("$($s.Key) HEALTHY_AGAIN code=$($p.Code) ms=$($p.Ms) had_streak=$($st.failStreak) task=$($tk) listener=$($li.Address)") }
            else { Write-Log 'PROBE' ("$($s.Key) ok code=$($p.Code) ms=$($p.Ms) task=$($tk) listener=$($li.Address)") }
            $st.failStreak = 0; $st.lastCode = $p.Code; $st.lastOk = $true
        } else {
            $st.failStreak = [int]$st.failStreak + 1
            $st.lastCode = $p.Code; $st.lastOk = $false
            Write-Log 'PROBE' ("$($s.Key) UNHEALTHY code=$($p.Code) streak=$($st.failStreak) task=$($tk) listener=$($li.Address) pid_age=$($li.AgeSec)s")
        }
    }
    Save-State $script:State

    # phase 2: act, Core before MCP
    foreach ($s in $Services) {
        $st = $script:State.services[$s.Key]
        if ($s.DependsOn -and -not $script:State.services[$s.DependsOn].lastOk) {
            Write-Log 'ACTION' ("$($s.Key) SKIP_DEPENDENCY $($s.DependsOn) unhealthy - Core is recovered first")
            continue
        }
        if ($st.lastOk) {
            Complete-ServiceRecoveryNotice -Svc $s
            Invoke-PersistentDependencyAlert -Svc $s
            continue
        }
        if (-not $Force -and $st.failStreak -lt $FailStreakThreshold) {
            Write-Log 'ACTION' ("$($s.Key) WAIT_STREAK $($st.failStreak)/$FailStreakThreshold - no action yet")
            continue
        }
        if ($maint.Active -and -not $Force) {
            Write-Log 'ACTION' ("$($s.Key) SKIP_MAINTENANCE reason=$($maint.Reason) detail=$($maint.Detail)")
            continue
        }
        Invoke-ServiceRecovery -Svc $s
        # Core may have just come back: refresh dependents so MCP can be handled in this same round
        if ($s.Key -eq 'core' -and $script:State.services['core'].lastOk) {
            $m = $script:State.services['mcp']
            $mp = Invoke-Probe -Url $Services[1].Url -TimeoutSec $ProbeTimeoutSec
            if ($mp.Ok -and -not $m.lastOk) { Write-Log 'PROBE' ("mcp HEALTHY_AGAIN code=$($mp.Code) ms=$($mp.Ms) after core recovery") ; $m.failStreak = 0; $m.lastOk = $true; $m.lastCode = $mp.Code }
            elseif (-not $mp.Ok) { $m.lastCode = $mp.Code }
            Save-State $script:State
        }
    }

    Invoke-RotationIfNeeded
    $script:State['lastRound'] = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    Save-State $script:State
    $coreOk = $script:State.services['core'].lastOk; $mcpOk = $script:State.services['mcp'].lastOk
    Write-Log 'ROUND' ("END core=$($script:State.services['core'].lastCode)/$(if($coreOk){'ok'}else{'bad'}) mcp=$($script:State.services['mcp'].lastCode)/$(if($mcpOk){'ok'}else{'bad'})")
    exit 0
} catch {
    try {
        Write-Log 'SELF' ("ROUND_FAILED " + $_.Exception.Message)
        if ($script:State) {
            $script:State['selfErrors'] = [int]$script:State['selfErrors'] + 1
            Save-State $script:State
        }
        [void](Request-Alert -Key 'SELF:round' -Body ('watchdog round threw: ' + $_.Exception.Message + "`r`nlog=$LogFile state=$StateFile"))
    } catch { }
    exit 1
} finally {
    if ($mutex) { try { $mutex.ReleaseMutex() } catch { }; $mutex.Dispose() }
}
