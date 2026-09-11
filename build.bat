@echo off
chcp 936 >nul 2>nul
setlocal
cd /d "%~dp0"
echo 打包错题收集工具 - 生成免安装单文件
echo.

if exist ".venv\Scripts\python.exe" goto deps

echo [首次运行] 创建 Python 环境...
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
echo 安装依赖与打包工具（约3-5分钟）...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q pyinstaller fastapi "uvicorn[standard]" python-multipart pillow opencv-python-headless numpy
if errorlevel 1 goto deps_fail

echo 开始打包...
".venv\Scripts\python.exe" -m PyInstaller --onefile --name 错题收集工具 ^
  --add-data "工具\错题裁剪工具\static;static" ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.lifespan.on ^
  tools\crop-tool\app.py
if errorlevel 1 goto build_fail

echo.
echo 完成! 免安装版: dist\错题收集工具.exe
echo 拷到任何电脑双击即用，数据保存在 exe 同目录。
echo.
pause
exit /b 0

:deps_fail
echo.
echo [错误] 依赖安装失败，请检查网络后重新运行。
echo.
pause
exit /b 1

:build_fail
echo.
echo [错误] 打包失败，请把本窗口内容截图反馈。
echo.
pause
exit /b 1
