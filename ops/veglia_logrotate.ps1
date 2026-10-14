<#
    veglia_logrotate.ps1 - bounded, handle-safe log rotation for Veglia runtime logs.

    WHY copytruncate instead of rename:
      veglia_server.py keeps logs/veglia-server.log open through redirected sys.stdout /
      sys.stderr for its whole lifetime, and uvicorn holds veglia-mcp.log. Renaming a file
      another process holds on Windows either fails or leaves those stale handles writing
      into the archive file, so the live log would silently stop receiving the interesting
      lines (banner, tracebacks). Copying first and then truncating in place keeps every
      existing handle valid: the append-mode writers simply continue at the new end of file.

    SAFETY RULES
      - Never delete the live file; never truncate unless the archive copy is at least as
        long as the live file was when the copy started (copytruncate tail loss is reported).
      - Archives are named <name>.<yyyyMMdd-HHmmss>.log under D:\veglia\logs\archive.
      - Files whose name starts with 'keep-' are evidence and are never deleted.
      - Retention: newest -KeepPerLog archives per log AND an overall -MaxTotalMB budget.
      - Nothing here is committed to Git (*.log is gitignored).

    USAGE
      veglia_logrotate.ps1                       rotate anything over 16 MB
      veglia_logrotate.ps1 -DryRun               report what would happen, touch nothing
      veglia_logrotate.ps1 -ThresholdMB 8        lower the trigger threshold
    EXIT: 0 all good, 2 one or more logs failed to rotate (live logs untouched in that case)
#>

[CmdletBinding()]
param(
    [int]$ThresholdMB = 16,
    [int]$KeepPerLog = 10,
    [int]$MaxTotalMB = 400,
    [string]$LogDir = 'D:\veglia\server\logs',
    [string]$ArchiveDir = 'D:\veglia\logs\archive',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$LogNames = @('veglia-server.log', 'veglia-mcp.log', 'device-sensor.log')
$failed = 0

if (-not (Test-Path $ArchiveDir) -and -not $DryRun) { New-Item -ItemType Directory -Force -Path $ArchiveDir | Out-Null }

foreach ($name in $LogNames) {
    $src = Join-Path $LogDir $name
    if (-not (Test-Path $src)) { Write-Output ("SKIP missing=$src"); continue }
    $item = Get-Item $src
    $mb = [Math]::Round($item.Length / 1MB, 2)
    if ($item.Length -lt ($ThresholdMB * 1MB)) { Write-Output ("OK $name size_mb=$mb under_threshold=$ThresholdMB"); continue }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $dst = Join-Path $ArchiveDir ($name + '.' + $stamp + '.log')
    if ($DryRun) { Write-Output ("WOULD_ROTATE $name size_mb=$mb -> $dst"); continue }

    $preLen = $item.Length
    try {
        Copy-Item -Path $src -Destination $dst -Force
        $copied = (Get-Item $dst).Length
        # The live log keeps growing while we copy (device reports land every couple of
        # seconds), so an exact match is NOT the right condition and would block rotation
        # forever on a busy service. Require only that the archive is not SHORTER than what
        # was on disk when we started. Any extra live bytes written during the copy are the
        # documented copytruncate tail loss and are reported, not hidden.
        if ($copied -lt $preLen) { throw ("copy incomplete: live=" + $preLen + " copy=" + $copied) }
        $tailLost = [Math]::Max(0, (Get-Item $src).Length - $copied)
        $fs = [IO.File]::Open($src, 'Open', 'Write', 'ReadWrite')
        try { $fs.SetLength(0) } finally { $fs.Close() }
        Write-Output ("ROTATED $name archived_mb=" + [Math]::Round($copied / 1MB, 2) + " -> " + (Split-Path $dst -Leaf) + " tail_lost_bytes=" + $tailLost + " live_now_mb=" + [Math]::Round((Get-Item $src).Length / 1MB, 2))
    } catch {
        $failed++
        Write-Output ("ROTATE_FAILED $name " + $_.Exception.Message)
        if (Test-Path $dst) { Remove-Item $dst -Force -ErrorAction SilentlyContinue }
        continue
    }

    # retention per log
    try {
        $set = Get-ChildItem -Path $ArchiveDir -Filter ($name + '.*.log') -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -notlike 'keep-*' } | Sort-Object LastWriteTime -Descending
        if ($set.Count -gt $KeepPerLog) {
            foreach ($old in ($set | Select-Object -Skip $KeepPerLog)) {
                Remove-Item $old.FullName -Force
                Write-Output ("PRUNED " + $old.Name)
            }
        }
    } catch { $failed++; Write-Output ('PRUNE_FAILED ' + $name + ' ' + $_.Exception.Message) }
}

# overall budget across the archive dir (keep-'d evidence is protected but still counted)
try {
    $all = Get-ChildItem -Path $ArchiveDir -Filter '*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending
    $total = ($all | Measure-Object -Property Length -Sum).Sum
    if ($total -gt ($MaxTotalMB * 1MB)) {
        $victimBudget = $total
        foreach ($v in ($all | Where-Object { $_.Name -notlike 'keep-*' })) {
            if ($victimBudget -le ($MaxTotalMB * 1MB)) { break }
            Remove-Item $v.FullName -Force
            $victimBudget -= $v.Length
            Write-Output ('PRUNED_BUDGET ' + $v.Name)
        }
    }
} catch { $failed++; Write-Output ('BUDGET_FAILED ' + $_.Exception.Message) }

if ($failed -gt 0) { exit 2 }
exit 0
