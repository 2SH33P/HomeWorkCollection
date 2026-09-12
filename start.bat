@echo off
chcp 936 >nul 2>nul
setlocal
cd /d "%~dp0"
echo 错题收集工具 - 启动
echo.

if exist ".venv\Scripts\python.exe" goto checkdeps

echo [首次运行] 正在准备运行环境（约 2 分钟），请耐心等待...
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
echo 正在安装依赖（首次约 2-3 分钟，请勿关闭本窗口）...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install fastapi "uvicorn[standard]" python-multipart pillow opencv-python-headless numpy typst
if errorlevel 1 goto deps_fail

:checkdeps
rem 检查依赖是否齐(升级版本后可能新增依赖, 缺失则自动补装)
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, cv2, PIL, numpy, typst" >nul 2>&1
if not errorlevel 1 goto run
if "%TRIED%"=="1" goto deps_fail
set TRIED=1
goto deps

:run
echo.
echo 服务已启动，浏览器将自动打开...
echo 关闭本窗口即停止服务
echo.
start "" cmd /c "ping -n 3 127.0.0.1 >nul && start http://localhost:8091"
:serve
".venv\Scripts\python.exe" "tools\crop-tool\app.py"
rem 退出码 3 = 检测到源码变化 -> 自动重启(热更新)
if "%errorlevel%"=="3" (
  echo.
  echo [热更新] 检测到代码变化，正在自动重启...
  timeout /t 1 >nul
  goto serve
)
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
