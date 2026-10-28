#!/usr/bin/env bash
# 整理后的参考启动入口；未在全新 RHEL 主机重跑。模型目录由调用方指定。
set -euo pipefail
export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-13.3}"
export PATH="/root/rag-lab/.venv-vllm/bin:$CUDA_HOME/bin:$PATH"
command -v nvcc >/dev/null || { echo '缺少 nvcc，请先完成基础安装'; exit 1; }
command -v ninja >/dev/null || { echo '缺少 ninja，请先完成基础安装'; exit 1; }
: "${MODEL_PATH:?请设置已有的生成模型绝对目录 MODEL_PATH}"
[[ "$MODEL_PATH" = /* && -d "$MODEL_PATH" ]] || { echo 'MODEL_PATH 必须是存在的绝对目录' >&2; exit 1; }
exec /root/rag-lab/.venv-vllm/bin/vllm serve "$MODEL_PATH" \
  --host 127.0.0.1 --port 8000 --served-model-name rag-llm \
  --dtype bfloat16 --max-model-len 8192 --max-num-seqs 1 \
  --gpu-memory-utilization 0.8 --language-model-only
