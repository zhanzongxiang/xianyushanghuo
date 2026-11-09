"""Keep the Windows wheel's native dependencies beside the extension."""
from PyInstaller.utils.hooks import collect_delvewheel_libs_directory

datas, binaries = collect_delvewheel_libs_directory("tdjson")
