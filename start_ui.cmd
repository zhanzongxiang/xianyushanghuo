@echo off
cd /d "%~dp0"
set "WEB_PORT="
set "APP_EXE="
for %%F in ("%~dp0XianYuMaterialPipeline_*.exe") do (
  if exist "%%~fF" if not defined APP_EXE set "APP_EXE=%%~fF"
)
for /f %%P in ('powershell -NoProfile -Command "$p=8765; while (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { $p++ }; $p"') do set "WEB_PORT=%%P"
if not defined WEB_PORT set "WEB_PORT=8765"
echo Web UI: http://127.0.0.1:%WEB_PORT%/
if defined APP_EXE (
  "%APP_EXE%" --config "%~dp0test.local.json" --web-ui --web-port %WEB_PORT%
) else (
  if not exist "%~dp0config\test.local.json" copy "%~dp0config\test.example.json" "%~dp0config\test.local.json" >nul
  "%~dp0.venv\Scripts\python.exe" "%~dp0run_pipeline.py" --config "%~dp0config\test.local.json" --web-ui --web-port %WEB_PORT%
)
if errorlevel 1 pause
