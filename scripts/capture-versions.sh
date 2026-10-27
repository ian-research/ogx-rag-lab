#!/usr/bin/env bash
# 仅收集公开软件版本，不导出环境变量、密码、文档内容或 OGX 展开的配置。
set -euo pipefail
OUT="${1:?请指定一个不存在的输出目录}"
[[ ! -e "$OUT" ]] || { echo '为避免覆盖，请使用新的输出目录' >&2; exit 1; }
umask 077
mkdir -p "$OUT"
cat /etc/os-release > "$OUT/os-release.txt"
uname -r > "$OUT/kernel.txt"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv > "$OUT/gpu.csv"
for name in vllm ogx ingest; do
  PY="/root/rag-lab/.venv-$name/bin/python"
  if [[ -x "$PY" ]]; then
    "$PY" -V > "$OUT/python-$name.txt" 2>&1
    "$PY" -m pip list --format=json > "$OUT/packages-$name.json"
    "$PY" -m pip check > "$OUT/pip-check-$name.txt" 2>&1 || true
  fi
done
if [[ -f /root/rag-lab/database/image.lock ]]; then
  cp /root/rag-lab/database/image.lock "$OUT/postgres-image.lock"
fi
podman exec rag-postgres psql -U postgres -d rag -Atc \
  "SELECT version(); SELECT extname || ':' || extversion FROM pg_extension WHERE extname='vector';" > "$OUT/database.txt"
echo "已保存软件清单到 $OUT；模型 revision 需另从下载记录核对。公开前检查所有内容。"
