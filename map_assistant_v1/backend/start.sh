#!/bin/bash

# 默认使用第0块显卡，可以通过参数指定，如: ./start.sh 1
GPU_ID=${1:-0}
PORT_ARG=${2:-}

echo "正在指定使用显卡: $GPU_ID"
export CUDA_VISIBLE_DEVICES=$GPU_ID
if [ -n "$PORT_ARG" ]; then export PORT="$PORT_ARG"; fi

# 设置 HuggingFace 镜像源（解决网络连接问题）
export HF_ENDPOINT=https://hf-mirror.com

# 启动 FastAPI 后端
# 使用指定的虚拟环境 Python 路径
/home/server/miniconda3/envs/mapagent6/bin/python3 main.py
