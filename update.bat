@echo off
chcp 936 >nul 2>nul
cd /d "%~dp0"
echo ============================================
echo   错题收集工具 - 一键更新
echo ============================================
echo.
set PUB=https://github.com/2SH33P/HomeWorkCollection.git

where git >nul 2>&1
if errorlevel 1 goto nogit

echo [1/3] 拉取远程代码...
git fetch origin main
if not errorlevel 1 goto okbranch
echo       直连远程失败，改用公开地址重试...
git fetch %PUB% main
if errorlevel 1 goto netfail
git checkout -B main FETCH_HEAD
goto done

:okbranch
git rev-parse --verify main >nul 2>&1
if errorlevel 1 git branch main origin/main
git checkout main >nul 2>&1
git merge --ff-only origin/main
if errorlevel 1 git reset --hard origin/main

:done
echo [3/3] 完成，当前版本：
git log --oneline -1
echo.
echo >>> 请重启服务：关掉旧窗口，重新双击 start.bat，然后刷新浏览器页面。
echo.
pause
exit /b 0

:nogit
echo [错误] 系统里找不到 git 命令。
echo        请安装 Git for Windows，或到下面地址下载新版覆盖本目录：
echo        https://github.com/2SH33P/HomeWorkCollection
echo.
pause
exit /b 1

:netfail
echo.
echo [错误] 拉取失败：请检查网络。
echo        国内网络建议：1) 代理开全局/TUN 模式；2) 或用网页「设置-更新代理」。
echo.
pause
exit /b 1
