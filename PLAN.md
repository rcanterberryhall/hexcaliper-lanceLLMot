# Hexcaliper — Development Plan

## Status: All Priorities Complete ✓

---

## Completed Phases

### Phase 1 — Core GraphRAG ✓
- SQLite WAL database replacing TinyDB
- ChromaDB vector store with persistent embeddings
- GraphRAG: concept/entity extraction, graph nodes + edges, graph-aware retrieval
- Scoped concept vocabulary
- Multi-scope documents (global / client / project / session)
- Classification field on documents (`public` / `client`)
- Workspace router (`/workspace`): clients and projects
- Streaming chat with SSE, thinking model support, tool calling, web search, URL fetch
- Multi-model switching, model status indicator, VRAM pre-warm
- Live GPU + system meters (dual P40)

### Phase 2 — Technical Library + Acquisition Queue ✓
- `library_items` table and `/library` router
- Scraper base class: rate limiting, retry, SHA-256 dedup, file storage
- Scrapers: Beckhoff, Allen Bradley, Siemens, Phoenix Contact, Danfoss, ABB, Yaskawa
- Acquisition queue (`/acquisition`): approval gating, SSE progress stream, retry
- Library browser UI: category tree (grouped by doc_type), source filter, indexed status
- Acquisition sub-tab UI: two-panel Active / Pending Approval with Approve All

### Phase 3 — Cloud Escalation ✓
- Escalation queue (`/escalation`): privacy gate, auto-approve path, SSE stream
- Anthropic + OpenAI support via raw httpx (no SDK dependency)
- `AUTO_ESCALATE` env var for public-only auto-approval
- Semantic escalation cache (`escalation_cache` Chroma collection, cosine distance ≤ 0.08)
- Acquisition → Escalation auto-fallback when scraper returns no results
- "Escalate →" button on assistant messages in chat (with privacy badge)
- Escalation sub-tab UI: pending count badge, public/client badges, response inline, cached indicator

### Phase 4 — Connections ✓
- `connectors/mfiles.py`: async M-Files REST (MFWS) — auth, search, list, download
- `connectors/mfiles_indexer.py`: bulk vault index, SSE progress stream
- `connectors/sharepoint.py`: Microsoft Graph API, OAuth2 client credentials, list + download
- `connectors/webdav.py`: generic WebDAV/REST, none/basic/bearer auth, PROPFIND + download
- `/connections` router: CRUD, enable/disable, test, env-var hint for all three types
- `POST /connections/mfiles/index` + `GET /connections/mfiles/index/stream`
- Connections sub-tab UI: per-connection config form, select fields, Index Vault button

### Phase 5 — Security & Shared Access ✓
- `crypto.py`: Fernet (AES-128-CBC + HMAC-SHA256) at-rest credential encryption
- `CREDENTIALS_KEY` env var: passphrase for key derivation; pass-through when unset
- `db.migrate_credentials_encryption()`: idempotent startup migration
- `nginx/library.conf`: read-only public library subdomain (port 8081)
- `X-Site-Mode: library` header + `LibraryModeMiddleware`: request-level mode switching
- `GET /site-config`: exposes `public_library_mode` to the frontend
- `PUBLIC_LIBRARY_MODE` env var: standalone library deployment without nginx header
- Frontend `applySiteConfig()`: hides Chat/Workbench/Acquisition/Escalation/Connections in library mode

### Phase 6 — UX Polish ✓
- Status bar: persistent bottom bar with spinner; replaces all `alert()` and silent failures
- Document attribute editing: `PATCH /documents/{id}` + inline ✎ edit form in workbench table (filename, doc_type, classification)
- Library category browser: left tree grouped by doc_type; right filter is dynamic manufacturer dropdown
- `db.update_document()`: generic field-level UPDATE

---

## Privacy Architecture (Reference)

### Document Classification

| Classification | Meaning |
|---|---|
| `public` | Technical library docs (manufacturer manuals, standards). Auto-escalation eligible. |
| `client` | Any client or project file. Never shared. Cloud escalation always requires explicit approval. |

### Auto-classification rules at ingestion
- Source = M-Files → always `client`
- Source = web acquisition scraper → always `public`
- Source = manual upload → defaults to `client`; global + standard → `public`
- Scope = client or project → `client` automatically
- Editable post-upload via `PATCH /documents/{id}` (client/project-scoped docs locked to `client`)

### Cloud escalation rules
- `public`-only context + `AUTO_ESCALATE=true` → auto-approved
- Any `client` document in context → held for explicit approval
- Semantic cache checked first → cached hits bypass cloud entirely

---

## File Layout

```
hexcaliper/
  api/
    app.py                   FastAPI bootstrap — LibraryModeMiddleware, /site-config, all routers
    config.py                All env vars
    crypto.py                Fernet credential encryption helpers
    db.py                    SQLite WAL — all tables, CRUD, migrations, encrypt/decrypt connections
    rag.py                   ChromaDB ingest/search + escalation_cache collection
    graph.py                 GraphRAG concept graph
    extractor.py             LLM concept/entity extraction
    models.py                Pydantic models, DOC_TYPES
    parser.py                Document text extraction
    ollama.py                Ollama API helpers
    web_search.py            DuckDuckGo search
    web_fetch.py             URL content fetch
    requirements.txt         Python dependencies (incl. cryptography)
    routers/
      health.py
      conversations.py
      documents.py           PATCH /documents/{id} for attribute editing
      library.py             /workspace — clients & projects
      tech_library.py        /library — public_only filter, library mode 403s
      acquisition.py         /acquisition — scrape queue + SSE + escalation fallback
      escalation.py          /escalation — cloud queue + SSE + semantic cache
      connections.py         /connections — M-Files, SharePoint, WebDAV
      chat.py                doc_ids + has_client_docs in SSE done event
    scrapers/
      __init__.py            REGISTRY + get_scraper()
      base.py                BaseScraper, rate limit, retry, dedup
      beckhoff.py
      allen_bradley.py
      siemens.py
      phoenix_contact.py
      danfoss.py
      abb.py
      yaskawa.py
    connectors/
      __init__.py
      mfiles.py              MFilesConnector — auth, search, list, download
      mfiles_indexer.py      Background vault indexer + SSE subscriber queue
      sharepoint.py          SharePointConnector — Graph API, OAuth2 CC
      webdav.py              WebDAVConnector — PROPFIND, Basic/Bearer/none
  web/
    index.html               Status bar, library category tree, workbench edit button
    app.js                   applySiteConfig(), _openWbEdit(), renderLibMfrTree (by doc_type)
    styles.css               wb-edit-form, lib-readonly-badge, lib-src-cell
  nginx/
    default.conf             Main app (port 8080)
    library.conf             Public library subdomain (port 8081)
  data/                      (runtime, bind-mounted)
    hexcaliper.db
    chroma/
    library/
      {manufacturer}/
        {product_id}/
          {filename}.pdf
  docker-compose.yml         Includes commented nginx-library service
  README.md
  PLAN.md
  HANDOFF.md
```
