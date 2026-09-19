# Indicode — Sovereign On-Premise Agentic AI Workbench

Air-gapped AI workbench for confidential industrial work. Nothing leaves the machine.

## Stack

| Layer | Tech |
|---|---|
| Models | Ollama — qwen3:8b, qwen3-vl:4b, qwen3:1.7b, bge-m3 |
| Gateway | LiteLLM (aliases: sovereign-general/coder/vision/guard/embed) |
| RAG | Qdrant + BGE-M3 + FlashRank |
| Doc parsing | Docling + OCR (SHA256-cached) |
| Traces | Langfuse v4 (`langfuse-upstream/`) |
| Egress proof | Falco eBPF + tcpdump + DOCKER-USER rules |
| UI | Streamlit via Nginx on 127.0.0.1:8080 |

## Setup

```bash
# 1. Copy real configs (already included in this private repo)
cp litellm/config.yaml.example litellm/config.yaml
# replace REPLACE_WITH_YOUR_LOCAL_KEY with sk-sovereign-local-only

# 2. Pull models (~15 min, needs internet ONCE)
docker compose up -d ollama
docker exec -it indicode-ollama ollama pull qwen3:8b
docker exec -it indicode-ollama ollama pull qwen3-vl:4b
docker exec -it indicode-ollama ollama pull qwen3:1.7b
docker exec -it indicode-ollama ollama pull bge-m3

# 3. Start Langfuse v4 (separate stack)
cd langfuse-upstream && docker compose up -d && cd ..

# 4. Start the main stack
docker compose up -d

# 5. Verify the airgap (must fail)
docker run --rm --network indicode-sovereign alpine wget -T3 http://google.com

## Terminal commands

In the terminal client, use `/quit` when the Ctrl+Q keybinding is unavailable.
To add a local document to the knowledge base, enter `/rag <path-to-file>`; for
example, `/rag ~/Documents/vendor-quote.pdf`. The client uploads the selected
file to the agent service, which stores and indexes it through the RAG service.
