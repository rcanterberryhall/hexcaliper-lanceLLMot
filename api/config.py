"""
config.py — Runtime configuration for Hexcaliper.
All values read from environment variables. Follows hexcaliper-squire/api/config.py pattern.
"""
import os


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


OLLAMA_BASE_URL = _get("OLLAMA_BASE_URL", "http://host.docker.internal:11400")
MERLLM_URL      = _get("MERLLM_URL",      "http://host.docker.internal:11400")
DEFAULT_MODEL   = _get("DEFAULT_MODEL", "qwen3:32b")
ANALYSIS_MODEL  = _get("ANALYSIS_MODEL", "") or DEFAULT_MODEL
EMBED_MODEL     = _get("EMBED_MODEL", "nomic-embed-text")

MAX_INPUT_CHARS = int(_get("MAX_INPUT_CHARS", "20000"))
REQUEST_TIMEOUT = float(_get("REQUEST_TIMEOUT_SECONDS", "120"))
MAX_DOC_BYTES   = 20 * 1024 * 1024

DB_PATH         = _get("DB_PATH", "/app/data/lancellmot.db")
CHROMA_PATH     = _get("CHROMA_PATH", "/app/data/chroma")
TINYDB_LEGACY   = _get("TINYDB_LEGACY_PATH", "/app/data/db.json")
LIBRARY_PATH    = _get("LIBRARY_PATH", "/app/data/library")

# ── Cloud escalation ──────────────────────────────────────────────────────────
# ESCALATION_PROVIDER: "anthropic" | "openai"
# AUTO_ESCALATE: if "true", public-only escalations are approved automatically.
ESCALATION_PROVIDER = _get("ESCALATION_PROVIDER", "anthropic")
ESCALATION_API_KEY  = _get("ESCALATION_API_KEY", "")
ESCALATION_MODEL    = _get("ESCALATION_MODEL", "claude-haiku-4-5-20251001")
AUTO_ESCALATE       = _get("AUTO_ESCALATE", "false").lower() == "true"

# ── M-Files connection ────────────────────────────────────────────────────────
MFILES_HOST  = _get("MFILES_HOST", "")   # e.g. "mfiles.example.com"
MFILES_VAULT = _get("MFILES_VAULT", "")  # Vault GUID
MFILES_USER  = _get("MFILES_USER", "")
MFILES_PASS  = _get("MFILES_PASS", "")

# ── SharePoint connection ─────────────────────────────────────────────────────
SP_TENANT_ID  = _get("SP_TENANT_ID", "")   # Azure AD tenant ID or domain
SP_CLIENT_ID  = _get("SP_CLIENT_ID", "")   # App registration client ID
SP_SITE_URL   = _get("SP_SITE_URL", "")    # e.g. "https://myorg.sharepoint.com/sites/mysite"
# SP_CLIENT_SECRET is never pre-filled — user must enter it in the UI.

# ── WebDAV / generic REST connection ─────────────────────────────────────────
WEBDAV_URL      = _get("WEBDAV_URL", "")       # e.g. "https://dav.example.com"
WEBDAV_USERNAME = _get("WEBDAV_USERNAME", "")

# ── Credential encryption ─────────────────────────────────────────────────────
# Set to a strong random value (e.g. a UUID4) to encrypt connection credentials
# (passwords, API keys, bearer tokens) at rest in the SQLite database.
# Leave unset to disable encryption (pass-through mode — backward compatible).
# Changing or removing this key after credentials have been stored will make
# them unreadable; re-enter all credentials if you rotate the key.
CREDENTIALS_KEY = _get("CREDENTIALS_KEY", "")

# ── CORS ──────────────────────────────────────────────────────────────────────
# Comma-separated list of allowed origins, or "*" for all (dev only).
# Defaults to localhost ports used by the bundled nginx.
_cors_raw   = _get("CORS_ORIGINS", "http://localhost:8080,http://localhost:8081")
CORS_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()]

# ── Public library mode ───────────────────────────────────────────────────────
# When true (or when the nginx X-Site-Mode: library header is present), the API
# serves only public library documents and blocks all write operations.
# Used by the library.hexcaliper.com subdomain.
PUBLIC_LIBRARY_MODE = _get("PUBLIC_LIBRARY_MODE", "false").lower() == "true"
