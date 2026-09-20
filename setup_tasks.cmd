@echo off
rem Registers the two Arr_Auto_Search scheduled tasks, pointing at the runner
rem scripts in THIS folder. Run it by double-clicking or from cmd.
rem Re-running is safe: /F overwrites the existing tasks.

rem drop tasks from before the rename, if any
schtasks /Delete /F /TN "seriespack hunt" >nul 2>&1
schtasks /Delete /F /TN "seriespack clean" >nul 2>&1

schtasks /Create /F /TN "Arr_Auto_Search Hunt" /SC MINUTE /MO 30 /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"%~dp0run_hunt.ps1\""
schtasks /Create /F /TN "Arr_Auto_Search Clean" /SC DAILY /ST 04:00 /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"%~dp0run_clean.ps1\""

rem Uncomment once [seedbox]/[sync] are configured in seriespack.ini:
rem schtasks /Create /F /TN "Arr_Auto_Search Sync" /SC MINUTE /MO 15 /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"%~dp0run_sync.ps1\""

echo.
echo Stored commands (verify the quoted paths look right):
schtasks /Query /TN "Arr_Auto_Search Hunt" /FO LIST /V | findstr /C:"Task To Run"
schtasks /Query /TN "Arr_Auto_Search Clean" /FO LIST /V | findstr /C:"Task To Run"
echo.
pause
