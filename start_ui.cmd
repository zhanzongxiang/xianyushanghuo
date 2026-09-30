@echo off
cd /d "%~dp0"
if exist "%~dp0XianYuMaterialPipeline_v24_profile_isolation.exe" (
  "%~dp0XianYuMaterialPipeline_v24_profile_isolation.exe" --config "%~dp0test.local.json" --web-ui
) else (
  if not exist "%~dp0config\test.local.json" copy "%~dp0config\test.example.json" "%~dp0config\test.local.json" >nul
  "%~dp0.venv\Scripts\python.exe" "%~dp0run_pipeline.py" --config "%~dp0config\test.local.json" --web-ui
)
if errorlevel 1 pause
