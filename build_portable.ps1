$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "未找到 .venv，请先创建虚拟环境并安装 requirements.txt"
}
$name = "XianYuMaterialPipeline_v24_profile_isolation"
$dist = Join-Path $root "outputs"
$build = Join-Path $root "build_v24"
$out = Join-Path $dist $name
if (Test-Path -LiteralPath $out) { throw "输出目录已存在，请先保留已有版本并选择新的版本名称：$out" }
& $python -m PyInstaller --noconfirm --onedir --name $name --distpath $dist --workpath $build --specpath $build --hidden-import playwright --collect-all playwright (Join-Path $root "run_pipeline.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }
Copy-Item (Join-Path $root "config\test.example.json") (Join-Path $out "test.local.json") -Force
Copy-Item (Join-Path $root "start_ui.cmd") (Join-Path $out "start_ui.cmd") -Force
Copy-Item (Join-Path $root "README_TEST.md") (Join-Path $out "README_TEST.md") -Force
Write-Host "构建完成：$out"
Write-Host "不再附带 Chromium。通过 start_ui.cmd 启动，并在配置页填写系统浏览器程序路径。"
