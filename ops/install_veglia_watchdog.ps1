<#
    install_veglia_watchdog.ps1 - register the Veglia keep-alive layer on this machine.

    WHAT IT CHANGES (all reversible, backups in D:\veglia\ops\backup)
      1. VegliaServer / VegliaMCP tasks
         - add a BootTrigger (they used to have only a LogonTrigger, so after a process exit
           nothing ever started them again until the next interactive logon)
         - keep the existing LogonTrigger, principal, action, working directory and
           MultipleInstances=IgnoreNew
         - IdleSettings/StopOnIdleEnd -> false (never let the scheduler stop a server task
           because the desktop went idle / came back from idle)
         - NO periodic trigger on these two: a raw time trigger would restart services
           during intentional maintenance, which only the watchdog knows how to respect.
      2. VegliaWatchdog task (new): powershell.exe -File <repo>\ops\veglia_watchdog.ps1
         every 5 minutes + BootTrigger, IgnoreNew, 10 min execution cap, same user as the
         service tasks but HighestAvailable (needs it for the Global\ named mutex and
         wevtutil); it must run as the interactive account because the Feishu alert path
         reads that account's lark-cli config.
      3. Enables the Microsoft-Windows-TaskScheduler/Operational event log so the next
         unexpected task exit can be attributed instead of guessed at.

    USAGE
      powershell -File install_veglia_watchdog.ps1            apply
      powershell -File install_veglia_watchdog.ps1 -Verify     report only, change nothing
      powershell -File install_veglia_watchdog.ps1 -Rollback   re-register from ops\backup XML
    Run elevated. Idempotent: safe to run again after pulling new code.
#>

[CmdletBinding()]
param(
    [switch]$Verify,
    [switch]$Rollback,
    [int]$WatchdogIntervalMin = 5,
    [string]$DeploymentRoot = 'D:\veglia'
)

$ErrorActionPreference = 'Stop'
$nsUri = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
$OpsDir = Join-Path $DeploymentRoot 'ops'
$BackupDir = Join-Path $OpsDir 'backup'
$WatchdogPs1 = Join-Path $OpsDir 'veglia_watchdog.ps1'
$RotatePs1 = Join-Path $OpsDir 'veglia_logrotate.ps1'
$MaintPs1 = Join-Path $OpsDir 'veglia_maintenance.ps1'
$TaskNames = 'VegliaServer', 'VegliaMCP'

function Get-Ns {
    param($Doc)
    $m = New-Object System.Xml.XmlNamespaceManager($Doc.NameTable)
    $m.AddNamespace('t', $nsUri)
    return $m
}

function Read-TaskXml {
    param([string]$TaskName)
    $raw = Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $d = New-Object System.Xml.XmlDocument
    # LoadXml returns the root element on .NET Framework: swallow it, otherwise the function
    # emits [XmlElement, XmlDocument] and the caller's $Doc.SelectSingleNode binding fails.
    [void]$d.LoadXml($raw)
    return $d
}

function Register-Xml {
    param([string]$TaskName, $Doc)
    $xml = $Doc.OuterXml
    try {
        return (Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force -ErrorAction Stop)
    } catch {
        # Task Scheduler occasionally rejects the exported UTF-16 declaration
        $xml2 = $xml -replace 'encoding="UTF-16"', 'encoding="UTF-8"'
        return (Register-ScheduledTask -TaskName $TaskName -Xml $xml2 -Force -ErrorAction Stop)
    }
}

function Ensure-BootTrigger {
    param($Doc, [string]$TaskName)
    $m = Get-Ns $Doc
    $tr = $Doc.SelectSingleNode('/t:Task/t:Triggers', $m)
    if (-not $tr) { $tr = $Doc.CreateElement('Triggers', $nsUri); [void]$Doc.DocumentElement.AppendChild($tr) }
    $existing = $tr.SelectSingleNode('t:BootTrigger', $m)
    # [Console]::WriteLine, not Write-Output: these helpers return a boolean that the caller
    # combines with -or, and pipeline text would turn it into an array.
    if ($existing) { [Console]::WriteLine("  $TaskName BootTrigger already present"); return $false }
    $bt = $Doc.CreateElement('BootTrigger', $nsUri)
    $en = $Doc.CreateElement('Enabled', $nsUri); $en.InnerText = 'true'; [void]$bt.AppendChild($en)
    [void]$tr.AppendChild($bt)
    [Console]::WriteLine("  $TaskName + BootTrigger")
    return $true
}

function Disable-StopOnIdleEnd {
    param($Doc, [string]$TaskName)
    $m = Get-Ns $Doc
    $node = $Doc.SelectSingleNode('/t:Task/t:Settings/t:IdleSettings/t:StopOnIdleEnd', $m)
    if (-not $node) {
        $idle = $Doc.SelectSingleNode('/t:Task/t:Settings/t:IdleSettings', $m)
        if (-not $idle) { return $false }
        $node = $Doc.CreateElement('StopOnIdleEnd', $nsUri); [void]$idle.AppendChild($node)
    }
    if ($node.InnerText -eq 'false') { return $false }
    $node.InnerText = 'false'
    [Console]::WriteLine("  $TaskName IdleSettings/StopOnIdleEnd -> false")
    return $true
}

function Set-NodeText {
    param($Doc, [string]$Path, [string]$Value)
    $m = Get-Ns $Doc
    $n = $Doc.SelectSingleNode($Path, $m)
    if (-not $n) { return $false }
    if ($n.InnerText -eq $Value) { return $false }
    $n.InnerText = $Value
    return $true
}

function Get-Health {
    param([string]$Url)
    $tmp = [IO.Path]::GetTempFileName()
    try {
        $code = ('' + (& curl.exe --noproxy '*' -s -o $tmp -w '%{http_code}' --max-time 8 $Url)).Trim()
        $body = if (Test-Path $tmp) { [IO.File]::ReadAllText($tmp) } else { '' }
        return (($code -eq '200') -and ($body -match '"ok"\s*:\s*true'))
    } catch { return $false } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}

function Show-State {
    Write-Output '--- current task definitions ---'
    foreach ($n in ($TaskNames + 'VegliaWatchdog')) {
        $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        if (-not $t) { Write-Output ("$n absent"); continue }
        $trig = ($t.Triggers | ForEach-Object { $_.CimClass.CimClassName -replace 'MSFT_Task', '' -replace 'Trigger', '' }) -join ','
        $info = Get-ScheduledTaskInfo -TaskName $n
        Write-Output ("{0} state={1} enabled={2} triggers={3} multi={4} timeLimit={5} stopOnIdleEnd={6} last={7} result={8}" -f `
                $n, $t.State, $t.Settings.Enabled, $trig, $t.Settings.MultipleInstances, $t.Settings.ExecutionTimeLimit, `
                $t.Settings.IdleSettings.StopOnIdleEnd, $info.LastRunTime, $info.LastTaskResult)
    }
    $log = Get-WinEvent -ListLog 'Microsoft-Windows-TaskScheduler/Operational' -ErrorAction SilentlyContinue
    Write-Output ('TaskScheduler operational log enabled=' + $(if ($log) { $log.IsEnabled } else { 'unknown' }))
    Write-Output ('health core=' + (Get-Health 'http://127.0.0.1:8513/health') + ' mcp=' + (Get-Health 'http://127.0.0.1:8514/health'))
}

Write-Output ('install_veglia_watchdog  root=' + $DeploymentRoot + '  watchdog=' + $WatchdogPs1)
if ($Verify) { Show-State; exit 0 }

if ($Rollback) {
    foreach ($n in ($TaskNames + 'VegliaWatchdog')) {
        $f = Join-Path $BackupDir ($n + '.task.xml')
        if (Test-Path $f) {
            $d = New-Object System.Xml.XmlDocument
            $d.Load((Get-Item $f).FullName)
            [void](Register-Xml -TaskName $n -Doc $d)
            Write-Output ("ROLLED_BACK $n from " + $f)
        } else {
            $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
            if ($n -eq 'VegliaWatchdog' -and -not $t) { Write-Output 'VegliaWatchdog not registered, nothing to roll back'; continue }
            Write-Output ("NO_BACKUP_FOR $n (looked for $f)")
        }
    }
    Write-Output 'rollback done - restart any stopped task with Start-ScheduledTask'
    exit 0
}

# ---- 0. preconditions
foreach ($p in @($WatchdogPs1, $RotatePs1, $MaintPs1)) {
    if (-not (Test-Path $p)) { throw ("missing script: $p (run git pull in $DeploymentRoot first)") }
}
New-Item -ItemType Directory -Force -Path (Join-Path $DeploymentRoot 'logs') | Out-Null

# ---- 1. patch the two service tasks (definition only, running instance is preserved)
foreach ($n in $TaskNames) {
    $doc = Read-TaskXml -TaskName $n
    $changed = $false
    $changed = [bool](Ensure-BootTrigger -Doc $doc -TaskName $n) -or [bool]$changed
    $changed = [bool](Disable-StopOnIdleEnd -Doc $doc -TaskName $n) -or [bool]$changed
    if ($changed) {
        [void](Register-Xml -TaskName $n -Doc $doc)
        Write-Output ("  $n re-registered")
    } else {
        Write-Output ("  $n already correct")
    }
}

# ---- 2. VegliaWatchdog, built from the proven VegliaServer skeleton
$wd = Read-TaskXml -TaskName 'VegliaServer'
$m = Get-Ns $wd
$uri = $wd.SelectSingleNode('/t:Task/t:RegistrationInfo/t:URI', $m)
if ($uri) { $uri.InnerText = '\VegliaWatchdog' }
$desc = $wd.SelectSingleNode('/t:Task/t:RegistrationInfo/t:Description', $m)
if (-not $desc) {
    $desc = $wd.CreateElement('Description', $nsUri)
    $reg = $wd.SelectSingleNode('/t:Task/t:RegistrationInfo', $m)
    [void]$reg.InsertBefore($desc, $reg.FirstChild)
}
$desc.InnerText = 'Veglia dual-service watchdog: probes 8513/8514 /health every ' + $WatchdogIntervalMin + ' min, acts after 2 consecutive failures, max 3 actions per hour per service, honours maintenance.flag, alerts through lark-cli. Log: ' + (Join-Path $DeploymentRoot 'logs\watchdog.log')
# principal: same account, highest available (Global mutex + wevtutil)
$rl = $wd.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:RunLevel', $m)
if (-not $rl) {
    $rl = $wd.CreateElement('RunLevel', $nsUri)
    $pr = $wd.SelectSingleNode('/t:Task/t:Principals/t:Principal', $m)
    [void]$pr.AppendChild($rl)
}
$rl.InnerText = 'HighestAvailable'
$lt = $wd.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:LogonType', $m)
if (-not $lt) {
    $lt = $wd.CreateElement('LogonType', $nsUri)
    $pr = $wd.SelectSingleNode('/t:Task/t:Principals/t:Principal', $m)
    [void]$pr.AppendChild($lt)
}
$lt.InnerText = 'S4U'
# action
$actions = $wd.SelectSingleNode('/t:Task/t:Actions', $m)
foreach ($child in @($actions.ChildNodes)) { $actions.RemoveChild($child) | Out-Null }
$exec = $wd.CreateElement('Exec', $nsUri)
foreach ($pair in @(
        @('Command', 'powershell.exe'),
        @('Arguments', ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $WatchdogPs1 + '"')),
        @('WorkingDirectory', $DeploymentRoot))) {
    $e = $wd.CreateElement($pair[0], $nsUri); $e.InnerText = $pair[1]; [void]$exec.AppendChild($e)
}
[void]$actions.AppendChild($exec)
# settings
[void](Set-NodeText $wd '/t:Task/t:Settings/t:ExecutionTimeLimit' 'PT10M')
[void](Set-NodeText $wd '/t:Task/t:Settings/t:MultipleInstancesPolicy' 'IgnoreNew')
[void](Set-NodeText $wd '/t:Task/t:Settings/t:RestartOnFailure/t:Count' '1')
$rfInt = $wd.SelectSingleNode('/t:Task/t:Settings/t:RestartOnFailure/t:Interval', $m)
if ($rfInt) { $rfInt.InnerText = ('PT' + $WatchdogIntervalMin + 'M') }
# triggers: replace everything with a repetition trigger (boot trigger is already there)
$tr = $wd.SelectSingleNode('/t:Task/t:Triggers', $m)
foreach ($child in @($tr.ChildNodes)) { $tr.RemoveChild($child) | Out-Null }
$tt = $wd.CreateElement('TimeTrigger', $nsUri)
$sb = $wd.CreateElement('StartBoundary', $nsUri)
$sb.InnerText = ([DateTime]::Now.AddMinutes(1)).ToString('yyyy-MM-ddTHH:mm:ss')
[void]$tt.AppendChild($sb)
$rep = $wd.CreateElement('Repetition', $nsUri)
$ri = $wd.CreateElement('Interval', $nsUri); $ri.InnerText = ('PT' + $WatchdogIntervalMin + 'M')
$rd = $wd.CreateElement('Duration', $nsUri); $rd.InnerText = 'P3650D'
$sae = $wd.CreateElement('StopAtDurationEnd', $nsUri); $sae.InnerText = 'false'
[void]$rep.AppendChild($ri); [void]$rep.AppendChild($rd); [void]$rep.AppendChild($sae)
[void]$tt.AppendChild($rep)
[void]$tr.AppendChild($tt)
$bt = $wd.CreateElement('BootTrigger', $nsUri)
$ben = $wd.CreateElement('Enabled', $nsUri); $ben.InnerText = 'true'; [void]$bt.AppendChild($ben)
[void]$tr.AppendChild($bt)

$existingWd = Get-ScheduledTask -TaskName 'VegliaWatchdog' -ErrorAction SilentlyContinue
[void](Register-Xml -TaskName 'VegliaWatchdog' -Doc $wd)
Write-Output ('  VegliaWatchdog ' + $(if ($existingWd) { 're-registered' } else { 'registered' }) + " every ${WatchdogIntervalMin}min")

# ---- 3. Task Scheduler audit trail
try {
    & wevtutil.exe sl 'Microsoft-Windows-TaskScheduler/Operational' /e:true /rd:false /ms:33408000 2>&1 | Out-Null
    $st = (Get-WinEvent -ListLog 'Microsoft-Windows-TaskScheduler/Operational' -ErrorAction SilentlyContinue).IsEnabled
    if ($st) { Write-Output '  TaskScheduler/Operational log enabled' }
    else { Write-Output '  WARN TaskScheduler/Operational still disabled - run this script elevated' }
} catch {
    Write-Output ('  WARN could not enable the audit log: ' + $_.Exception.Message)
}

# ---- 4. make sure the services are still up after touching their definitions
foreach ($pair in @(@('VegliaServer', 'http://127.0.0.1:8513/health'), @('VegliaMCP', 'http://127.0.0.1:8514/health'))) {
    if (-not (Get-Health $pair[1])) {
        Write-Output ("  " + $pair[0] + ' unhealthy after re-registration - starting it now')
        Start-ScheduledTask -TaskName $pair[0] -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 8
    }
}

Start-ScheduledTask -TaskName 'VegliaWatchdog' -ErrorAction SilentlyContinue
Write-Output ''
Show-State
exit 0
