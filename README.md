# Hexcaliper — Self-Hosted AI Chat Workbench

A self-hosted chat interface for local LLMs via [Ollama](https://ollama.com), with document RAG and automatic URL fetching. Runs entirely on your machine — no cloud API keys required.

## Features

- **Multi-model chat** — switch between any model pulled into your local Ollama instance
- **Persistent conversations** — history stored in a lightweight TinyDB JSON file
- **Document RAG** — upload PDF, DOCX, TXT, or Markdown files; relevant chunks are retrieved and injected into context automatically
- **URL fetching** — paste a URL in your message and the page content is fetched and included as context
- **Streaming responses** — tokens stream to the browser via Server-Sent Events
- **Thinking model support** — extended reasoning tokens from DeepSeek-R1, QwQ, and similar models are displayed separately
- **Live GPU meter** — real-time utilisation and VRAM readout via NVML
- **Multi-user ready** — user isolation via Cloudflare Access header (`cf-access-authenticated-user-email`); falls back to `local@dev` for local use

## Architecture

```
Browser (nginx :8080)
  └── /api/* → FastAPI (uvicorn :8000)
                 ├── Ollama  (host :11434)
                 ├── ChromaDB  (./data/chroma)
                 └── TinyDB    (./data/db.json)
```

| Component | Technology |
|-----------|------------|
| Frontend  | Vanilla JS + CSS, served by nginx |
| API       | Python 3.12, FastAPI, uvicorn |
| Vector DB | ChromaDB (persistent, cosine similarity) |
| Embeddings | Ollama (`nomic-embed-text` by default) |
| Document storage | TinyDB (flat JSON) |
| Container orchestration | Docker Compose |

## Prerequisites (Ubuntu 24.04.4 LTS — tested)

> The instructions below have been verified on **Ubuntu 24.04.4 LTS**. For macOS or Windows, see the platform-specific sections further down.

### Docker

Docker Engine and the Compose plugin (v2) are required to build and run the containers.

- **Linux:** follow the [official install guide](https://docs.docker.com/engine/install/) for your distro, then install the [Compose plugin](https://docs.docker.com/compose/install/linux/)
- **macOS / Windows:** install [Docker Desktop](https://www.docker.com/products/docker-desktop/), which bundles Compose

Verify your installation:

```bash
docker --version          # Docker 24+ recommended
docker compose version    # should print v2.x
```

### Ollama

Ollama serves the local LLMs and generates embeddings. It must be running on the host before starting Hexcaliper.

- **All platforms:** download from [ollama.com/download](https://ollama.com/download)
- **Linux one-liner:**
  ```bash
  curl -fsSL https://ollama.com/install.sh | sh
  ```

After installing, start the Ollama daemon (it starts automatically on most platforms after installation):

```bash
ollama serve   # if not already running as a system service
```

### NVIDIA GPU (optional)

An NVIDIA GPU is required for the live GPU meter and for the `devices` passthrough in `docker-compose.yml`. If you don't have one, remove the `devices` block from `docker-compose.yml` and skip the NVML step below — everything else still works.

- Install the [NVIDIA driver](https://www.nvidia.com/Download/index.aspx) for your OS
- On Linux, also install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) to allow Docker to access the GPU:
  ```bash
  # Ubuntu / Debian
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
  ```

### Pulling required Ollama models

At minimum you need a chat model and the embedding model:

```bash
ollama pull llama3:8b          # or any other chat model you prefer
ollama pull nomic-embed-text   # required for document RAG
```

### Locating `libnvidia-ml.so.1`

The API container needs the NVML shared library to read GPU stats:

```bash
find /usr -name "libnvidia-ml.so.1" 2>/dev/null
# e.g. /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1
cp /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1 api/
```

If you don't have an NVIDIA GPU, skip this step and remove the `devices` section from `docker-compose.yml`. The GPU meter will simply show `--`.

## Quickstart (Ubuntu 24.04.4 LTS — tested)

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

## Configuration

All tunables are set via environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama API endpoint |
| `DEFAULT_MODEL` | `llama3:8b` | Model selected on first load |
| `MAX_INPUT_CHARS` | `20000` | Maximum characters per user message |
| `REQUEST_TIMEOUT_SECONDS` | `120` | Ollama request timeout |
| `EMBED_MODEL` | `nomic-embed-text` | Ollama model used for embeddings |

## Data persistence

Conversation history and document metadata are stored in `./data/db.json`. Vector embeddings live in `./data/chroma/`. Both paths are bind-mounted into the container and survive restarts.

To reset all data:

```bash
docker compose down
rm -rf data/db.json data/chroma
```

## macOS (untested)

> **Note:** Hexcaliper has only been tested on Ubuntu 24.04.4 LTS. The steps below are provided in good faith but have not been verified. Contributions and bug reports from macOS users are welcome.

1. **Install Docker Desktop** — download from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/). Both Intel and Apple Silicon builds are available. `host.docker.internal` resolves to the host automatically, so no extra networking configuration is needed.

2. **Install Ollama** — download the macOS app from [ollama.com/download](https://ollama.com/download). On Apple Silicon (M1 and later) Ollama uses Metal for GPU acceleration and generally delivers excellent inference performance without any extra setup.

3. **Remove GPU device passthrough** — macOS does not expose NVIDIA or Apple Silicon GPUs to Docker containers. Before running `docker compose`, remove the `devices` block from `docker-compose.yml`:
   ```yaml
   # delete these lines:
   devices:
     - /dev/nvidiactl:/dev/nvidiactl
     - /dev/nvidia0:/dev/nvidia0
   ```
   Skip the `libnvidia-ml.so.1` copy step entirely. The GPU meter in the UI will show `--`.

4. **Clone and run** — follow the standard [Quickstart](#quickstart-ubuntu-24044-lts--tested) from your terminal. Skip the NVML library step.

## Windows (untested)

> **Note:** Hexcaliper has only been tested on Ubuntu 24.04.4 LTS. The steps below are provided in good faith but have not been verified. Contributions and bug reports from Windows users are welcome.

Running on Windows is possible in theory via WSL2 and Docker Desktop:

1. **Enable WSL2** — follow [Microsoft's WSL install guide](https://learn.microsoft.com/en-us/windows/wsl/install). Ubuntu 22.04 LTS from the Microsoft Store is a good choice.

2. **Install Docker Desktop** — download from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/). In Settings → General, ensure **Use the WSL 2 based engine** is enabled. In Settings → Resources → WSL Integration, enable integration for your Ubuntu distro.

3. **Install Ollama** — download the Windows installer from [ollama.com/download](https://ollama.com/download). Ollama runs natively on Windows and is reachable at `localhost:11434` from both Windows and WSL2.

4. **GPU passthrough (optional)** — the `docker-compose.yml` passes Linux NVIDIA device nodes (`/dev/nvidia0`, `/dev/nvidiactl`) that do not exist on Windows. To use GPU monitoring inside the container you will need:
   - [NVIDIA drivers for Windows](https://www.nvidia.com/Download/index.aspx) (these also expose the GPU inside WSL2)
   - [CUDA Toolkit for WSL](https://developer.nvidia.com/cuda-downloads?target_os=Linux&target_arch=x86_64&Distribution=WSL-Ubuntu) — this provides `libnvidia-ml.so.1` inside WSL2 at a path such as `/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1`
   - The `devices` block in `docker-compose.yml` may still not work as written under Docker Desktop; you may need to switch to the `deploy.resources.reservations.devices` (NVIDIA Container Toolkit) syntax instead

   If you do not need GPU monitoring, remove the `devices` block from `docker-compose.yml` entirely and skip the `libnvidia-ml.so.1` copy step.

5. **Clone and run inside WSL2** — open your WSL2 terminal and follow the standard [Quickstart](#quickstart-ubuntu-24044-lts--tested) from there. Do not run `docker compose` from a Windows Command Prompt or PowerShell, as path handling differs.

## Cloudflare Access (optional)

When deployed behind [Cloudflare Access](https://www.cloudflare.com/products/zero-trust/access/), the `cf-access-authenticated-user-email` header is forwarded by nginx and used to scope conversations and documents per user. No additional configuration is needed.

## API reference

The FastAPI backend exposes these endpoints (all prefixed with `/api/` when accessed through nginx):

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service health and config |
| `GET` | `/models` | List available Ollama models |
| `GET` | `/gpu` | GPU utilisation and VRAM |
| `GET/POST/DELETE` | `/conversations[/{id}]` | Manage conversations |
| `GET/POST/DELETE` | `/documents[/{id}]` | Manage uploaded documents |
| `POST` | `/chat` | Send a message (SSE streaming) |

Interactive docs are available at `http://localhost:8000/docs` when the container is running.

## Open source acknowledgements

Hexcaliper is built on the shoulders of these open source projects:

| Project | License | Role |
|---------|---------|------|
| [Ollama](https://github.com/ollama/ollama) | MIT | Local LLM serving and embeddings |
| [nginx](https://github.com/nginx/nginx) | BSD-2-Clause | Reverse proxy and static file server |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | Python API framework |
| [uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI server |
| [ChromaDB](https://github.com/chroma-core/chroma) | Apache-2.0 | Vector database for document RAG |
| [TinyDB](https://github.com/msiemens/tinydb) | MIT | Lightweight JSON document store |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | Async HTTP client |
| [pydantic](https://github.com/pydantic/pydantic) | MIT | Request/response data validation |
| [pypdf](https://github.com/py-pdf/pypdf) | BSD-3-Clause | PDF text extraction |
| [python-docx](https://github.com/python-openxml/python-docx) | MIT | DOCX text extraction |
| [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) | MIT | HTML parsing for URL fetching |
| [pynvml](https://github.com/gpuopenanalytics/pynvml) | BSD-3-Clause | NVIDIA GPU monitoring |
| [python-multipart](https://github.com/Kludex/python-multipart) | Apache-2.0 | File upload parsing |
