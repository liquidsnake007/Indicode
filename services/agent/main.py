"""
Indicode Agent Service — exposes the LangGraph agent via FastAPI.
Endpoints:
  POST /chat — send a message, get the agent's response (with tool calls)
  POST /approve — respond to a pending approval interrupt
  GET  /state/{thread_id} — inspect current state (for the dashboard)
  GET  /health
"""
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any
import json
import uuid
from pathlib import Path
import httpx

from graph import build_agent_graph
from langgraph.types import Command
from langchain_core.messages import AIMessage

app = FastAPI(title="Indicode Agent Service")

# Build the graph once when the service starts.
agent_graph = build_agent_graph()


class ChatRequest(BaseModel):
    message: str
    thread_id: Optional[str] = None


class ChatResponse(BaseModel):
    thread_id: str
    response: Optional[str] = None
    task_type: Optional[str] = None
    pending_approval: Optional[Dict] = None
    tool_calls_made: list = []


class ApproveRequest(BaseModel):
    thread_id: str
    decision: str  # "approve" | "reject" | "edit"
    edits: Optional[Dict] = None


@app.get("/health")
def health():
    return {"status": "healthy", "service": "agent"}


@app.post("/rag")
async def ingest_rag_file(file: UploadFile = File(...)):
    filename = Path(file.filename or "uploaded-document").name
    destination = Path("/workspace/inputs") / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(await file.read())
    try:
        response = httpx.post(
            "http://rag:8001/ingest",
            json={"file_path": str(destination)},
            timeout=600.0,
        )
        response.raise_for_status()
        return {"filename": filename, **response.json()}
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"RAG ingestion failed: {error}")


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Send a message to the agent. If the agent needs approval for
    a sensitive action, returns pending_approval instead of a response."""
    thread_id = req.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    try:
        # Run the graph with the new message
        result = agent_graph.invoke(
            {"messages": [("user", req.message)]},
            config={"configurable": {"thread_id": thread_id}, "recursion_limit": 25},
        )

        # Check for pending approval (interrupt state)
        state = agent_graph.get_state(config)
        pending = None
        if state.next and "approval" in state.next:
            # Graph is paused at the approval node
            if state.tasks:
                for task in state.tasks:
                    if hasattr(task, "interrupts") and task.interrupts:
                        intr = task.interrupts[0]
                        pending = intr.value if hasattr(intr, "value") else None

        # Extract the last AI message content
        response_text = None
        for msg in reversed(result.get("messages", [])):
            if hasattr(msg, "content") and isinstance(msg, AIMessage):
                response_text = msg.content
                break

        # Track tool calls made
        tools_used = []
        for msg in result.get("messages", []):
            if hasattr(msg, "tool_calls"):
                for tc in msg.tool_calls:
                    tools_used.append(tc["name"])

        return ChatResponse(
            thread_id=thread_id,
            response=response_text,
            task_type=result.get("task_type"),
            pending_approval=pending,
            tool_calls_made=tools_used,
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/approve", response_model=ChatResponse)
async def approve(req: ApproveRequest):
    """Respond to a pending approval interrupt."""
    config = {"configurable": {"thread_id": req.thread_id}}

    decision = Command(resume={"type": req.decision, "edits": req.edits or {}})

    try:
        result = agent_graph.invoke(decision, config={"configurable": {"thread_id": req.thread_id}, "recursion_limit": 25})

        state = agent_graph.get_state(config)
        pending = None
        if state.next and "approval" in state.next:
            if state.tasks:
                for task in state.tasks:
                    if hasattr(task, "interrupts") and task.interrupts:
                        intr = task.interrupts[0]
                        pending = intr.value if hasattr(intr, "value") else None

        response_text = None
        for msg in reversed(result.get("messages", [])):
            if hasattr(msg, "content") and isinstance(msg, AIMessage):
                response_text = msg.content
                break

        tools_used = []
        for msg in result.get("messages", []):
            if hasattr(msg, "tool_calls"):
                for tc in msg.tool_calls:
                    tools_used.append(tc["name"])

        return ChatResponse(
            thread_id=req.thread_id,
            response=response_text,
            pending_approval=pending,
            tool_calls_made=tools_used,
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/state/{thread_id}")
async def get_state(thread_id: str):
    """Inspect the current state of an agent conversation."""
    config = {"configurable": {"thread_id": thread_id}}
    try:
        state = agent_graph.get_state(config)
        return {
            "thread_id": thread_id,
            "next": list(state.next) if state.next else [],
            "task_type": state.values.get("task_type") if state.values else None,
            "message_count": len(state.values.get("messages", [])) if state.values else 0,
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Thread not found: {e}")

@app.get("/messages/{thread_id}")
async def get_messages(thread_id: str):
    """Return the full conversation for a thread (for TUI session resume)."""
    config = {"configurable": {"thread_id": thread_id}}
    try:
        state = agent_graph.get_state(config)
        if not state.values or "messages" not in state.values:
            raise HTTPException(status_code=404, detail="Thread not found")

        messages = []
        for msg in state.values["messages"]:
            msg_type = type(msg).__name__
            content = str(getattr(msg, "content", ""))

            if msg_type == "HumanMessage":
                messages.append({"role": "user", "content": content})
            elif msg_type == "AIMessage":
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    for tc in tool_calls:
                        messages.append({
                            "role": "tool_call",
                            "tool": tc["name"],
                            "args_preview": str(tc.get("args", ""))[:200],
                        })
                elif content and not content.startswith('{"'):
                    messages.append({"role": "assistant", "content": content})
            elif msg_type == "ToolMessage":
                messages.append({
                    "role": "tool_result",
                    "content": content[:300],
                })

        return {"thread_id": thread_id, "messages": messages}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Thread not found: {e}")

@app.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """Stream agent progress as SSE. Uses a SYNC generator so FastAPI runs
    it in a thread pool — this prevents the sync graph.stream() from blocking
    the event loop and allows real-time token delivery."""
    import json
    thread_id = req.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit":15}

    def event_generator():  # ← SYNC, not async
        yield f"data: {json.dumps({'type': 'thread', 'thread_id': thread_id})}\n\n"

        try:
            for stream_event in agent_graph.stream(
                {"messages": [("user", req.message)]},
                config=config,
                stream_mode=["updates", "custom"],
            ):
                mode, data = stream_event

                # ── CUSTOM mode: ONLY reasoning tokens (from agent_reasoning) ──
                if mode == "custom" and isinstance(data, dict):
                    etype = data.get("type", "")
                    if etype in ("reasoning_start", "reasoning", "reasoning_end"):
                        yield f"data: {json.dumps(data)}\n\n"
                    # Skip tool_call/final/error from custom — updates handles those

                # ── UPDATES mode: node completion events ──
                elif mode == "updates" and isinstance(data, dict):
                    for node_name, node_output in data.items():
                        if not isinstance(node_output, dict):
                            continue

                        if node_name == "classify":
                            yield f"data: {json.dumps({'type': 'classified', 'task_type': node_output.get('task_type', 'unknown')})}\n\n"

                        elif node_name == "parse":
                            msgs = node_output.get("messages", [])
                            if msgs:
                                msg = msgs[-1]
                                if hasattr(msg, "tool_calls") and msg.tool_calls:
                                    for tc in msg.tool_calls:
                                        yield f"data: {json.dumps({'type': 'tool_call', 'tool': tc['name'], 'args_preview': str(tc.get('args', ''))[:300]})}\n\n"
                                elif msg.content and not str(msg.content).startswith('{"'):
                                    yield f"data: {json.dumps({'type': 'final', 'content': str(msg.content)})}\n\n"

                        elif node_name == "tools":
                            msgs = node_output.get("messages", [])
                            for msg in msgs:
                                if hasattr(msg, "content") and msg.content:
                                    preview = str(msg.content)[:200]
                                    yield f"data: {json.dumps({'type': 'tool_result', 'tool': 'tool', 'content_preview': preview})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)[:500]})}\n\n"

        # ── After stream: check for approval gate ──
        try:
            state = agent_graph.get_state(config)
            if state.next and "approval" in (state.next or []):
                if state.tasks:
                    for task in state.tasks:
                        if hasattr(task, "interrupts") and task.interrupts:
                            intr = task.interrupts[0]
                            if hasattr(intr, "value"):
                                yield f"data: {json.dumps({'type': 'approval_required', 'pending': intr.value})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'state check: {e}'})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        },
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)