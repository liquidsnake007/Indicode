import json
import os
import httpx

class AgentClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.thread_id = None

    def stream_chat(self, message: str):
        """Yield SSE event dicts as they arrive."""
        payload = {"message": message, "thread_id": self.thread_id}
        events = []
        with httpx.Client(timeout=600.0) as client:
            with client.stream("POST", f"{self.base_url}/chat/stream", json=payload) as resp:
                buffer = ""
                for chunk in resp.iter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        raw, buffer = buffer.split("\n\n", 1)
                        if raw.startswith("data: "):
                            try:
                                ev = json.loads(raw[6:])
                                events.append(ev)
                                if ev.get("type") == "thread" and self.thread_id is None:
                                    self.thread_id = ev["thread_id"]
                                yield ev
                            except json.JSONDecodeError:
                                pass

    def approve(self, decision: str):
        payload = {"thread_id": self.thread_id, "decision": decision, "edits": {}}
        with httpx.Client(timeout=600.0) as client:
            resp = client.post(f"{self.base_url}/approve", json=payload)
            resp.raise_for_status()
            return resp.json()

    def ingest_file(self, path: str):
        with open(path, "rb") as file_handle:
            response = httpx.post(
                f"{self.base_url}/rag",
                files={"file": (os.path.basename(path), file_handle)},
                timeout=600.0,
            )
        response.raise_for_status()
        return response.json()