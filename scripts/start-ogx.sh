#!/usr/bin/env bash
# 不把数据库密码写入配置文件。与已运行的 OGX 二选一启动，避免端口冲突。
set -euo pipefail
CONFIG_PATH="${1:-/root/rag-lab/ogx/config.yaml}"
[[ -f "$CONFIG_PATH" ]] || { echo "配置文件不存在: $CONFIG_PATH" >&2; exit 1; }
if [[ -z "${PGVECTOR_PASSWORD:-}" ]]; then
  read -r -s -p '请输入 rag_app 数据库密码: ' PGVECTOR_PASSWORD
  echo
fi
[[ -n "$PGVECTOR_PASSWORD" ]] || { echo '密码不能为空' >&2; exit 1; }
export PGVECTOR_PASSWORD
export CUDA_VISIBLE_DEVICES=""
mkdir -p /root/rag-lab/ogx/data
exec /root/rag-lab/.venv-ogx/bin/ogx run "$CONFIG_PATH"
