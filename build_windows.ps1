param(
    [string]$Python = "python",
    [string]$WorkDirectory = ""
)

# 在 Windows 上重建免安装目录；中间文件与发布文件分开存放。
$ErrorActionPreference = "Stop"
$sourceDirectory = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($WorkDirectory)) {
    $WorkDirectory = Join-Path ([System.IO.Path]::GetTempPath()) "PyScope-build"
}
$buildDirectory = [System.IO.Path]::GetFullPath($WorkDirectory)
$specDirectory = Join-Path $buildDirectory "spec"
$cacheDirectory = Join-Path $buildDirectory "cache"
$releaseDirectory = Join-Path $sourceDirectory "Windows免安装版"
New-Item -ItemType Directory -Force -Path $specDirectory, $cacheDirectory, $releaseDirectory | Out-Null

# 用户代码是运行时编译的，因此常用标准库须显式收录。
# 第三方科学计算库不在此标准库发行包中；源码版可自行安装并重新打包。
$standardModules = @(
    "math", "cmath", "random", "statistics", "decimal", "fractions",
    "collections", "collections.abc", "heapq", "itertools", "functools",
    "operator", "bisect", "array", "copy", "re", "string", "textwrap",
    "datetime", "calendar", "time", "json", "csv", "pathlib", "io",
    "os", "sys", "pprint", "typing", "dataclasses", "enum", "uuid",
    "hashlib", "base64", "struct", "gzip", "zipfile", "pickle",
    "sqlite3", "urllib.request", "urllib.parse", "xml.etree.ElementTree",
    "queue", "threading", "asyncio", "concurrent.futures", "difflib"
)
$arguments = @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
    "--name", "PyScope", "--distpath", $releaseDirectory,
    "--workpath", $cacheDirectory, "--specpath", $specDirectory,
    "--add-data", ((Join-Path $sourceDirectory "templates") + ";templates"),
    "--add-data", ((Join-Path $sourceDirectory "static") + ";static"),
    "--hidden-import", "trace_worker", "--hidden-import", "profile_worker"
)
foreach ($module in $standardModules) {
    $arguments += @("--hidden-import", $module)
}
$arguments += (Join-Path $sourceDirectory "app.py")

Push-Location $sourceDirectory
try {
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 打包失败，退出码：$LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$executable = Join-Path $releaseDirectory "PyScope\PyScope.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "打包命令结束，但没有找到 PyScope.exe。"
}

# 保留运行环境、Web 依赖、编辑器及打包运行时代码的第三方许可。
$licenseScript = @'
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import sys

source = Path(sys.argv[1])
release = Path(sys.argv[2]) / "PyScope"
target = release / "THIRD_PARTY_LICENSES"
target.mkdir(parents=True, exist_ok=True)
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if not python_license.is_file():
    raise RuntimeError("Python LICENSE.txt was not found; retain the runtime license before distribution")
shutil.copy2(python_license, target / "Python-LICENSE.txt")
manifest = [{"name": "Python", "version": sys.version.split()[0], "license_files": ["Python-LICENSE.txt"]}]
names = ["Flask", "Werkzeug", "Jinja2", "MarkupSafe", "itsdangerous", "click", "blinker", "packaging", "setuptools", "pyinstaller", "pyinstaller-hooks-contrib"]
for name in names:
    distribution = metadata.distribution(name)
    copied = []
    for relative in distribution.files or []:
        filename = Path(str(relative)).name.lower()
        if not any(word in filename for word in ("license", "copying", "notice")):
            continue
        if Path(str(relative)).suffix.lower() in (".py", ".pyc", ".pyd"):
            continue
        original = Path(distribution.locate_file(relative))
        if not original.is_file():
            continue
        destination = target / (name + "-" + distribution.version) / str(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, destination)
        copied.append(destination.relative_to(target).as_posix())
    if not copied:
        raise RuntimeError("No license file found for " + name)
    manifest.append({"name": name, "version": distribution.version, "license_files": copied})
editor_license = source / "static" / "vendor" / "codemirror" / "LICENSE"
if editor_license.is_file():
    shutil.copy2(editor_license, target / "CodeMirror-LICENSE.txt")
    manifest.append({"name": "CodeMirror", "license_files": ["CodeMirror-LICENSE.txt"]})
(target / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print("Third-party licenses retained:", len(manifest), "components")
'@
$licenseScript | & $Python - $sourceDirectory $releaseDirectory
if ($LASTEXITCODE -ne 0) {
    throw "第三方许可证收集失败。"
}
Write-Output "免安装版已生成：$executable"
Write-Output "请将整个 PyScope 文件夹一起复制，不能只复制 exe。"
