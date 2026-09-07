#!/usr/bin/env bash
# 一键启动 AudioMuse 服务（开发模式）。
# 首次运行自动创建虚拟环境并安装依赖；之后复用 .venv。
# 用法：scripts/start.sh [--reload]
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
VENV=.venv
if [ ! -x "$VENV/bin/python" ]; then
  echo "[start] 首次运行：创建虚拟环境 $VENV ..."
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  echo "[start] 安装依赖：pip install -e '.[dev]' ..."
  "$VENV/bin/pip" install --quiet -e ".[dev]"
fi

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
EXTRA=()
if [[ "${1:-}" == "--reload" ]]; then
  EXTRA+=(--reload)
fi
exec "$VENV/bin/uvicorn" app.main:app --host "$HOST" --port "$PORT" "${EXTRA[@]}"
