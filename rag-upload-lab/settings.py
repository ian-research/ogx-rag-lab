import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("RAG_DATA_DIR", ROOT / "data")).resolve()
OGX = os.environ.get("OGX_URL", "http://127.0.0.1:8321/v1")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "cpu-embedding/bge-small-zh")
LLM = os.environ.get("LLM_MODEL", "local-vllm/rag-llm")
TOKENIZER = os.environ.get("TOKENIZER_PATH", "/root/rag-lab/models/bge-small-zh-v1.5")
DIM = 512
MAX_TOKENS = 384
MAX_BYTES = 50 * 1024 * 1024
SUFFIXES = {".pdf", ".md", ".txt", ".docx", ".doc"}
ACTIVE = ("parsing", "chunking", "embedding")
