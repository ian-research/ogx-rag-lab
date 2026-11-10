"""Configuration shared by the two independent, loopback-only services."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIVATE = Path(os.getenv("PRIVATE_DIR", str(ROOT / "private")))
RUN_ROOT = Path(os.getenv("RUN_ROOT", str(ROOT / "evidence" / "runs")))
OGX_BASE = os.getenv("OGX_BASE", "http://127.0.0.1:8321").rstrip("/")
OGX_MODEL = os.getenv("OGX_MODEL", "local-vllm/rag-llm")
MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8090/mcp")
DOC_SERVICE = os.getenv("DOC_SERVICE", "http://127.0.0.1:8089").rstrip("/")


def positive(name, default, cast=int):
    value = cast(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


MAX_STEPS = positive("MAX_STEPS", 4)
MAX_TOOL_CALLS = positive("MAX_TOOL_CALLS", 6)
REQUEST_TIMEOUT = positive("REQUEST_TIMEOUT", 240, float)
MODEL_TIMEOUT = positive("MODEL_TIMEOUT", 120, float)
TOOL_TIMEOUT = positive("TOOL_TIMEOUT", 60, float)
MAX_TOOL_BYTES = positive("MAX_TOOL_BYTES", 24000)
MAX_TOTAL_TOOL_BYTES = positive("MAX_TOTAL_TOOL_BYTES", 48000)
