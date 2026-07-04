#!/bin/bash
set -e

# 默认使用第0块显卡，可以通过参数指定，如: ./start.sh 1 或 ./start.sh 0 8007
GPU_ID=${1:-0}
PORT_ARG=${2:-}
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$APP_DIR/.env" ]; then
  set -a
  . "$APP_DIR/.env"
  set +a
fi
DEFAULT_PORT=${PORT:-${APP_PORT:-8006}}
RUN_PORT=${PORT_ARG:-$DEFAULT_PORT}
PYTHON_BIN="/home/server/miniconda3/envs/mapagent6/bin/python3"

find_port_pids() {
  ss -ltnp 2>/dev/null | awk -v port=":${RUN_PORT}" '$4 ~ port {print $NF}' \
    | grep -o 'pid=[0-9]\+' | cut -d= -f2 | sort -u
}

cleanup_existing_app() {
  local pids
  pids=$(find_port_pids || true)
  [ -z "$pids" ] && return 0

  for pid in $pids; do
    [ -d "/proc/$pid" ] || continue
    local cwd cmdline
    cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
    cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)

    if [ "$cwd" = "$APP_DIR" ] && echo "$cmdline" | grep -q "main.py"; then
      echo "检测到旧的本项目实例占用端口 ${RUN_PORT}，正在停止 (PID: $pid)..."
      kill "$pid" 2>/dev/null || true
      sleep 2
      if kill -0 "$pid" 2>/dev/null; then
        echo "旧实例未在预期时间内退出，执行强制停止 (PID: $pid)..."
        kill -9 "$pid" 2>/dev/null || true
      fi
    else
      echo "端口 ${RUN_PORT} 已被其他进程占用，无法自动停止。"
      echo "占用进程 PID: $pid"
      echo "请改用其他端口，例如: ./start.sh ${GPU_ID} 8007"
      exit 1
    fi
  done
}

echo "正在指定使用显卡: $GPU_ID"
echo "准备启动端口: $RUN_PORT"
export CUDA_VISIBLE_DEVICES="$GPU_ID"
export PORT="$RUN_PORT"
export USE_INTENT_AGENT="${USE_INTENT_AGENT:-true}"
echo "LangGraph / Intent Agent 模式: $USE_INTENT_AGENT"

# 设置 HuggingFace 镜像源（解决网络连接问题）
export HF_ENDPOINT=https://hf-mirror.com

cleanup_existing_app

cd "$APP_DIR"
exec "$PYTHON_BIN" main.py
