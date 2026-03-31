"""
config.py — Runtime configuration for Hexcaliper.
All values read from environment variables. Follows hexcaliper-squire/api/config.py pattern.
"""
import os


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


OLLAMA_BASE_URL = _get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
DEFAULT_MODEL   = _get("DEFAULT_MODEL", "llama3:8b")
EMBED_MODEL     = _get("EMBED_MODEL", "nomic-embed-text")

MAX_INPUT_CHARS = int(_get("MAX_INPUT_CHARS", "20000"))
REQUEST_TIMEOUT = float(_get("REQUEST_TIMEOUT_SECONDS", "120"))
MAX_DOC_BYTES   = 20 * 1024 * 1024

DB_PATH         = _get("DB_PATH", "/app/data/hexcaliper.db")
CHROMA_PATH     = _get("CHROMA_PATH", "/app/data/chroma")
TINYDB_LEGACY   = _get("TINYDB_LEGACY_PATH", "/app/data/db.json")
