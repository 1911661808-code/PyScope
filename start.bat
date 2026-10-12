@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 (
    echo 请先安装 Python 3.10 或更新版本，并启用 Python Launcher。
    pause
    exit /b 1
  )
)
.venv\Scripts\python.exe -m pip show Flask >nul 2>&1
if errorlevel 1 (
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 (
    echo 依赖安装失败。请检查网络或按 README 手动安装。
    pause
    exit /b 1
  )
)
echo 浏览器打开 http://127.0.0.1:8765
.venv\Scripts\python.exe app.py --open
pause
