"""
Indicode Agent Service — exposes the LangGraph agent via FastAPI.
Endpoints:
  POST /chat — send a message, get the agent's response (with tool calls)
  POST /approve — respond to a pending approval interrupt
  GET  /state/{thread_id} — inspect current state (for the dashboard)
  GET  /health
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any
import uuid

from graph import build_agent_graph
from langgraph.types import Command
from langchain_core.messages import AIMessage

app = FastAPI(title="Indicode Agent Service")

# Build the graph once (module-level singleton)
agent_graph = build_agent_graph()


class ChatRequest(BaseModel):
    message: str
    thread_id: Optional[str] = None


class ChatResponse(BaseModel):
    thread_id: str
    response: Optional[str] = None
    task_type: Optional[str] = None          # ← must have = None
    pending_approval: Optional[Dict] = None
    tool_calls_made: list = []


class ApproveRequest(BaseModel):
    thread_id: str
    decision: str  # "approve" | "reject" | "edit"
    edits: Optional[Dict] = None


@app.get("/health")
def health():
    return {"status": "healthy", "service": "agent"}


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)