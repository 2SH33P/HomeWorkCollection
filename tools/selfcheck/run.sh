#!/usr/bin/env bash
# 一条命令跑全部自检（前端纯逻辑 + 后端逻辑）。不碰用户数据，不需要起服务。
#
#   bash tools/selfcheck/run.sh
#   PYTHON=/path/to/python bash tools/selfcheck/run.sh
set -u
cd "$(dirname "$0")/../.."

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in .venv/bin/python ../.venv/bin/python python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "import fastapi, cv2, typst, PIL, numpy" >/dev/null 2>&1; then
      PY="$c"; break
    fi
  done
fi

echo "== 前端纯逻辑（续块状态机 / 取消续块并题 / 草稿时间戳 / 已入库回标）=="
if command -v node >/dev/null 2>&1; then
  node tools/selfcheck/logic.test.js || exit 1
else
  echo "  (跳过：没装 node)"
fi

echo
echo "== 后端逻辑（图块坐标/全局裁图/续块并题/PDF/组卷/改名/仓库）=="
if [ -n "$PY" ]; then
  "$PY" tools/selfcheck/backend.test.py || exit 1
else
  echo "  (跳过：找不到带 fastapi/cv2/typst 的 python；先装依赖或设 PYTHON=...)"
fi

echo
echo "全部自检通过 ✓"
