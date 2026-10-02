$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Missing .venv. Create the environment and install requirements.txt first."
}
$name = "XianYuMaterialPipeline_v34_download_directory"
$dist = Join-Path $root "outputs"
$build = Join-Path $root "build_v34"
$out = Join-Path $dist $name
if (Test-Path -LiteralPath $out) { throw "Output already exists. Preserve it and select a new version name: $out" }
if (Test-Path -LiteralPath (Join-Path $dist "$name.zip")) { throw "Portable ZIP already exists. Preserve the existing version: $name.zip" }
& $python -m PyInstaller --noconfirm --onedir --name $name --distpath $dist --workpath $build --specpath $build --hidden-import playwright --collect-all playwright --add-data "$(Join-Path $root 'xianyu_pipeline\static');xianyu_pipeline\static" (Join-Path $root "run_pipeline.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
Copy-Item (Join-Path $root "config\test.example.json") (Join-Path $out "test.local.json") -Force
Copy-Item (Join-Path $root "start_ui.cmd") (Join-Path $out "start_ui.cmd") -Force
Copy-Item (Join-Path $root "README_TEST.md") (Join-Path $out "README_TEST.md") -Force
& $python (Join-Path $root "package_portable.py") $out (Join-Path $root "config\test.example.json") (Join-Path $dist "$name.zip")
if ($LASTEXITCODE -ne 0) { throw "Portable ZIP build or privacy check failed." }
Write-Host "Build complete: $out"
Write-Host "Start with start_ui.cmd. Chromium is not bundled; configure a browser path for Telegram Web or Quark uploads."
