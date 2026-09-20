# Scheduled-task runner: hunt a few missing episodes/movies.
# Shows its console window while running (register the task WITHOUT
# -WindowStyle Hidden), streams each step live, and also logs it.
# On any failure the window stays open 20s showing the error.
$Host.UI.RawUI.WindowTitle = "Arr_Auto_Search Hunt"
try {
    Set-Location -LiteralPath $PSScriptRoot
    $py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (-not (Test-Path $py)) { $py = (Get-Command python -ErrorAction Stop).Source }
    & $py -u seriespack.py hunt 2>&1 |
        ForEach-Object { Write-Host $_; "$_" } |
        Out-File hunt_last_run.log -Encoding utf8
    Start-Sleep -Seconds 3   # brief linger so the final lines are readable
} catch {
    Write-Host "runner failed: $_" -ForegroundColor Red
    Start-Sleep -Seconds 20
}
