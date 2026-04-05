# Hexcaliper — Session Handoff

**Date:** 2026-04-04
**Status:** All planned priorities complete. Roadmap fully closed including Phase 5 deep analysis integration.

---

## What Is Complete

### Phase 1 — Core GraphRAG
Document ingest, ChromaDB, SQLite, Ollama chat, concept extraction, graph indexing,
scoped vocabulary, conversation management, workspace (clients/projects), streaming chat.

### Phase 2 — Technical Library + Acquisition Queue
- Technical library router (`/library`) — CRUD + file storage, category browser (grouped by doc_type)
- Scrapers: Beckhoff, Allen Bradley, Siemens, Phoenix Contact, Danfoss, ABB, Yaskawa
- Acquisition queue (`/acquisition`) — pending → approve → scrape → complete lifecycle, SSE stream
- Acquisition → Escalation auto-fallback when scraper finds no results

### Phase 3 — Cloud Escalation
- Escalation queue (`/escalation`) — pending → approve → cloud call → complete, SSE stream
- Supports Anthropic and OpenAI via raw httpx (no SDK dependency)
- Privacy gate: `has_client_docs=true` always requires manual approval
- `AUTO_ESCALATE=true` env var for auto-approving public-only items
- Semantic cache (`escalation_cache` Chroma collection) — cosine distance ≤ 0.08 threshold
- "Escalate →" button on every assistant message in chat

### Phase 4 — Connections
- `connectors/mfiles.py` — M-Files REST (MFWS): auth, search, list, download
- `connectors/mfiles_indexer.py` — bulk vault index background job, SSE progress
- `connectors/sharepoint.py` — Microsoft Graph API: OAuth2 CC flow, list files, download
- `connectors/webdav.py` — Generic WebDAV/REST: none/basic/bearer auth, PROPFIND, download
- `/connections` router — CRUD + enable/disable/test/env-hint for all three types
- `POST /connections/mfiles/index` + `GET /connections/mfiles/index/stream`

### Phase 5 — Security & Access
- `crypto.py` — Fernet (AES-128-CBC + HMAC-SHA256) at-rest encryption for credentials
- `CREDENTIALS_KEY` env var — passphrase for key derivation; pass-through if unset
- `db.migrate_credentials_encryption()` — idempotent startup migration encrypts existing plaintext secrets
- `nginx/library.conf` — read-only public library subdomain config (port 8081)
- `X-Site-Mode: library` header — nginx signals library mode; API filters client docs and blocks writes
- `GET /site-config` — exposes `public_library_mode` flag to the frontend
- `PUBLIC_LIBRARY_MODE` env var — alternative to nginx header for standalone library deployments

### Phase 5b — Deep Analysis (merLLM batch integration)
- `routers/health.py` — three new batch proxy routes: `POST /batch/submit`, `GET /batch/status/{job_id}`, `GET /batch/results/{job_id}`
- `web/app.js` — `addDeepAnalysisBtn()` adds an ⚗ button to every completed assistant message; `_pollDeepAnalysis()` polls `/batch/status/{id}` every 5 s (max 360 polls), fetches result when complete, and inserts it as a new AI message
- `web/styles.css` — `.deep-btn` (hidden until hover, gold highlight, same pattern as `.escalate-btn`)
- `api/tests/test_batch_proxy.py` — 8 tests for all three proxy routes (success, 404/409, 502)

### Phase 6 — UX Improvements
- Status bar — persistent bottom bar replaces silent failures and `alert()` calls
- Document attribute editing — `PATCH /documents/{id}` + inline ✎ edit form in workbench table
- Library category browser — left tree groups by doc_type (Standards, Manuals, etc.) not manufacturer; right filter is a dynamic source/manufacturer dropdown
- `db.update_document()` — generic field-level UPDATE

---

## Key Files

```
api/
  app.py                — FastAPI bootstrap; LibraryModeMiddleware; /site-config
  config.py             — All env vars incl. CREDENTIALS_KEY, PUBLIC_LIBRARY_MODE, SP_*, WEBDAV_*
  crypto.py             — Fernet encrypt_secret/decrypt_secret/encrypt_config/decrypt_config
  db.py                 — SQLite layer; connections encrypt/decrypt via crypto.py; update_document()
  rag.py                — search_escalation_cache / store_escalation_cache
  routers/
    documents.py        — PATCH /documents/{id} (filename, doc_type, classification)
    tech_library.py     — public_only filter; 403 on add/delete in library mode
    connections.py      — KNOWN_TYPES incl. sharepoint + webdav; /mfiles/index SSE
    escalation.py       — semantic cache check before cloud call; auto-fallback from acquisition
    acquisition.py      — escalation fallback on empty scraper results
    chat.py             — doc_ids + has_client_docs in SSE done event
  connectors/
    mfiles.py           — MFilesConnector
    mfiles_indexer.py   — run_indexer(); SSE subscriber queue
    sharepoint.py       — SharePointConnector
    webdav.py           — WebDAVConnector
nginx/
  default.conf          — main app (port 8080)
  library.conf          — public library subdomain (port 8081)
web/
  app.js                — applySiteConfig(); _openWbEdit(); renderLibMfrTree (by doc_type)
  index.html            — status bar; library category tree header; workbench edit button
  styles.css            — wb-edit-form; lib-readonly-badge; lib-src-cell
```

---

## Database Schema (current)

All tables in `hexcaliper.db`:

| Table | Key columns |
|-------|-------------|
| `conversations` | id, user_email, title, model, messages (JSON) |
| `clients` | id, name |
| `projects` | id, name, client_id |
| `documents` | id, user_email, filename, scope_type, scope_id, doc_type, classification, summary |
| `library_items` | id, manufacturer, product_id, doc_type, version, filename, filepath, source, indexed |
| `acquisition_queue` | id, manufacturer, product_id, status, requested_at, approved_at, completed_at, error |
| `escalation_queue` | id, query_text, source_doc_ids, has_client_docs, status, response |
| `connections` | id, type, enabled, config (JSON, secrets Fernet-encrypted if CREDENTIALS_KEY set) |
| `nodes` / `edges` | GraphRAG concept graph |
| `concept_scope` | concept_label, scope_type, scope_id |

ChromaDB collections: `documents`, `library`, `escalation_cache`

---

## Startup Migrations (idempotent, run every boot)

1. `db.migrate_from_tinydb()` — one-time import from legacy TinyDB
2. `db.migrate_classification_column()` — adds `classification` to `documents` if absent
3. `db.migrate_library_source_column()` — adds `source` to `library_items` if absent
4. `db.migrate_credentials_encryption()` — encrypts plaintext secrets in `connections` if `CREDENTIALS_KEY` is set
5. `rag.migrate_legacy_scopes()` — backfills scope metadata in ChromaDB

---

## Privacy Architecture

| Classification | Storage | Escalation rule |
|---|---|---|
| `public` | Library or global docs | Auto-approved if `AUTO_ESCALATE=true` |
| `client` | Client/project docs or M-Files | Always held for manual approval |

**Auto-classification rules at ingest:**
- Source = M-Files → `client`
- Source = web scraper → `public`
- Scope = client or project → `client`
- Global + doc_type=standard → `public`; otherwise → `client`
- Post-upload editable via `PATCH /documents/{id}` (client/project-scoped docs locked to `client`)

---

## What Could Come Next

- **Encryption key rotation** — utility to re-encrypt `connections` config when `CREDENTIALS_KEY` changes
- **SharePoint / WebDAV indexers** — equivalent of `mfiles_indexer.py` for the other two connectors
- **M-Files incremental sync** — checksum-based delta re-index (currently full re-index only)
- **Library item attribute editing** — `PATCH /library/items/{id}` for manufacturer, product_id, doc_type, version (parallel to `PATCH /documents/{id}`)
- **Deep analysis history** — persist submitted batch jobs and completed results so they survive page reload (currently in-memory poll only)
- **Deep analysis on conversations** — currently triggers on individual messages; could be extended to submit entire conversation context
