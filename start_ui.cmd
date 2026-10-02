@echo off
cd /d "%~dp0"
set "WEB_PORT="
for /f %%P in ('powershell -NoProfile -Command "$p=8765; while (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) { $p++ }; $p"') do set "WEB_PORT=%%P"
if not defined WEB_PORT set "WEB_PORT=8765"
echo Web UI: http://127.0.0.1:%WEB_PORT%/
if exist "%~dp0XianYuMaterialPipeline_v34_download_directory.exe" (
  "%~dp0XianYuMaterialPipeline_v34_download_directory.exe" --config "%~dp0test.local.json" --web-ui --web-port %WEB_PORT%
) else (
  if not exist "%~dp0config\test.local.json" copy "%~dp0config\test.example.json" "%~dp0config\test.local.json" >nul
  "%~dp0.venv\Scripts\python.exe" "%~dp0run_pipeline.py" --config "%~dp0config\test.local.json" --web-ui --web-port %WEB_PORT%
)
if errorlevel 1 pause
