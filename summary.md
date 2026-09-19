# Indicode Project — Full Status Summary

## What's Done and Working

### Phase 1 — Sovereign Spine ✓
- Docker stack with internal networking, Ollama (4 models), LiteLLM gateway, Qdrant, Langfuse v4, Falco eBPF, Loki, Nginx
- Airgap verified — zero egress, Falco proof
- Build script (`./build.sh`) handles the iptables toggle for builds

### Phase 2 — RAG + Document Processing ✓
- Docling + OCR service with caching
- RAG ingestion (Qdrant + BGE-M3 + FlashRank) — all demo documents ingested
- Vision model (qwen3-vl:4b with /no_think) — handwriting and P&ID both tested working
- Image-based PDF creation pipeline (genuine OCR test, not fake)

### Phase 3 — Agent Core ✓
- LangGraph agent with 8 tools (search_knowledge, parse_document, analyze_image, write_file, run_code, read_file, grep_codebase, list_files)
- JSON tool-calling format (replaced bind_tools — qwen3:8b can't do native tool calling reliably)
- Human approval gates with interrupt/resume
- Self-correcting retries (verified — model fixes its own parameter errors)
- Task classifier with keyword fast-path
- Dynamic token limits (6000 for documents, 3000 for coding)
- Echo-back guard, empty-content retry mechanism
- SqliteSaver session persistence (survives container restarts)

### Phase 4 — CLI/TUI ✓ Working
- Textual TUI with streaming reasoning, tool call display, approval modals
- Session history with persistence, click-to-resume
- `/prompt` command for long prompts from files
- Slash commands with filtering
- Multi-line TextArea input (ctrl+enter to submit)
- VS Code dark theme colors applied
- Homepage with logo and input box
- Approximate context window counter with history support

### File Generation ⚠️ Mostly Working
- DOCX generator with proper formatting (headings, tables, bold, no raw `**`)
- XLSX generator with colored headers, alternating rows, multi-sheet support
- PPTX generator with title slides, accent bars, real PowerPoint tables
- Shared markdown parser handles inline formatting
- All generators verified working through agent + approval flow

### Infrastructure ✓
- Nginx `/api/agent/` route for external access (SSE unbuffered)
- Agent API exposed for external CLI clients
- Session persistence with `~/.indicode_sessions.json` (deduplicated)

---

## What Remains To Be Done

### Critical Path to 80% Tag
1. **Full test suite pass** — run the backend scorecard and verify the current model and prompt changes end-to-end
2. **Release smoke test** — run the external TUI from Windows against the LAN API, including history and approval flow
3. **Commit and tag `v0.8-milestone`**

### Final 20% (After v0.8 Tag)
5. **VS Code extension** — 2-hour version (repackage OpenCode extension pointing at your LiteLLM)
6. **Observability dashboard** — one Streamlit page with airgap status, active trace, session list, output files
7. **Demo hardening** — pre-warm models, script the 3 demos, prepare scalability answer

---

## Known Open Issues

| Issue | Severity | Workaround |
|-------|----------|------------|
| Long prompts sometimes produce empty content | Model limitation | Retry mechanism handles it; `/prompt` from file is reliable |
| Multi-turn conversations can loop on context overload | Model limitation | Use `/new` between prompts |
| Dashboard is still a Phase 1 placeholder | Future work | Build the observability view after the milestone |
| NeMo Guardrails is not wired into the active stack | Future work | Keep the proven model roster for the hackathon |
---

## The Honest State

The engine and external TUI are working. The remaining milestone risk is full end-to-end validation after the latest agent and logging changes.

**What matters for the 80% milestone:**

1. The chat interface works (it does)
2. Streaming works (it does)
3. Approvals work (they do)
4. File generation works (it does)
5. It runs outside Docker (the LAN TUI path is now working)