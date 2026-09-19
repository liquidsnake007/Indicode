Here's a self-contained prompt you can paste into a fresh conversation. It includes everything the assistant needs to know — no context from this conversation required:

---

# PROMPT: Build Indicode Observability Dashboard

## Project Context

I have a sovereign, air-gapped AI workbench called **Indicode** running in Docker on a single machine (WSL2 Ubuntu 22.04, RTX 5050 8GB). It's an SIH hackathon project: an on-prem agent (LangGraph + FastAPI) that uses local Ollama models (qwen3:8b etc. via LiteLLM gateway) to process confidential industrial documents — with OCR, RAG, sandboxed code execution, human approval gates, and file generation (DOCX/XLSX/PPTX). The critical claim is **sovereignty: zero external network calls**, proven via eBPF monitoring.

I need a **Streamlit observability dashboard** — one page, dark-themed, demo-ready for judges. It replaces an existing placeholder app.

## Existing Infrastructure (all running, all reachable)

Everything runs on a Docker network called `indicode-sovereign`. The dashboard container is already defined in docker-compose and can reach all services by hostname:

| Service | URL (from dashboard container) | What it provides |
|---|---|---|
| **Agent** | `http://agent:8003` | `/health` (JSON), `/state/{thread_id}`, `/messages/{thread_id}` (returns `{"messages": [{"role": "user"/"assistant"/"tool_call"/"tool_result", "content": ...}]}`) |
| **Ollama** | `http://ollama:11434` | `/api/tags` → loaded models list |
| **LiteLLM** | `http://litellm:4000` | `/health/liveness`, `/v1/models` |
| **Qdrant** | `http://qdrant:6333` | `/collections/sovereign_knowledge` → `result.points_count` |
| **RAG** | `http://rag:8001` | `/health` |
| **Docling (OCR)** | `http://docling:8000` | `/health` |
| **Langfuse v4** | `http://langfuse:3000` | LLM trace UI (see note below) |
| **Loki** | `http://loki:3100` | Container logs via Promtail. Query API: `GET /loki/api/v1/query_range?query={container_name=~"indicode-.*"}&limit=50&start=<ns>&end=<ns>` |
| **Falco (eBPF)** | File: `/falco/output/` (volume-mounted dir, JSON event lines) + `docker logs indicode-falco` | Egress attempt events with fields: `output` containing `EGRESS_ATTEMPT container=X proc=Y dest_ip=Z dest_port=W timestamp=T` |
| **Nginx** | external `http://localhost:8080` | Publishes dashboard at `/`, Langfuse at `/langfuse/` (with X-Frame-Options stripped so iframes work) |

Generated deliverables live at `/workspace/outputs/` (volume-mounted into the dashboard container).

## Dashboard Requirements

**One page, auto-refreshing every ~5 seconds, VS Code dark theme** (background `#1E1E1E`, panels `#252526`, borders `#3C3C3C`, accent `#007ACC`, success `#89D185`, error `#F44747`, muted text `#858585`, warning `#DCDCAA`). Must look professional — this is shown live to judges.

### Panel 1: Sovereignty Status (the money panel — make this prominent)
- Big banner: "🔒 ZERO EXTERNAL CONNECTIONS" in green if no egress attempts detected recently, red if any
- Parse Falco output files in `/falco/output/` for `EGRESS_ATTEMPT` lines; show count, and a table of the last ~10 attempts (timestamp, destination IP, process/container)
- A "Test the airgap" button that runs `docker run --rm --network indicode-sovereign alpine wget -T3 http://google.com` via subprocess, shows it FAILING (this is the live proof judges love), and shows the resulting Falco event appearing
- Show tcpdump pcap file size from `/falco/pcap/` as "internal traffic captured: X MB"

### Panel 2: System Health
- Grid of service status pills (agent, ollama, litellm, qdrant, rag, docling, falco, loki) — green dot + name when healthy (HTTP 200 from /health), red when down
- Ollama loaded models (from `/api/tags` → each model's name + size)
- Qdrant knowledge base: points count
- Model routing display (static text): coding→qwen3:8b /think, vision→qwen3-vl:4b, guard→qwen3:1.7b

### Panel 3: Agent Activity (live)
- Query Loki for recent `indicode-agent` logs, extract `[PARSE DEBUG]` lines to show current agent activity (what tool it's calling, what it's thinking about)
- Show last N tool calls: tool name + timestamp, styled with icons (🔍 search_knowledge, 📄 parse_document, 👁 analyze_image, 📝 write_file, ⚡ run_code, 🔎 grep_codebase)
- Show pending approvals if any agent thread is paused at the approval node (poll a few recent thread states)

### Panel 4: Sessions & Deliverables
- List files in `/workspace/outputs/` — name, size, modified time, file-type icon (📄 docx, 📊 xlsx, 📽 pptx, 🐍 py)
- Session list from agent `/state/` — but I don't have a "list all threads" endpoint, so instead track recently seen thread IDs. Simpler: show file activity and a note about the TUI's session history

### Panel 5: Traces (Langfuse)
- Embed Langfuse UI via iframe `http://localhost:8080/langfuse/` (works because nginx strips X-Frame-Options on that path)
- **Important:** Langfuse may show NO traces currently because the agent doesn't emit OTLP yet. Two-part handling:
  - Show the iframe anyway (so judges see the infrastructure exists)
  - Show a note: "Traces appear when LiteLLM callback is enabled"
  - ALSO give me (as a comment in the code or separate instructions) the exact LiteLLM config addition to enable Langfuse callbacks — I believe it's `success_callback: ["langfuse"]` in litellm_settings plus `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST` env vars — please verify this pattern and give me the exact config + docker-compose env changes, since wiring LiteLLM→Langfuse would make traces actually appear and complete the observability story

## Technical Constraints

- **Streamlit** (already in the dashboard container), Python 3.11
- The dashboard container is on BOTH `indicode-sovereign` and `edge` networks, so it can reach internal services AND is published via nginx at `http://localhost:8080/`
- Use `requests` (or httpx) for service calls with short timeouts (2-3s) — a down service must not freeze the dashboard
- Auto-refresh: use `st.rerun()` with `time.sleep()` or the `streamlit-autorefresh` package (add to requirements.txt if needed)
- Falco output files are root-owned in some cases — handle permission errors gracefully (fall back to parsing `docker logs` output if file read fails, or just show "monitoring active")
- The "Test airgap" button needs the Docker socket — the dashboard container currently does NOT have `/var/run/docker.sock` mounted; give me the docker-compose volumes addition for that
- Everything must work offline/air-gapped — no CDN links, no external fonts, inline all CSS in Streamlit markdown blocks (`st.markdown(..., unsafe_allow_html=True)`)
- Layout: use `st.columns` for a grid; sovereignty panel spans full width on top, then 2-3 columns below
- Title header: "Indicode — Sovereign Observability" with a subtle status line

## Deliverables I Need From You

1. **Complete `dashboard/app.py`** — the full Streamlit app, ready to paste
2. **Complete `dashboard/requirements.txt`**
3. **docker-compose changes** — the exact YAML diff for: Docker socket mount for the airgap test button, and any env vars needed
4. **LiteLLM → Langfuse callback wiring** — exact config.yaml addition + env vars so LLM traces actually appear in Langfuse
5. **One command to rebuild and test** the dashboard

## Style Notes

- Dark theme via Streamlit config: create `.streamlit/config.toml` with `backgroundColor: "#1E1E1E"`, `primaryColor: "#007ACC"`, dark base theme
- Custom HTML/CSS panels via `st.markdown` for the status pills and sovereignty banner (Streamlit widgets alone look generic — use styled HTML divs for the health grid and the big green/red sovereignty banner)
- Keep it to ONE page — no sidebar navigation, no tabs. Judges should see everything at a glance.
- Auto-refresh must not cause flicker — use `st_autorefresh(interval=5000)` if available, else a key-based `st.rerun` pattern

---

That's the prompt. Paste it into a fresh conversation and the assistant has everything: service names, URLs, API shapes, Falco log format, Loki query format, the iframe trick, the Docker socket gap, and the Langfuse callback question. It should produce the complete dashboard in one pass.

**One heads-up before you use it:** the OTel collector in your stack is deployed but the agent never actually emits OTLP spans — that's why Langfuse is likely empty. The prompt asks the new assistant to give you the LiteLLM→Langfuse callback config (`success_callback: ["langfuse"]` + the three LANGFUSE_* env vars), which is the 5-minute fix that makes real traces flow without touching the agent code at all. That single change turns Panel 5 from "infrastructure exists" into "look at the agent's actual reasoning traces live" — which is a much stronger demo moment.
