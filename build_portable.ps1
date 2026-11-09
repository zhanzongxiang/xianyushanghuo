$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Missing .venv. Create the environment and install requirements.txt first."
}
$name = "XianYuMaterialPipeline_v56_tdlib_download"
$dist = Join-Path $root "outputs"
$build = Join-Path $root "build_v56_tdlib_download"
$out = Join-Path $dist $name
if (Test-Path -LiteralPath $out) { throw "Output already exists. Preserve it and select a new version name: $out" }
if (Test-Path -LiteralPath (Join-Path $dist "$name.zip")) { throw "Portable ZIP already exists. Preserve the existing version: $name.zip" }
& $python (Join-Path $root "run_pipeline.py") --download-check
if ($LASTEXITCODE -ne 0) { throw "Download acceleration self-test failed." }
& $python (Join-Path $root "run_pipeline.py") --tdlib-check
if ($LASTEXITCODE -ne 0) { throw "TDLib self-test failed." }
& $python -c "from xianyu_pipeline.runtime import telegram_crypto_backend; assert telegram_crypto_backend() == 'cryptg', 'Install requirements.txt before building.'"
if ($LASTEXITCODE -ne 0) { throw "cryptg acceleration is required for the portable build." }
& $python -m PyInstaller --noconfirm --onedir --name $name --distpath $dist --workpath $build --specpath $build --additional-hooks-dir (Join-Path $root "packaging_hooks") --hidden-import playwright --collect-all playwright --collect-all cryptg --collect-all tdjson --collect-all qrcode --collect-all PIL --collect-all httpx --collect-all sqlalchemy --collect-all psycopg --collect-all psycopg_binary --collect-all pymysql --collect-all cryptography --add-data "$(Join-Path $root 'third_party');third_party" --add-data "$(Join-Path $root 'xianyu_pipeline\static');xianyu_pipeline\static" (Join-Path $root "run_pipeline.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
& (Join-Path $out "$name.exe") --tdlib-check
if ($LASTEXITCODE -ne 0) { throw "Packaged TDLib self-test failed." }
Copy-Item (Join-Path $root "config\test.example.json") (Join-Path $out "test.local.json") -Force
Copy-Item (Join-Path $root "start_ui.cmd") (Join-Path $out "start_ui.cmd") -Force
Copy-Item (Join-Path $root "start_database_tunnel.ps1") (Join-Path $out "start_database_tunnel.ps1") -Force
Copy-Item (Join-Path $root "README_TEST.md") (Join-Path $out "README_TEST.md") -Force
& $python (Join-Path $root "package_portable.py") $out (Join-Path $root "config\test.example.json") (Join-Path $dist "$name.zip")
if ($LASTEXITCODE -ne 0) { throw "Portable ZIP build or privacy check failed." }
Write-Host "Build complete: $out"
Write-Host "Start with start_ui.cmd. Chromium is not bundled; configure a browser path only for Quark uploads."
