"""Indicode observability console."""
from __future__ import annotations

import html
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import streamlit as st
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

TIMEOUT = 2.5
FALCO_OUTPUT, PCAP_DIR, OUTPUT_DIR = Path("/falco/output"), Path("/falco/pcap"), Path("/workspace/outputs")
SERVICES = {
    "Agent": "http://agent:8003/health", "Ollama": "http://ollama:11434/api/tags",
    "LiteLLM": "http://litellm:4000/health/liveness", "Qdrant": "http://qdrant:6333/collections/sovereign_knowledge",
    "RAG": "http://rag:8001/health", "Docling": "http://docling:8000/health", "Loki": "http://loki:3100/ready",
    "Langfuse": "http://langfuse-web:3000/",
}
TOOLS = ("search_knowledge", "parse_document", "analyze_image", "write_file", "run_code", "grep_codebase", "list_files", "read_file")
EGRESS = re.compile(r"EGRESS_ATTEMPT\s+container=(?P<container>\S+)\s+proc=(?P<process>\S+)\s+dest_ip=(?P<ip>\S+)\s+dest_port=(?P<port>\S+)\s+timestamp=(?P<timestamp>.+)")
UUID = re.compile(r"\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b", re.I)

st.set_page_config(page_title="Indicode | Observability", layout="wide", initial_sidebar_state="expanded")


def get_json(url: str):
    try:
        response = requests.get(url, timeout=TIMEOUT)
        if not response.ok:
            return False, {}
        try:
            return True, response.json()
        except ValueError:
            return True, {}
    except requests.RequestException:
        return False, {}


def size(value: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def falco_events() -> tuple[list[dict], str | None]:
    records, issue = [], None
    try:
        paths = sorted((p for p in FALCO_OUTPUT.rglob("*") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    except (OSError, PermissionError) as exc:
        return [], str(exc)
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
        except (OSError, PermissionError) as exc:
            issue = str(exc)
            continue
        for line in lines:
            try:
                line = json.loads(line).get("output", line)
            except json.JSONDecodeError:
                pass
            match = EGRESS.search(line)
            if match:
                records.append(match.groupdict())
    return sorted(records, key=lambda record: record["timestamp"], reverse=True), issue


def pcap_bytes() -> int:
    try:
        return sum(path.stat().st_size for path in PCAP_DIR.glob("*.pcap") if path.is_file())
    except (OSError, PermissionError):
        return 0


def loki_rows() -> list[dict]:
    now = datetime.now(timezone.utc)
    params = {"query": '{container_name=~"indicode-agent.*"}', "limit": "120", "start": str(int((now - timedelta(minutes=20)).timestamp() * 1e9)), "end": str(int(now.timestamp() * 1e9))}
    try:
        response = requests.get("http://loki:3100/loki/api/v1/query_range", params=params, timeout=TIMEOUT)
        response.raise_for_status()
        rows = []
        for stream in response.json().get("data", {}).get("result", []):
            for timestamp, message in stream.get("values", []):
                rows.append({"Time": datetime.fromtimestamp(int(timestamp) / 1e9, timezone.utc).strftime("%H:%M:%S"), "Event": message})
        return list(reversed(rows))
    except (requests.RequestException, ValueError, KeyError):
        return []


def output_files() -> list[dict]:
    try:
        files = sorted((path for path in OUTPUT_DIR.iterdir() if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
    except (OSError, PermissionError):
        return []
    return [{"Name": path.name, "Type": path.suffix.removeprefix(".").upper() or "FILE", "Size": size(path.stat().st_size), "Modified": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")} for path in files]


def status_grid() -> dict[str, bool]:
    with ThreadPoolExecutor(max_workers=len(SERVICES)) as executor:
        results = executor.map(lambda url: get_json(url)[0], SERVICES.values())
        health = dict(zip(SERVICES, results))
    health["Falco"] = FALCO_OUTPUT.exists()
    return health


st.markdown("""
<style>
.stApp { background:#171717; color:#e8e8e8; } [data-testid="stSidebar"] { background:#202020; border-right:1px solid #343434; }
#MainMenu, footer, header { visibility:hidden; } .block-container { max-width:1600px; padding:1.3rem 2rem 2rem; }
h1 { font-size:1.45rem!important; font-weight:600!important; letter-spacing:-.02em; } h2, h3 { font-weight:600!important; }
.eyebrow { color:#8a8a8a; font-size:.73rem; text-transform:uppercase; letter-spacing:.09em; }
.card { background:#202020; border:1px solid #343434; border-radius:7px; padding:1rem 1.1rem; min-height:110px; }
.card-title { color:#a8a8a8; font-size:.75rem; text-transform:uppercase; letter-spacing:.07em; margin-bottom:.5rem; }
.value { font-size:1.55rem; font-weight:600; color:#f1f1f1; } .good { color:#75be8b; } .bad { color:#e06c75; } .warning { color:#d7ba7d; }
.service { display:inline-block; width:145px; padding:.55rem .65rem; margin:.2rem; border:1px solid #393939; border-radius:5px; background:#1a1a1a; font-size:.86rem; }
.dot { font-size:.7rem; margin-right:.4rem; } .line { border-top:1px solid #343434; margin:1rem 0; }
[data-testid="stDataFrame"] { border:1px solid #343434; border-radius:7px; overflow:hidden; }
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("### INDICODE")
    st.caption("Sovereign observability")
    page = st.radio("Navigation", ["Overview", "Agent activity", "Sovereignty", "System", "Deliverables", "Traces"], label_visibility="collapsed")
    st.divider()
    st.caption("Refresh interval: 5 seconds")

if page != "Traces":
    st_autorefresh(interval=5_000, key="refresh")

health = status_grid()
falco, falco_problem = falco_events()
logs = loki_rows()

st.markdown(f"<div class='eyebrow'>INDICODE / {page.upper()}</div>", unsafe_allow_html=True)
st.title(page)

if page == "Overview":
    up_count = sum(health.values())
    c1, c2, c3, c4 = st.columns(4)
    for column, title, value, cls in ((c1, "Service availability", f"{up_count}/{len(health)}", "good" if up_count == len(health) else "warning"), (c2, "Egress events", str(len(falco)), "good" if not falco else "bad"), (c3, "Knowledge vectors", str(get_json(SERVICES["Qdrant"])[1].get("result", {}).get("points_count", "—")), ""), (c4, "Generated files", str(len(output_files())), "")):
        with column:
            st.markdown(f"<div class='card'><div class='card-title'>{title}</div><div class='value {cls}'>{value}</div></div>", unsafe_allow_html=True)
    left, right = st.columns((1.15, 1))
    with left:
        st.subheader("Recent agent events")
        st.dataframe(logs[:15] or [{"Time": "—", "Event": "No agent events in the last 20 minutes."}], use_container_width=True, hide_index=True, height=430)
    with right:
        st.subheader("Platform status")
        tags = "".join(f"<span class='service'><span class='dot {'good' if state else 'bad'}'>●</span>{html.escape(name)}</span>" for name, state in health.items())
        st.markdown(tags, unsafe_allow_html=True)
        st.markdown("<div class='line'></div>", unsafe_allow_html=True)
        st.subheader("Latest deliverables")
        st.dataframe(output_files()[:8] or [{"Name": "No output files", "Type": "—", "Size": "—", "Modified": "—"}], use_container_width=True, hide_index=True)

elif page == "Agent activity":
    tool_rows = [row for row in logs if any(tool in row["Event"] for tool in TOOLS)]
    parse_rows = [row for row in logs if "[PARSE DEBUG]" in row["Event"]]
    a, b = st.columns(2)
    with a:
        st.subheader("Tool execution")
        st.dataframe(tool_rows[:40] or [{"Time": "—", "Event": "No tool calls recorded."}], use_container_width=True, hide_index=True, height=520)
    with b:
        st.subheader("Parser diagnostics")
        st.dataframe(parse_rows[:40] or [{"Time": "—", "Event": "No parser diagnostics recorded."}], use_container_width=True, hide_index=True, height=520)
    threads = []
    for row in logs:
        threads.extend(UUID.findall(row["Event"]))
    st.caption(f"Observed thread IDs in recent logs: {len(set(threads))}")

elif page == "Sovereignty":
    if falco:
        st.error(f"Egress attempts detected: {len(falco)}. Review the events below.")
    else:
        st.success("No external egress attempts recorded by Falco.")
    a, b = st.columns(2)
    a.metric("Falco egress events", len(falco))
    b.metric("Internal capture volume", size(pcap_bytes()))
    if st.button("Run controlled airgap test"):
        command = ["docker", "run", "--rm", "--network", "indicode-sovereign", "alpine", "wget", "-T3", "http://google.com"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=12, check=False)
            (st.success if result.returncode else st.error)("Blocked as expected." if result.returncode else "Request unexpectedly succeeded.")
            st.code((result.stdout + result.stderr).strip() or "No command output.")
        except (OSError, subprocess.TimeoutExpired) as exc:
            st.error(f"Airgap test unavailable: {exc}")
    st.subheader("Falco event ledger")
    st.dataframe(falco[:25] or [{"timestamp": "—", "ip": "—", "port": "—", "container": "No egress attempts", "process": "—"}], use_container_width=True, hide_index=True)
    if falco_problem:
        st.caption(f"Falco output access note: {falco_problem}")

elif page == "System":
    st.subheader("Service health")
    tags = "".join(f"<span class='service'><span class='dot {'good' if state else 'bad'}'>●</span>{html.escape(name)}</span>" for name, state in health.items())
    st.markdown(tags, unsafe_allow_html=True)
    left, right = st.columns(2)
    with left:
        st.subheader("Loaded models")
        ok, models = get_json(SERVICES["Ollama"])
        st.dataframe([{"Model": item.get("name"), "Size": size(item.get("size", 0))} for item in models.get("models", [])] or [{"Model": "Unavailable", "Size": "—"}], use_container_width=True, hide_index=True)
    with right:
        st.subheader("Routing")
        st.code("coding   → qwen3:8b /think\nvision   → qwen3-vl:4b\nguard    → qwen3:1.7b\nembed    → bge-m3", language="text")

elif page == "Deliverables":
    files = output_files()
    st.metric("Files in workspace/outputs", len(files))
    st.dataframe(files or [{"Name": "No generated deliverables", "Type": "—", "Size": "—", "Modified": "—"}], use_container_width=True, hide_index=True, height=580)

else:  # Traces
    langfuse_up = health["Langfuse"]
    if not langfuse_up:
        st.error("Langfuse is unavailable: Nginx cannot currently connect to langfuse-web:3000. Start or repair the Langfuse web container, then refresh this view.")
        st.code("cd langfuse-upstream && docker compose up -d langfuse-web langfuse-worker")
    else:
        st.success("Langfuse is reachable. This view is its live trace UI.")
        components.html('<iframe src="/langfuse/" title="Langfuse" style="width:100%;height:820px;border:1px solid #343434;border-radius:7px;background:#202020"></iframe>', height=835, scrolling=True)
