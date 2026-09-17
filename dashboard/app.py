import streamlit as st
import json
import os
from pathlib import Path
from datetime import datetime

st.set_page_config(page_title="Indicode Sovereign Console", layout="wide")

st.title("🛡️ Indicode Sovereign Console — Phase 1")
st.caption("Sovereign Spine Verification")

tab1, tab2, tab3 = st.tabs(["Sovereignty", "Services", "About"])

with tab1:
    st.subheader("Egress Monitor (Falco)")
    falco_log = Path("/falco/output") if Path("/falco/output").exists() else None
    # Falco output will be mounted read-only when the dashboard is expanded.
    st.info("Falco output will appear here once the dashboard container has access to ./falco/output (configured in Phase 6).")
    st.code("docker logs indicode-falco --tail 20", language="bash")
    st.caption("Run the above in a separate terminal to see live Falco events.")

with tab2:
    st.subheader("Service Health")
    services = {
        "Ollama": "http://ollama:11434",
        "LiteLLM": "http://litellm:4000/health/liveness",
        "Langfuse": "http://langfuse:3000",
        "OTel Collector": "http://otel-collector:13133",
        "Loki": "http://loki:3100/ready",
    }
    for name, url in services.items():
        st.write(f"**{name}**: `{url}`")
    st.caption("Click the Langfuse tab in the navbar above to open the trace UI.")

with tab3:
    st.markdown("""
    ## Phase 1: Sovereign Spine
    
    This is the placeholder dashboard. Phase 6 replaces it with:
    - **Sovereignty tab**: Falco egress events + tcpdump flow summary
    - **Traces tab**: Langfuse iframe (embedded via Nginx header stripping)
    - **Logs tab**: Loki container logs
    - **Approvals tab**: LangGraph pending interrupts
    
    ## Verification checklist
    - [ ] `curl http://localhost:8080/` returns this page
    - [ ] `curl http://localhost:8080/langfuse/` returns Langfuse UI
    - [ ] `docker run --rm --network=indicode-sovereign alpine wget -T3 google.com` FAILS
    - [ ] `docker logs indicode-falco --tail 5` shows EGRESS_ATTEMPT from the above test
    """)
