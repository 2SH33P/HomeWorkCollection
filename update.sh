#!/usr/bin/env bash
# 一键更新（Linux / macOS）：拉取最新代码 -> 提示重启
cd "$(dirname "$0")" || exit 1
PUB="https://github.com/2SH33P/HomeWorkCollection.git"

if ! command -v git >/dev/null 2>&1; then
  echo "[错误] 系统里没有 git 命令，请手动下载新版覆盖。"; exit 1
fi
echo "[1/3] 拉取远程代码..."
if ! git fetch origin main 2>/dev/null; then
  echo "      直连远程失败，改用公开地址（无需登录）重试..."
  if ! git fetch "$PUB" main 2>/dev/null; then
    echo "[错误] 拉取失败：检查网络/代理（国内建议设置代理，见网页「设置→更新代理」）。"; exit 1
  fi
  git checkout -B main FETCH_HEAD 2>/dev/null || git reset --hard FETCH_HEAD
else
  echo "[2/3] 切换到 main 分支并快进合并..."
  git rev-parse --verify main >/dev/null 2>&1 || git branch main origin/main 2>/dev/null
  git checkout main 2>/dev/null || true
  if ! git merge --ff-only origin/main 2>/dev/null; then
    git reset --hard origin/main || { echo "[错误] 合并失败：本地有改动，请先处理后重试。"; exit 1; }
  fi
fi
echo "[3/3] 完成，当前版本："
git log --oneline -1
echo
echo ">>> 请重启服务：重新运行 start.sh（或重新双击 start.bat），然后刷新浏览器页面。"
