#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
INGEST_VENV=/root/rag-lab/.venv-ingest
if [[ ! -x "$INGEST_VENV/bin/python" ]]; then
  python3 -m venv "$INGEST_VENV"
fi
"$INGEST_VENV/bin/python" -c 'import sys; assert sys.version_info >= (3,12), "需要 Python 3.12+"'
"$INGEST_VENV/bin/python" -m pip install --upgrade pip
# 先安装 CPU torch，再用精确约束防止后续解析依赖换成 CUDA 版本。
"$INGEST_VENV/bin/python" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
"$INGEST_VENV/bin/python" - <<'PY' > cpu-runtime.constraints
from importlib.metadata import version
import torch
assert torch.version.cuda is None, '此独立虚拟环境需要 CPU 版 PyTorch'
for name in ('torch', 'torchvision'):
    print(f'{name}=={version(name)}')
PY
"$INGEST_VENV/bin/python" -m pip install -c cpu-runtime.constraints -r requirements.txt
"$INGEST_VENV/bin/python" -m pip check
"$INGEST_VENV/bin/python" -m pip freeze > requirements.installed.lock
"$INGEST_VENV/bin/python" check_environment.py
