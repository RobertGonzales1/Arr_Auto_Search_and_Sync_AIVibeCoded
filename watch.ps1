# Live monitor for the seriespack scheduled tasks. Leave this window open;
# it refreshes every few seconds. Ctrl+C to quit.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File watch.ps1
param([switch]$Once)

$here = $PSScriptRoot

function TaskLine($name) {
    $q = schtasks /Query /TN $name /FO LIST /V 2>$null
    if (-not $q) { return "  $name  -- not installed on this machine" }
    $get = { param($label)
        $m = @($q | Select-String ("^" + $label + ":"))
        if ($m.Count) { $m[0].ToString().Split(":", 2)[1].Trim() } else { "?" } }
    $status = & $get "Status"
    $last   = & $get "Last Run Time"
    $result = & $get "Last Result"
    $next   = & $get "Next Run Time"
    $verdict = if ($status -eq "Running") { ">>> RUNNING <<<" }
               elseif ($result -eq "0")   { "ok" }
               elseif ($result -eq "267011") { "not run yet" }
               else { "last result $result" }
    "  {0,-18} {1,-16} last: {2,-22} next: {3}" -f $name, $verdict, $last, $next
}

function TailLog($file, $n) {
    $p = Join-Path $here $file
    if (Test-Path $p) {
        $age = [int]((Get-Date) - (Get-Item $p).LastWriteTime).TotalSeconds
        Write-Host ("--- {0} (updated {1}s ago) " -f $file, $age).PadRight(78, "-")
        Get-Content -LiteralPath $p -Tail $n -ErrorAction SilentlyContinue |
            ForEach-Object { Write-Host ("  " + $_) }
    } else {
        Write-Host ("--- {0} (no runs yet) " -f $file).PadRight(78, "-")
    }
}

while ($true) {
    Clear-Host
    Write-Host ("seriespack monitor -- {0}   (Ctrl+C to quit)" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
    Write-Host ""
    Write-Host (TaskLine "Arr_Auto_Search Hunt")
    Write-Host (TaskLine "Arr_Auto_Search Clean")
    Write-Host ""
    TailLog "hunt_last_run.log" 14
    Write-Host ""
    TailLog "clean_last_run.log" 8
    if ($Once) { break }
    Start-Sleep -Seconds 3
}
