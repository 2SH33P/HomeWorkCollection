@echo off
chcp 936 >nul 2>nul
setlocal
cd /d "%~dp0"
echo ============================================
echo   错题收集工具 - 一键更新（Windows）
echo ============================================
echo.
set PUB=https://github.com/2SH33P/HomeWorkCollection.git

where git >nul 2>&1
if errorlevel 1 (
  echo [错误] 系统里没有 git 命令。
  echo        请手动到 https://github.com/2SH33P/HomeWorkCollection 下载新版覆盖
  echo        或者安装 Git for Windows 后再运行本脚本。
  echo.
  pause
  exit /b 1
)

echo [1/3] 拉取远程代码...
git fetch origin main
if errorlevel 1 (
  echo       直连远程失败，改用公开地址重试...
  git fetch %PUB% main
  if errorlevel 1 (
    echo.
    echo [错误] 拉取失败：检查网络。国内网络建议：
    echo        1) 打开代理软件的“全局/TUN 模式”，或
    echo        2) 在网页「设置 - 更新代理」里填代理地址后再点“立即更新”
    echo.
    pause
    exit /b 1
  )
  git checkout -B main FETCH_HEAD
  goto done
)

echo [2/3] 切换到 main 分支并快进合并...
git rev-parse --verify main >nul 2>&1
if errorlevel 1 git branch main origin/main
git checkout main
git merge --ff-only origin/main
if errorlevel 1 (
  echo       本地有改动，强制对齐到远程版本...
  git reset --hard origin/main
  if errorlevel 1 (
    echo [错误] 更新失败，请手动处理。 & pause & exit /b 1
  )
)

:done
echo [3/3] 完成，当前版本：
git log --oneline -1
echo.
echo >>> 请重启服务：关掉旧的黑窗口，重新双击 start.bat，然后刷新浏览器页面。
echo.
pause
