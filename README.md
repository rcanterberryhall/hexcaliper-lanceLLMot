# Hexcaliper — Self-Hosted AI Knowledge Workbench

A self-hosted, privacy-first AI workbench for industrial automation engineering.
Combines local LLM chat (via [Ollama](https://ollama.com)), GraphRAG over your own documents,
an auto-acquiring technical document library, cloud escalation, and external system connections —
all running on your hardware.

No data leaves your machine unless you explicitly approve it.

---

## Features

### Chat & RAG
- **Multi-model chat** — switch between any model pulled into your local Ollama instance
- **GraphRAG** — documents are indexed into a concept graph; retrieval uses both vector similarity and graph traversal for richer context
- **Retrieval attribution** — after each response, a collapsible "Sources" section shows which documents, chunk snippets, relevance scores, and graph nodes informed the answer; "No documents matched" is shown explicitly when RAG returned zero results
- **Active scope indicator** — a persistent badge in the chat header shows the current scope (Global / Client / Project / Session), the document count, and a warning when the scope is empty
- **Scoped documents** — global, client, project, or session scope; each conversation only sees what it should
- **Library-to-conversation bridge** — attach technical library documents directly to the active conversation from the library browser; reciprocally, browse the library from within a chat session
- **Document summaries** — every uploaded document gets a model-generated summary injected into every chat so the model always knows what exists
- **System prompt management** — create, name, and save reusable system prompts; assign one to any conversation via the sidebar; the assigned prompt is prepended as a system message on every chat request
- **Conversation export** — download any conversation as Markdown (with title, model, system prompt, and timestamped messages) or JSON (full structured payload including system prompt object and message array)
- **Streaming responses** — tokens stream to the browser via SSE
- **RAG status reporting** — the first SSE chunk includes a `rag_status` field (`ok` or `error`) with document and graph node counts; failures are logged server-side and surfaced to the client
- **Thinking model support** — extended reasoning tokens from DeepSeek-R1, QwQ, etc. are shown in a collapsible section
- **Autonomous web search** — tool-capable models (Qwen3, Qwen2.5, Mistral) search DuckDuckGo automatically when current information is needed
- **URL fetching** — paste a URL into your message and the page content is fetched and included as context
- **Escalate to Cloud** — each assistant response has an "Escalate →" button to send the query to a cloud model (Anthropic/OpenAI) for a second opinion; held for manual approval if client documents are in context
- **Copyright acknowledgement** — a compliance notice is shown once per browser session before the chat input is enabled, reminding users that uploaded and discussed material may be copyrighted

### Technical Library
- **Category browser** — documents grouped by type (Standards, Manuals, Datasheets, etc.) with a per-source filter; not all library documents are manufacturer docs
- **Manufacturer scraper registry** — automatic documentation acquisition for Beckhoff, Allen Bradley / Rockwell, Siemens, Phoenix Contact, Danfoss, ABB, Yaskawa
- **M-Files vault indexing** — one-click bulk import from a connected M-Files vault; SSE progress stream
- **SHA-256 dedup** — files are never downloaded twice regardless of URL
- **Rate limiting & retry** — per-domain rate limiting (0.5 s gap) and exponential-backoff retry on all scrapers

### Acquisition Queue
- **Approval-gated scraping** — every web acquisition requires explicit user approval before any network activity
- **Real-time progress** — Server-Sent Events stream shows file-by-file progress
- **Approve All** — batch-approve pending items in one click
- **Escalation fallback** — if a scraper finds nothing, an escalation is automatically queued as a last resort

### Cloud Escalation
- **Privacy-aware** — if the query context contains any client documents, cloud escalation is held for explicit approval regardless of settings
- **Auto-escalate** — public-only queries can be auto-approved via `AUTO_ESCALATE=true`
- **Semantic cache** — before calling the cloud, a local ChromaDB collection is checked for a semantically similar previous response (cosine distance ≤ 0.08); cached hits are returned instantly
- **Anthropic & OpenAI** — configurable via `ESCALATION_PROVIDER` env var; no SDK dependency (raw httpx)
- **Response stored locally** — cloud responses are saved to the DB and displayed inline

### Connections
- **M-Files** — connect to an M-Files vault via MFWS REST API; test connectivity and bulk-index from the UI
- **SharePoint** — Microsoft SharePoint via Graph API (OAuth 2.0 client credentials)
- **WebDAV / REST** — generic WebDAV or REST file server (none, Basic, or Bearer auth)
- **Env-var pre-fill** — `MFILES_*`, `SP_*`, and `WEBDAV_*` env vars auto-populate config forms
- **Encrypted credentials** — set `CREDENTIALS_KEY` to encrypt all stored passwords and API keys with Fernet (AES-128-CBC + HMAC-SHA256)
- **Credential masking** — passwords/tokens are never returned from the API; the UI preserves the placeholder on save

### Workbench
- **Document attribute editing** — after upload, click the ✎ button on any document to edit its filename, doc type, and classification inline
- **Scope hierarchy** — global, client, project, and session scopes; project view inherits all parent-scope documents

### System
- **Live GPU meter** — real-time utilisation and VRAM per card via NVML (dual P40 supported)
- **Request logging** — HTTP request logging middleware records method, path, status code, duration, and user email at INFO/WARNING/ERROR levels
- **Multi-user ready** — user isolation via Cloudflare Access header; falls back to `local@dev`
- **Status bar** — persistent bottom status bar shows upload feedback, indexer progress, and escalation state
- **Model status indicator** — shows whether a model is loaded in VRAM; Load button pre-warms it
- **Configurable CORS** — allowed origins set via `CORS_ORIGINS` env var; supports wildcard for development
- **Escalation cache eviction** — the ChromaDB escalation semantic cache is capped at `MAX_ESCALATION_CACHE_SIZE` entries; oldest entries are evicted automatically
- **SQLite + WAL** — all metadata in a single WAL-mode SQLite database; no Postgres required
- **ChromaDB** — persistent vector store for document chunks, library content, and escalation cache

---

## Architecture

```
Browser
  └── nginx (:8080)          ← main app
  └── nginx (:8081, opt.)    ← library.hexcaliper.com public view
        └── /api/* → FastAPI/uvicorn (:8000)
                       ├── Ollama              (:11434, host)
                       ├── ChromaDB            (./data/chroma)
                       ├── SQLite              (./data/lancellmot.db)
                       ├── Library files       (./data/library/)
                       ├── DuckDuckGo          (web search, no key)
                       ├── Manufacturer sites  (scraper, approval-gated)
                       ├── M-Files / SharePoint / WebDAV  (connections)
                       └── Cloud API           (Anthropic/OpenAI, approval-gated)
```

| Component | Technology |
|-----------|------------|
| Frontend | Vanilla JS + CSS, served by nginx |
| API | Python 3.12, FastAPI, uvicorn |
| Vector DB | ChromaDB (persistent, cosine similarity) |
| Embeddings | Ollama (`nomic-embed-text` by default) |
| Metadata DB | SQLite (WAL mode) |
| File storage | Local filesystem under `./data/library/` |
| Credential encryption | Fernet / `cryptography` package |
| Container orchestration | Docker Compose |

---

## Prerequisites

> Tested on **Ubuntu 24.04.4 LTS**. macOS and Windows notes are at the end of this file.

### Docker

```bash
docker --version          # 24+ recommended
docker compose version    # v2.x
```

Install: [docs.docker.com/engine/install](https://docs.docker.com/engine/install/)

### Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve   # if not already running as a system service
```

Pull the models you need:

```bash
ollama pull qwen2.5:32b        # recommended chat model for this hardware
ollama pull nomic-embed-text   # required for RAG embeddings
```

### NVIDIA GPU (optional)

The GPU meter requires NVML. If you don't have an NVIDIA GPU, skip this step and
remove the `devices` block from `docker-compose.yml`.

```bash
# Find the NVML library
find /usr -name "libnvidia-ml.so.1" 2>/dev/null

# Copy it into the api/ directory (Dockerfile copies it into the container)
cp /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1 api/
```

For Docker GPU passthrough, install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html):

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

---

## Quickstart

```bash
git clone <repo-url> hexcaliper
cd hexcaliper

# Copy NVML library (skip if no GPU)
cp /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1 api/

# Build and start
docker compose up --build -d

# Open in browser
xdg-open http://localhost:8080
```

Interactive API docs: `http://localhost:8000/docs`

---

## Configuration

All tunables are set via environment variables in `docker-compose.yml`:

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama API endpoint |
| `DEFAULT_MODEL` | `llama3:8b` | Model selected on first load |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model |
| `EXTRACT_MODEL` | _(same as ANALYSIS_MODEL)_ | Model used for graph/concept extraction; set to a smaller model for faster extraction at lower quality |
| `MAX_INPUT_CHARS` | `20000` | Max characters per user message |
| `REQUEST_TIMEOUT_SECONDS` | `120` | Ollama request timeout |
| `DB_PATH` | `/app/data/lancellmot.db` | SQLite database path |
| `CHROMA_PATH` | `/app/data/chroma` | ChromaDB persistence path |
| `LIBRARY_PATH` | `/app/data/library` | Technical document file store |

### Cloud Escalation

| Variable | Default | Description |
|----------|---------|-------------|
| `ESCALATION_PROVIDER` | `anthropic` | Cloud provider: `anthropic` or `openai` |
| `ESCALATION_API_KEY` | _(empty)_ | API key for the cloud provider |
| `ESCALATION_MODEL` | `claude-haiku-4-5-20251001` | Model to use for escalation |
| `AUTO_ESCALATE` | `false` | Set to `true` to auto-approve public-only escalations |

### Connections

| Variable | Default | Description |
|----------|---------|-------------|
| `MFILES_HOST` | _(empty)_ | M-Files server hostname (e.g. `mfiles.example.com`) |
| `MFILES_VAULT` | _(empty)_ | Vault GUID |
| `MFILES_USER` | _(empty)_ | M-Files username |
| `MFILES_PASS` | _(empty)_ | M-Files password (used only as env-var hint; store via UI) |
| `SP_TENANT_ID` | _(empty)_ | SharePoint Azure AD tenant ID |
| `SP_CLIENT_ID` | _(empty)_ | SharePoint app registration client ID |
| `SP_SITE_URL` | _(empty)_ | SharePoint site URL |
| `WEBDAV_URL` | _(empty)_ | WebDAV/REST base URL |
| `WEBDAV_USERNAME` | _(empty)_ | WebDAV username |

When the `*_HOST`/`*_URL` env vars are set, the Connections UI pre-populates the config form
(passwords/secrets must always be entered manually in the UI).

### Security

| Variable | Default | Description |
|----------|---------|-------------|
| `CREDENTIALS_KEY` | _(empty)_ | Passphrase for Fernet credential encryption at rest. Set to any strong random value (e.g. a UUID4). Leave unset to store credentials as plain text (backward compatible). |

> **Key rotation warning:** changing or removing `CREDENTIALS_KEY` after credentials have been
> stored will make them unreadable. Re-enter all connection credentials via the UI if you rotate the key.

### CORS

| Variable | Default | Description |
|----------|---------|-------------|
| `CORS_ORIGINS` | `http://localhost:8080,http://localhost:8081` | Comma-separated list of allowed origins. Set to `*` to allow all origins (development only — a warning is logged). |

### Escalation Cache

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_ESCALATION_CACHE_SIZE` | `500` | Maximum number of entries in the escalation semantic cache. Oldest entries (by `created_at`) are evicted when this limit is exceeded. |

### Public Library Mode

| Variable | Default | Description |
|----------|---------|-------------|
| `PUBLIC_LIBRARY_MODE` | `false` | Set to `true` to restrict the API to public library documents only (hides client/project data, blocks writes). Used for the optional `library.hexcaliper.com` subdomain. |

---

## Public Library Subdomain (optional)

`nginx/library.conf` provides a read-only public view of the technical document library,
suitable for exposing as `library.hexcaliper.com`. It shares the same API container and database.

How it works:
- nginx injects `X-Site-Mode: library` on all proxied requests
- The API filters out M-Files-sourced (client) documents
- Write operations (upload, delete, acquisition, escalation, connections) return 403
- The frontend hides the Chat and Workbench tabs, and the Acquisition/Escalation/Connections sub-tabs
- A "Public Library" badge appears in the header

To enable, add an `nginx-library` service to `docker-compose.yml` (a commented example is included):

```yaml
nginx-library:
  image: nginx:1.27-alpine
  ports:
    - "8081:8081"
  volumes:
    - ./nginx/library.conf:/etc/nginx/conf.d/default.conf:ro
    - ./web:/usr/share/nginx/html:ro
  depends_on:
    - api
  networks:
    - app
```

---

## Data Persistence

| Path | Contents |
|------|----------|
| `./data/lancellmot.db` | All metadata: conversations, documents, library items, queues, connections |
| `./data/chroma/` | Vector embeddings (ChromaDB) — includes escalation cache collection |
| `./data/library/` | Downloaded technical documents, laid out as `{manufacturer}/{product_id}/{filename}` |

To reset everything:

```bash
docker compose down
rm -rf data/
```

### Database backup

Back up `lancellmot.db` while the container is running using the SQLite CLI's
online backup command (safe with WAL mode):

```bash
sqlite3 data/lancellmot.db ".backup 'backups/lancellmot-$(date +%Y%m%d-%H%M%S).db'"
```

**Scheduled backup via cron (run on the host):**

```bash
# Back up every night at 02:30
30 2 * * * sqlite3 /opt/hexcaliper-lanceLLMot/data/lancellmot.db \
  ".backup '/opt/hexcaliper-lanceLLMot/backups/lancellmot-$(date +%Y%m%d-%H%M%S).db'" \
  >> /var/log/lancellmot-backup.log 2>&1
```

Or with the helper script (if present):

```bash
30 2 * * * /opt/hexcaliper-lanceLLMot/backup_db.sh >> /var/log/lancellmot-backup.log 2>&1
```

---

## Document Types

### Chat / Workbench documents

| Type | Description |
|------|-------------|
| `standard` | ISO/IEC/EN/NFPA standard |
| `requirement` | Technical or client requirement |
| `theop` | THEOP / operational philosophy |
| `fmea` | Failure Mode & Effects Analysis |
| `hazard_analysis` | Hazard / HAZOP / SIL analysis |
| `fat` / `sat` | Factory / Site Acceptance Test |
| `contract` | Contract or commercial document |
| `correspondence` | Email or letter |
| `plc_code` | PLC / SCADA source code |
| `technical_manual` | Technical or operating manual |
| `datasheet` | Product datasheet |
| `firmware_notes` | Firmware release notes |
| `app_note` | Application note |
| `misc` | Anything else |

### Document classification

| Classification | Meaning |
|----------------|---------|
| `public` | Technical library docs. Auto-escalation eligible. |
| `client` | Client/project docs. Cloud escalation always requires explicit approval. |

**Auto-classification rules:**
- Source = M-Files → always `client`
- Source = web acquisition scraper → always `public`
- Scope = client or project → always `client`
- Manual upload to global scope → `public` if doc_type is `standard`, otherwise `client`
- Classification can be changed post-upload via the ✎ edit button (except client/project-scoped docs)

---

## Manufacturer Scrapers

The acquisition queue can automatically fetch documentation for the following manufacturers.
Each scraper tries multiple strategies (direct URL → primary search → fallback search) before giving up.
If a scraper finds nothing, an escalation is automatically queued as a last resort.

| Manufacturer | Registry key(s) | Primary source |
|---|---|---|
| Beckhoff | `beckhoff` | `beckhoff.com/products/` + InfoSys |
| Allen Bradley / Rockwell | `allen bradley`, `rockwell` | `literature.rockwellautomation.com` |
| Siemens | `siemens` | `support.industry.siemens.com` |
| Phoenix Contact | `phoenix contact` | `phoenixcontact.com/en-us/products/` |
| Danfoss | `danfoss` | `files.danfoss.com` |
| ABB | `abb` | `library.e.abb.com` |
| Yaskawa | `yaskawa` | `yaskawa.com/document-download-center` |

All scrapers respect a 0.5 s per-domain rate limit and retry up to 3 times with
exponential backoff. Downloads are capped at 30 MB per file.

---

## API Reference

All endpoints are prefixed with `/api/` when accessed through nginx.
Interactive docs: `http://localhost:8000/docs`

### Core

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service health and config summary |
| `GET` | `/site-config` | Runtime feature flags (e.g. `public_library_mode`) |
| `GET` | `/models` | List available Ollama models |
| `GET` | `/model-status` | Check if a model is loaded (`?model=name`) |
| `POST` | `/warm-model` | Pre-load a model into VRAM |
| `GET` | `/gpu` | GPU utilisation and VRAM per card |
| `GET` | `/system` | CPU and RAM stats |
| `POST` | `/chat` | Streaming chat (SSE); emits `rag_status`, `sources`, and `done` events |
| `GET` | `/status/pending` | Pending items summary for the merLLM "My Day" panel |

### Conversations

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/conversations` | List all conversations |
| `POST` | `/conversations` | Create a conversation |
| `GET` | `/conversations/{id}` | Get a conversation with history |
| `PATCH` | `/conversations/{id}` | Rename a conversation |
| `DELETE` | `/conversations/{id}` | Delete conversation and its scoped documents |

### Documents

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/documents` | List documents (filterable by scope) |
| `POST` | `/documents` | Upload a document (returns once chunks are embedded and searchable; summary, copyright notices, and concept-graph indexing finish asynchronously in the background) |
| `PATCH` | `/documents/{id}` | Edit document attributes (filename, doc_type, classification) |
| `DELETE` | `/documents/{id}` | Delete a document |
| `POST` | `/documents/reindex` | Re-run concept extraction on all documents |

### Workspace (Clients & Projects)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/workspace/clients` | List clients |
| `POST` | `/workspace/clients` | Create a client |
| `DELETE` | `/workspace/clients/{id}` | Delete a client |
| `GET` | `/workspace/projects` | List projects (optionally by client) |
| `POST` | `/workspace/projects` | Create a project |
| `DELETE` | `/workspace/projects/{id}` | Delete a project |

### Technical Library

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/library/items` | List library items (filter by manufacturer, product, type) |
| `GET` | `/library/manufacturers` | List manufacturers with document counts |
| `POST` | `/library/items` | Manually register a document already on disk |
| `DELETE` | `/library/items/{id}` | Remove from library and disk |
| `GET` | `/library/items/{id}/download` | Download a library document |

### Acquisition Queue

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/acquisition/queue` | List queue items (filter by `?status=`) |
| `POST` | `/acquisition/queue` | Add an item to the queue |
| `PATCH` | `/acquisition/queue/{id}/approve` | Approve and start scraping |
| `PATCH` | `/acquisition/queue/{id}/reject` | Reject an item |
| `POST` | `/acquisition/queue/{id}/retry` | Retry a failed item |
| `DELETE` | `/acquisition/queue/{id}` | Remove an item |
| `GET` | `/acquisition/stream` | SSE progress stream |

### Escalation Queue

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/escalation/queue` | List queue items (filter by `?status=`) |
| `POST` | `/escalation/queue` | Add a query to the escalation queue |
| `PATCH` | `/escalation/queue/{id}/approve` | Approve and trigger cloud call |
| `PATCH` | `/escalation/queue/{id}/reject` | Reject an item |
| `POST` | `/escalation/queue/{id}/retry` | Retry a failed item |
| `DELETE` | `/escalation/queue/{id}` | Remove an item |
| `GET` | `/escalation/stream` | SSE progress stream |

### Connections

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/connections` | List all connection types with config (secrets masked) |
| `PUT` | `/connections/{type}` | Save a connection config |
| `PATCH` | `/connections/{type}/enable` | Enable a connection |
| `PATCH` | `/connections/{type}/disable` | Disable a connection |
| `POST` | `/connections/{type}/test` | Test connectivity |
| `GET` | `/connections/{type}/env-hint` | Check for env-var pre-fill values |
| `POST` | `/connections/mfiles/index` | Start M-Files vault index (background) |
| `GET` | `/connections/mfiles/index/stream` | SSE stream for vault indexer progress |

---

## Chat SSE Protocol

The `POST /chat` endpoint streams Server-Sent Events. Beyond the standard token chunks, three metadata events are emitted:

| Event type | When | Payload |
|------------|------|---------|
| `rag_status` | Before the first token | `{"type": "rag_status", "status": "ok"|"error", "docs_used": N, "graph_nodes": N}` — reports whether RAG retrieval succeeded and how many sources were found |
| `sources` | After the last token, before `done` | `{"type": "sources", "documents": [{"name": "...", "chunk": "...", "score": 0.82}], "graph_nodes": [{"entity": "...", "relation": "..."}]}` — full attribution data rendered in the Sources panel |
| `done` | Final event | `{"type": "done", "conversation_id": "...", "model": "...", "sources": {...}, "doc_ids": [...], "has_client_docs": bool}` |

When merLLM queues the request due to GPU contention, a `queue_status` event may precede all others:

```json
{"type": "queue_status", "reason": "all GPU slots occupied — queued for dispatch", "estimated_wait_seconds": 30}
```

If the wait exceeds merLLM's `QUEUE_HEARTBEAT_INTERVAL_SECONDS`, periodic keepalive lines follow so the read-gap timer on `requests.post(timeout=60, stream=True)` does not trip during a long background-bucket drain:

```json
{"type": "queue_status", "waiting": true, "elapsed_seconds": 20}
```

The UI displays the first event as a waiting indicator with the reason and the heartbeats as a growing wait timer.

---

## Supported File Formats

| Format | Parser |
|--------|--------|
| `.pdf` | pypdf |
| `.docx` | python-docx |
| `.xlsx` / `.xls` / `.csv` | openpyxl |
| `.txt` / `.md` | UTF-8 plain text |
| `.st` / `.scl` / `.lad` etc. | IEC 61131-3 PLC source (plain text) |

---

## Web Search

Built-in, no API key required. Tool-capable models (Qwen3, Qwen2.5, Mistral) invoke
the `web_search` tool automatically. The API scrapes DuckDuckGo HTML, injects up to
5 snippets into context, then streams the grounded answer.

Recommended models:
```bash
ollama pull qwen3:30b          # best tool-calling + thinking support
ollama pull qwen2.5:32b        # strong general + tool use
ollama pull qwen2.5-coder:32b  # code + web search
```

---

## Cloudflare Access (optional)

When deployed behind [Cloudflare Access](https://www.cloudflare.com/products/zero-trust/access/),
the `cf-access-authenticated-user-email` header is forwarded by nginx and used to
scope conversations and documents per user. No additional configuration is needed.

---

## macOS (untested)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) — `host.docker.internal` resolves automatically.
2. Install Ollama from [ollama.com/download](https://ollama.com/download).
3. Remove the `devices` block from `docker-compose.yml` — macOS containers cannot access NVIDIA GPUs.
4. Skip the `libnvidia-ml.so.1` step. The GPU meter shows `--`.
5. Follow the Quickstart from your terminal.

## Windows (untested)

1. Enable WSL2 (Ubuntu 22.04 LTS from the Store is recommended).
2. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) with WSL2 backend.
3. Install Ollama for Windows from [ollama.com/download](https://ollama.com/download) — it's reachable at `localhost:11434` from WSL2.
4. Remove the `devices` block from `docker-compose.yml` if you don't need GPU monitoring.
5. Run all commands from inside your WSL2 terminal.

---

## Open Source Acknowledgements

| Project | License | Role |
|---------|---------|------|
| [Ollama](https://github.com/ollama/ollama) | MIT | Local LLM serving and embeddings |
| [nginx](https://github.com/nginx/nginx) | BSD-2-Clause | Reverse proxy and static file server |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | Python API framework |
| [uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI server |
| [ChromaDB](https://github.com/chroma-core/chroma) | Apache-2.0 | Vector database |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | Async HTTP client (chat, scrapers, cloud) |
| [pydantic](https://github.com/pydantic/pydantic) | MIT | Request/response validation |
| [cryptography](https://github.com/pyca/cryptography) | Apache-2.0 / BSD | Fernet credential encryption |
| [pypdf](https://github.com/py-pdf/pypdf) | BSD-3-Clause | PDF text extraction |
| [python-docx](https://github.com/python-openxml/python-docx) | MIT | DOCX text extraction |
| [openpyxl](https://openpyxl.readthedocs.io/) | MIT | XLSX text extraction |
| [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) | MIT | HTML parsing |
| [pynvml](https://github.com/gpuopenanalytics/pynvml) | BSD-3-Clause | NVIDIA GPU monitoring |
| [python-multipart](https://github.com/Kludex/python-multipart) | Apache-2.0 | File upload parsing |
| [psutil](https://github.com/giampaolo/psutil) | BSD-3-Clause | CPU/RAM system metrics |
