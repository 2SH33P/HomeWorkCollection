@echo off
setlocal
cd /d "%~dp0"
echo 错题收集工具 - 启动
echo.

if exist ".venv\Scripts\python.exe" goto run

echo [首次运行] 正在准备环境，约2分钟，请耐心等待...
where py >nul 2>&1
if not errorlevel 1 goto mkvenv_py
where python >nul 2>&1
if not errorlevel 1 goto mkvenv_py
echo.
echo [错误] 未找到 Python。
echo 请到 https://www.python.org/downloads/ 安装，
echo 安装时务必勾选 "Add Python to PATH"，然后重试。
echo.
pause
exit /b 1

:mkvenv_py
py -3 -m venv .venv
if errorlevel 1 goto mkvenv_python
goto deps

:mkvenv_python
python -m venv .venv

:deps
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q fastapi "uvicorn[standard]" python-multipart pillow opencv-python-headless numpy
if errorlevel 1 goto deps_fail

:run
echo 正在启动服务，稍后自动打开浏览器...
echo 关闭本窗口即停止服务。
echo.
start "" cmd /c "ping -n 3 127.0.0.1 >nul && start http://localhost:8091"
".venv\Scripts\python.exe" "tools\crop-tool\app.py"
echo.
echo 服务已停止。
pause
exit /b 0

:deps_fail
echo.
echo [错误] 依赖安装失败，请检查网络后重新运行。
echo.
pause
exit /b 1
