"""Configuration for the two independent, loopback-only action services."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Persistent state lives outside the code directory so a revision switch never
# takes the database or the signing keys with it.
STATE_DIR = Path(os.getenv("STATE_DIR", str(ROOT.parent / "rag-action-state")))
PRIVATE = Path(os.getenv("PRIVATE_DIR", str(STATE_DIR / "private")))
DATA_DIR = Path(os.getenv("DATA_DIR", str(STATE_DIR / "data")))
RUN_ROOT = Path(os.getenv("RUN_ROOT", str(STATE_DIR / "evidence" / "runs")))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "action.db")))

OGX_BASE = os.getenv("OGX_BASE", "http://127.0.0.1:8321").rstrip("/")
OGX_MODEL = os.getenv("OGX_MODEL", "local-vllm/rag-llm")
DOC_SERVICE = os.getenv("DOC_SERVICE", "http://127.0.0.1:8089").rstrip("/")
ACTION_MCP_URL = os.getenv("ACTION_MCP_URL", "http://127.0.0.1:8093/mcp")
PUBLIC_BASE = os.getenv("PUBLIC_BASE", "http://127.0.0.1:8092").rstrip("/")

# Experiment parameters, not enterprise policy.
DRAFT_TTL_SECONDS = int(os.getenv("DRAFT_TTL_SECONDS", "900"))
CONFIRM_TTL_SECONDS = int(os.getenv("CONFIRM_TTL_SECONDS", "600"))
MODEL_TIMEOUT = float(os.getenv("MODEL_TIMEOUT", "120"))
TOOL_TIMEOUT = float(os.getenv("TOOL_TIMEOUT", "60"))
SUBMIT_TIMEOUT = float(os.getenv("SUBMIT_TIMEOUT", "20"))
MAX_STEPS = int(os.getenv("MAX_STEPS", "4"))
MAX_TOOL_CALLS = int(os.getenv("MAX_TOOL_CALLS", "6"))

# Failure injection for the simulated back office. Never enabled by default.
FAULT_FILE = Path(os.getenv("ACTION_FAULT_FILE", str(DATA_DIR / "fault-drop-once")))
FAULT_DELAY = float(os.getenv("ACTION_FAULT_DELAY", "45"))

API_PORT = int(os.getenv("API_PORT", "8092"))
MCP_PORT = int(os.getenv("MCP_PORT", "8093"))

DEFAULT_POLICY = os.getenv("DEFAULT_POLICY", "eval3-device")
DEFAULT_AS_OF = os.getenv("DEFAULT_AS_OF", "2026-03-01")
