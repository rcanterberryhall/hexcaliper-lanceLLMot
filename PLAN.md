# Hexcaliper System Expansion Plan

## Vision

Hexcaliper is evolving from a GraphRAG chat tool for standards and contracts into a
full private knowledge platform with three integrated layers:

1. **Technical Document Library** — a curated, LLM-queryable store of public technical
   documentation (manufacturer manuals, datasheets, firmware notes, standards). Acquired
   automatically by the system as projects identify gaps. Accessible via a file-browser
   interface at `library.hexcaliper.com`.

2. **Client Workspace** — private client and project files (contracts, drawings, IO lists,
   specifications). Fully isolated from public data. Never shared. Cloud escalation requires
   explicit user approval.

3. **Intelligent Acquisition & Escalation** — the LLM can request new technical resources
   (web acquisition queue) and request help from cloud models (escalation queue). Both queues
   require user approval when sensitive context is involved. Approved items are processed
   automatically.

---

## Hardware Context

- Two Tesla P40 GPUs, 24 GB VRAM each (48 GB total)
- Primary deployment: 2× 34B models (one per GPU, parallel flows)
- Fallback: 1× 70B split across both GPUs for deep analysis
- All private data stays local. Cloud queries are opt-in, approval-gated, and sanitized.

---

## Privacy Architecture

### Document Classification

Every document in the system carries a `classification` field:

| Classification | Meaning |
|---|---|
| `public` | Technical library docs. Freely shareable. Eligible for auto cloud escalation. |
| `client` | Any client file. Never shared. Cloud escalation requires explicit user approval. |

**Auto-classification rules at ingestion:**
- Source = M-Files → always `client`, no override
- Source = web acquisition → always `public`
- Source = manual upload → prompt user, default to `client` (safer)
- Scope = client or project → `client` automatically

### Cloud Escalation Rules

- `public`-only context → escalation can proceed automatically (subject to confidence threshold)
- Any `client` document in context → escalation held in approval queue
- M-Files sourced content → escalation blocked entirely, not just queued
- User sees the full sanitized query text before approving
- User can edit the query before approving
- All escalations are logged permanently

### Shared FTP (future)

When `library.hexcaliper.com` is opened to external users:
- Only `public` / `classification: public` documents are visible
- `client` documents are absent from directory listings entirely — not locked, just not present
- No LLM query access for shared users
- Same codebase, different auth tier

---

## Current Codebase State (as of 2026-03-31)

### hexcaliper — main AI chat tool

**Stack:** FastAPI + SQLite + ChromaDB + Ollama

**Existing tables:**
- `conversations` — chat history, per user
- `clients` — organizational clients
- `projects` — scoped under clients
- `documents` — uploaded files with scope_type (global/client/project/session)
- `nodes` + `edges` — GraphRAG graph
- `concept_scope` — scoped concept vocabulary

**Existing routers:**
- `routers/health.py` — health check
- `routers/conversations.py` — conversation CRUD
- `routers/documents.py` — upload, list, delete, reindex
- `routers/library.py` — client and project management ← NAME CONFLICT (see below)
- `routers/chat.py` — chat with GraphRAG

**Existing document types (models.py DOC_TYPES):**
```python
"standard", "requirement", "theop", "fmea", "hazard_analysis",
"fat", "sat", "contract", "correspondence", "plc_code", "misc"
```

**Key config (config.py):**
- `DB_PATH` = `/app/data/hexcaliper.db`
- `CHROMA_PATH` = `/app/data/chroma`
- `OLLAMA_BASE_URL`, `DEFAULT_MODEL`, `EMBED_MODEL`

**Web UI:**
- Deep navy/indigo palette (`--bg: #0c0a18`, `--gold: #d4a017`, `--purple: #9b5de5`)
- Sidebar (conversations + documents) + main panel (Chat tab / Workbench tab)
- Workbench tab has client/project selector and document list

### hexcaliper-squire — project analysis tool

Separate service. Has GPU + CPU/RAM system meters. Not directly part of this plan
but may integrate with the library acquisition workflow in future.

---

## Schema Changes Required

### 1. Add `classification` to `documents`

```sql
ALTER TABLE documents ADD COLUMN classification TEXT NOT NULL DEFAULT 'client';
```

Migration: all existing documents get `classification = 'client'` (safe default).
Global-scoped documents that are clearly public (standards, etc.) can be reclassified
manually or via a migration script that checks `doc_type IN ('standard')`.

### 2. New table: `library_items`

The technical document store. Separate from `documents` because these are system-acquired,
not user-uploaded.

```sql
CREATE TABLE IF NOT EXISTS library_items (
    id           TEXT PRIMARY KEY,
    manufacturer TEXT NOT NULL,
    product_id   TEXT NOT NULL,
    doc_type     TEXT NOT NULL,   -- manual|datasheet|firmware_notes|app_note|mounting
    version      TEXT,
    filename     TEXT NOT NULL,
    filepath     TEXT NOT NULL,   -- /app/data/library/beckhoff/EL1008/...
    source_url   TEXT,            -- origin URL, for update checks
    checksum     TEXT,            -- SHA-256 of file content
    indexed      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_library_mfr     ON library_items(manufacturer);
CREATE INDEX IF NOT EXISTS idx_library_product ON library_items(manufacturer, product_id);
```

Library items are always `classification: public`. They live at
`/app/data/library/{manufacturer}/{product_id}/{filename}` on disk.

### 3. New table: `acquisition_queue`

```sql
CREATE TABLE IF NOT EXISTS acquisition_queue (
    id           TEXT PRIMARY KEY,
    manufacturer TEXT NOT NULL,
    product_id   TEXT NOT NULL,
    doc_type     TEXT,
    source_url   TEXT,             -- known URL, or null if model only knows product_id
    reason       TEXT,             -- human-readable: why the model wants this
    project_id   TEXT,             -- which project triggered this request
    status       TEXT NOT NULL DEFAULT 'pending_approval',
    requested_at TEXT NOT NULL,
    approved_at  TEXT,
    completed_at TEXT,
    error        TEXT
);
-- status values: pending_approval | approved | in_progress | complete | failed | rejected
CREATE INDEX IF NOT EXISTS idx_acq_status ON acquisition_queue(status);
```

### 4. New table: `escalation_queue`

```sql
CREATE TABLE IF NOT EXISTS escalation_queue (
    id              TEXT PRIMARY KEY,
    query_text      TEXT NOT NULL,           -- sanitized text to send to cloud
    source_doc_ids  TEXT NOT NULL DEFAULT '[]',  -- JSON array of doc IDs
    has_client_docs INTEGER NOT NULL DEFAULT 0,  -- 1 = requires approval
    conversation_id TEXT,
    status          TEXT NOT NULL DEFAULT 'pending_approval',
    requested_at    TEXT NOT NULL,
    approved_at     TEXT,
    completed_at    TEXT,
    response        TEXT                     -- cloud model response, stored locally
);
-- status values: pending_approval | approved | in_progress | complete | failed | rejected
-- has_client_docs = 0 → can auto-send (if confidence threshold met)
-- has_client_docs = 1 → always held for approval
CREATE INDEX IF NOT EXISTS idx_esc_status ON escalation_queue(status);
```

### 5. New table: `connections`

```sql
CREATE TABLE IF NOT EXISTS connections (
    id      TEXT PRIMARY KEY,
    type    TEXT NOT NULL UNIQUE,   -- mfiles | web_scraper
    enabled INTEGER NOT NULL DEFAULT 0,
    config  TEXT NOT NULL DEFAULT '{}'  -- JSON, encrypted at rest (future)
);
```

M-Files config shape:
```json
{
  "vault_url": "https://...",
  "auth_type": "userpass",
  "username": "...",
  "password": "..."
}
```

---

## API Changes Required

### Rename: `/library` → `/workspace`

`routers/library.py` currently handles clients and projects. Rename the prefix to
`/workspace` to free the `/library` namespace for the technical document store.

**Files to update:**
- `api/routers/library.py` — change `prefix="/library"` to `prefix="/workspace"`
- `api/app.py` — update router import/include comment
- `web/app.js` — update all fetch calls from `/library/clients` → `/workspace/clients`
  and `/library/projects` → `/workspace/projects`

### New router: `routers/tech_library.py`

Prefix: `/library`

Endpoints:
```
GET    /library/items                     list all library items (with filters)
GET    /library/items/{id}/download       download a file
POST   /library/items                     manually add a document to the library
DELETE /library/items/{id}                remove from library and disk
GET    /library/gaps?project_id=...       what's missing for a project
```

### New router: `routers/acquisition.py`

Prefix: `/acquisition`

```
GET    /acquisition/queue                 list all queue items (filterable by status)
POST   /acquisition/queue                 add item to queue (LLM or manual)
PATCH  /acquisition/queue/{id}/approve    approve → moves to in_progress
PATCH  /acquisition/queue/{id}/reject     reject
POST   /acquisition/queue/{id}/retry      retry a failed item
GET    /acquisition/stream                SSE stream for real-time progress
```

### New router: `routers/escalation.py`

Prefix: `/escalation`

```
GET    /escalation/queue                  list pending/recent escalations
POST   /escalation/queue                  LLM submits a cloud query request
PATCH  /escalation/queue/{id}/approve     user approves (optionally with edited query)
PATCH  /escalation/queue/{id}/reject      user rejects
GET    /escalation/stream                 SSE stream for status updates
```

### New router: `routers/connections.py`

Prefix: `/connections`

```
GET    /connections                       list connections and status
PUT    /connections/{type}                configure a connection
POST   /connections/{type}/test           test connectivity
DELETE /connections/{type}                remove configuration
```

### Update: `routers/documents.py`

- Add `classification` field to upload endpoint (optional param, defaults to `client`)
- Auto-set `classification = 'client'` when `scope_type` is client or project
- Return `classification` in document list responses

### Update: `models.py`

Add to `DOC_TYPES`:
```python
"technical_manual", "datasheet", "firmware_notes", "app_note"
```

---

## Web Scrapers

Each manufacturer gets its own scraper module. All share a base class.

**Location:** `api/scrapers/`

```
api/scrapers/
    __init__.py
    base.py          -- download(), dedup(), rate_limit(), metadata, retry
    beckhoff.py      -- product pages + InfoSys portal
    allen_bradley.py -- Rockwell Automation portal
    siemens.py       -- Industry Online Support
    danfoss.py
```

`base.py` responsibilities:
- Rate limiting (configurable, default 2 req/s per domain)
- SHA-256 dedup (skip if checksum already in library_items)
- Version detection (compare to existing entry by manufacturer+product_id+doc_type)
- Retry with exponential backoff (3 attempts)
- User-agent: `hexcaliper-library/1.0`
- Stores files to `/app/data/library/{manufacturer}/{product_id}/`
- On completion: inserts/updates `library_items`, triggers re-index

**Beckhoff scraper specifics:**
- Product page pattern: `beckhoff.com/en-us/products/.../{product_id.lower()}`
- Falls back to InfoSys: `infosys.beckhoff.com`
- Looks for PDF links with text containing: manual, documentation, datasheet, mounting
- Handles redirect to product-specific page for EL/EK/EP series

---

## LLM Tools (for local model to call)

The local model gets these tools via function-calling or structured prompting:

```python
search_library(query: str, manufacturer: str = None, product_id: str = None,
               doc_type: str = None) -> list[dict]

list_library_gaps(project_id: str) -> list[dict]

request_acquisition(manufacturer: str, product_id: str, doc_type: str = None,
                    source_url: str = None, reason: str) -> str

request_escalation(query_text: str, source_doc_ids: list[str]) -> str
    # returns: "queued:{id}" or "blocked:mfiles_source"

check_queue_status(queue: str, item_id: str) -> dict
    # queue: "acquisition" | "escalation"
```

---

## UI Changes

### Navigation

Add **Library** as a third tab in the main header tab bar alongside Chat and Workbench.

```
[ Chat ]  [ Workbench ]  [ Library ]
```

Library tab has three sub-views:
```
[ Browse ]  [ Acquisition ]  [ Connections ]
```

### Library › Browse

Two-panel layout:
- Left: manufacturer tree (collapsible, matches sidebar style)
- Right: document list with columns: name, doc_type badge, version, date, indexed dot, download arrow

No inline PDF preview. Download arrow only.

Classification badges on all documents:
- `public` docs: no badge (default, uncluttered)
- `client` docs: small gold lock icon — subtle but unambiguous

### Library › Acquisition

Two columns:
- **Active** (left): real-time progress via SSE. Items show progress bar, completion checkmark, or error with retry button. Auto-scrolls.
- **Pending Approval** (right): queued items waiting for user action. Each item shows:
  - Manufacturer + product ID
  - Reason (what triggered the request, which project)
  - Source URL if known; paste field if not
  - `[ Approve ]` `[ Reject ]`
  - Paywall/login cases: URL paste field + `[ Add ]`
  - `[ Approve All ]` button at bottom

Also contains **Escalation** sub-section (or separate column):
- Pending cloud queries waiting for approval
- Each shows: contributing source docs with classification badges, full sanitized query text (editable inline), `[ Approve ]` `[ Edit → Approve ]` `[ Reject ]`

### Library › Connections

```
── Technical Sources ──────────────────────────────────────────
Web Acquisition       ● Active         [ Configure ]
  Rate limit: 2 req/s   User-agent: hexcaliper-library/1.0

── Private / Client Sources ───────────────────────────────────
M-Files               ○ Not connected  [ Configure ]
⚠ Documents from M-Files are always treated as client-confidential.
  Never used in automated cloud queries.
```

### Workbench Tab Updates

Project view gains a **Library Coverage** section:
```
Library Coverage for: [Project Name]
  ✓ EL1008   Beckhoff Digital Input    v2.5   indexed
  ✓ EL2008   Beckhoff Digital Output   v2.5   indexed
  ⟳ EL3024   Beckhoff Analog Input     —      acquiring...
  ✗ EK1100   Beckhoff EtherCAT Coupler —      not found
  [ Acquire missing ]  [ Re-index ]
```

### Chat View Updates

Citations that draw from library items show a `lib` tag.
Citations from client documents show a lock icon.
If a response draws from M-Files content, a persistent amber banner:
`⚠ This response includes context from confidential client documents.`

---

## M-Files Integration

**Direction (phase 1):** Read-only. Pull documents from M-Files vault into local context.
**Direction (phase 2, future):** Write-back acquired tech docs into M-Files with metadata.

**Scope control:** Configure which vault object types / metadata classes the model
is allowed to pull from. Prevents accidental indexing of HR/financial records.

**Data flow:**
- M-Files documents never enter the public technical library
- They are indexed into a separate Chroma collection (`mfiles_private`)
- Never mixed with `library_items` or public global documents
- Every retrieval is logged: document ID, conversation ID, timestamp

**Audit log table (future):**
```sql
CREATE TABLE mfiles_access_log (
    id              TEXT PRIMARY KEY,
    mfiles_doc_id   TEXT NOT NULL,
    conversation_id TEXT,
    accessed_at     TEXT NOT NULL
);
```

---

## library.hexcaliper.com

Separate nginx-served interface. Same hexcaliper visual identity.

**Auth tiers:**
- Owner (you): full access — Browse, Acquisition, Connections, download all
- Shared user (future): Browse only, `public` documents only, download only

The shared tier is the same frontend with Acquisition and Connections tabs hidden,
and `client` documents filtered server-side. No separate codebase needed.

---

## Implementation Order

### Phase 1 — Foundation
1. Schema migrations: `classification` column, `library_items`, `acquisition_queue`,
   `escalation_queue`, `connections` tables
2. Rename `/library` → `/workspace` in router, app.py, and all JS fetch calls
3. Add `classification` to document upload/list API
4. New `routers/tech_library.py` with Browse endpoints
5. UI: Library tab + Browse sub-view

### Phase 2 — Acquisition
6. `api/scrapers/base.py` + `beckhoff.py`
7. `routers/acquisition.py` with queue management + SSE stream
8. UI: Acquisition sub-view (real-time Active column + Pending Approval column)
9. LLM tools: `request_acquisition`, `list_library_gaps`

### Phase 3 — Escalation
10. Sanitization layer (NER scrubbing via GLiNER or spaCy)
11. `routers/escalation.py` with queue management
12. UI: Escalation section in Acquisition view
13. LLM tool: `request_escalation`
14. Cloud connector (Anthropic SDK, using Claude as default)

### Phase 4 — M-Files
15. `routers/connections.py`
16. M-Files REST client (read-only)
17. Separate Chroma collection for M-Files content
18. Access log table
19. UI: Connections sub-view

### Phase 5 — library.hexcaliper.com
20. Nginx config for subdomain
21. Auth tier separation (owner vs. shared)
22. Shared FTP view (public documents only)

---

## File Layout After Changes

```
hexcaliper/
  api/
    app.py                   (updated: new routers added)
    config.py                (updated: LIBRARY_PATH added)
    db.py                    (updated: new tables, classification migration)
    models.py                (updated: new DOC_TYPES)
    routers/
      health.py
      conversations.py
      documents.py           (updated: classification field)
      library.py             (renamed prefix /workspace)
      chat.py
      tech_library.py        (new)
      acquisition.py         (new)
      escalation.py          (new)
      connections.py         (new)
    scrapers/
      __init__.py            (new)
      base.py                (new)
      beckhoff.py            (new)
      allen_bradley.py       (new)
    sanitizer.py             (new: NER-based scrubbing before escalation)
  web/
    index.html               (updated: Library tab, coverage in Workbench)
    app.js                   (updated: library UI, acquisition/escalation queues)
    styles.css               (updated: new badges, queue components)
  data/
    hexcaliper.db
    chroma/
    library/                 (new: flat file store for tech docs)
      beckhoff/
      allen-bradley/
      standards/
```

---

## Open Questions / Future Decisions

- **Semantic escalation cache:** Before sending any cloud query, check if a semantically
  similar question was already answered. Requires embedding the query and searching a
  local cache. Reduces cloud usage significantly over time.

- **Update checker:** Background job that periodically re-checks `source_url` for
  library items and flags when newer versions exist. Especially important for firmware
  notes.

- **hexcaliper-squire integration:** Squire currently does project analysis independently.
  It could trigger library gap detection and feed the acquisition queue. Deferred until
  Phase 2 is stable.

- **Additional scrapers:** Allen Bradley (Rockwell portal requires login for some docs),
  Siemens (Industry Online Support), Danfoss, ABB, Yaskawa. Each needs its own module
  and some may need the approval queue for login-gated content.

- **Encryption at rest for connections table:** M-Files credentials stored in `connections`
  should be encrypted. Consider using the system keyring or a local secrets file outside
  the database.
