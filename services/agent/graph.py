"""
LangGraph agent with explicit JSON tool-calling.
qwen3:8b cannot reliably emit structured tool calls via bind_tools,
so we instruct it to output JSON in a strict format and parse it.
"""
import json
import re
from typing import Annotated, TypedDict, Optional, List, Dict
from langchain_core.messages import (
    BaseMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage
)
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END, START
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command

from config import (
    LITELLM_BASE_URL, LITELLM_API_KEY,
    MODEL_CLASSIFIER, MODEL_GENERAL, MODEL_CODER, MODEL_VISION,
    MAX_TOKENS_GENERAL, SENSITIVE_TOOLS,
)
from tools import (
    search_knowledge, parse_document, analyze_image,
    write_file, run_code, read_file,
)

TOOL_MAP = {
    "search_knowledge": search_knowledge,
    "parse_document": parse_document,
    "analyze_image": analyze_image,
    "write_file": write_file,
    "run_code": run_code,
    "read_file": read_file,
}

TOOL_DESCRIPTIONS = """
Available tools (call exactly ONE per response):
1. search_knowledge(query: str) — Search internal SOPs, manuals, correspondence.
2. parse_document(file_path: str) — Parse a PDF/scanned doc via OCR.
3. analyze_image(image_path: str, question: str) — Analyze an image (P&ID, photo, handwriting).
4. write_file(filename: str, content: str) — Write a file. REQUIRES APPROVAL.
5. run_code(code: str) — Execute Python in sandbox. REQUIRES APPROVAL.
6. read_file(file_path: str) — Read a file from the workspace.
"""

TOOL_FORMAT = """To call a tool, respond with ONLY this JSON (no other text):
{"tool": "tool_name", "args": {"param1": "value1"}}

To give your FINAL answer (no more tool calls), respond with ONLY:
{"final": "your complete answer text here"}

Rules:
- Call exactly ONE tool per response, then wait for its result.
- Use tools step by step — don't try to do everything at once.
- After you have enough information, use the "final" format.
- NEVER write JSON as part of other text — it must be the ENTIRE response."""


class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    task_type: Optional[str]
    tool_plan: Optional[List[str]]


# ─── Node 1: Classify (with keyword hardening) ──────────────
VISION_KEYWORDS = ("image", "photo", "picture", "handwritten", "note.jpg",
                   "note.jpeg", "png", "drawing", "diagram", "pid",
                   "site note", "what do you see", "look at")
CODING_KEYWORDS = ("code", "function", "script", "python", "calculate",
                   "formula", "compute", "execute", "run", "barlow",
                   "verify", "debug")
DOCUMENT_KEYWORDS = ("approval note", "word document", "docx", "draft",
                     "create a document", "write a note", "generate a file",
                     "as a word", "as word", "spreadsheet", "excel",
                     "xlsx", "presentation", "pptx", "powerpoint",
                     "draft a note", "create a file", "make a document")
SEARCH_KEYWORDS = ("what is the", "what are the", "how does", "sop",
                   "process", "procedure", "policy", "approval process",
                   "search", "find", "manual", "knowledge base")

def classify_task(state: AgentState) -> Dict:
    # Get the last human message
    last_human = None
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            last_human = msg.content
            break
    user_text = (last_human or "").lower()

    # ── Keyword fast-path ──
    if any(kw in user_text for kw in VISION_KEYWORDS):
        return {"task_type": "vision"}
    if any(kw in user_text for kw in CODING_KEYWORDS):
        return {"task_type": "coding"}
    if any(kw in user_text for kw in DOCUMENT_KEYWORDS):
        return {"task_type": "document"}
    if any(kw in user_text for kw in SEARCH_KEYWORDS):
        return {"task_type": "search"}

    # ── LLM fallback for ambiguous cases ──
    classifier_llm = ChatOpenAI(
        model=MODEL_CLASSIFIER,
        base_url=LITELLM_BASE_URL,
        api_key=LITELLM_API_KEY,
        max_tokens=20,
        temperature=0,
    )

    system_prompt = """Classify the user's request into ONE word:
- coding — write/debug/execute code or calculations
- document — parse documents, draft notes, create files
- vision — analyze images, photos, drawings, handwriting
- search — search internal knowledge base
- general — anything else

Reply with ONLY the word, nothing else."""

    response = classifier_llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=last_human or "general"),
    ])

    task_type = response.content.strip().lower()
    match = re.search(r'(coding|document|vision|search|general)', task_type)
    task_type = match.group(1) if match else "general"

    return {"task_type": task_type}

# ─── Node 2: Agent reasoning (explicit JSON format) ─────────
def agent_reasoning(state: AgentState) -> Dict:
    task_type = state.get("task_type") or "general"
    model_alias = {
        "coding": MODEL_CODER,
        "document": MODEL_GENERAL,
        "vision": MODEL_VISION,
        "search": MODEL_GENERAL,
        "general": MODEL_GENERAL,
    }.get(task_type, MODEL_GENERAL)

    llm = ChatOpenAI(
        model=model_alias,
        base_url=LITELLM_BASE_URL,
        api_key=LITELLM_API_KEY,
        max_tokens=1500,
        temperature=0,
    )

    # ── List available files so the model uses correct paths ──
    from pathlib import Path
    input_files = [f.name for f in Path("/workspace/inputs").rglob("*") if f.is_file()]
    file_listing = "\n".join(f"  - inputs/{f}" for f in input_files) or "  (none)"

    task_prompts = {
        "coding": "You are an industrial coding assistant. Write code, then ALWAYS verify it with run_code before giving your final answer.",
        "document": """You are an industrial document assistant. When the user asks for a document to be created (approval note, summary, report as .docx/.xlsx/.pptx), you MUST call write_file with the full content. NEVER just summarize in your final answer — always produce the actual file. Steps: 1) parse or search for source material, 2) call write_file with the complete document content, 3) confirm the file was created in your final answer.""",
        "vision": "You are an industrial vision assistant. Use analyze_image for images. Report what you see.",
        "search": "You are a knowledge search assistant. Use search_knowledge, then answer with citations.",
        "general": "You are an industrial AI assistant.",
    }

    system_prompt = f"""{task_prompts.get(task_type, task_prompts['general'])}

{TOOL_DESCRIPTIONS}

Available files in the workspace:
{file_listing}

Use these EXACT filenames when calling tools. Do not guess filenames.

{TOOL_FORMAT}"""

    # Build message list: system + conversation + tool results
    messages = [SystemMessage(content=system_prompt)]
    for msg in state["messages"]:
        if isinstance(msg, HumanMessage):
            messages.append(msg)
        elif isinstance(msg, ToolMessage):
            messages.append(HumanMessage(content=f"[TOOL RESULT] {msg.content}"))
        elif isinstance(msg, AIMessage):
            if msg.content and not msg.content.startswith('{"'):
                messages.append(AIMessage(content=msg.content))

    response = llm.invoke(messages)

    # ── Handle empty content (model put output in reasoning) ──
    if not response.content or not response.content.strip():
        # Check if reasoning_content has something
        reasoning = getattr(response, 'additional_kwargs', {}).get('reasoning_content', '')
        if reasoning:
            # Try to extract the JSON or final answer from the reasoning
            import re as _re
            json_match = _re.search(r'\{[^{}]*"tool"[^{}]*\}', reasoning)
            final_match = _re.search(r'\{[^{}]*"final"[^{}]*\}', reasoning)
            if json_match:
                response = AIMessage(content=json_match.group())
            elif final_match:
                response = AIMessage(content=final_match.group())
            else:
                # Last 500 chars of reasoning might have the answer
                response = AIMessage(content=reasoning[-500:])
        else:
            # Completely empty — retry once with simpler message
            retry_messages = messages[:2]  # system + last message only
            if len(messages) > 2:
                retry_messages.append(messages[-1])
            response = llm.invoke(retry_messages)

    return {"messages": [response]}


# ─── Node 3: Parse response and route ───────────────────────
def parse_and_route(state: AgentState) -> Dict:
    """
    Parses the agent's response. Handles:
    1. Direct JSON: {"tool": ..., "args": {...}} or {"final": "..."}
    2. JSON embedded in text (with preamble)
    3. JSON with nested objects (like args containing content strings with braces)
    4. Non-JSON text → treated as final answer
    """
    last = state["messages"][-1]
    content = last.content if hasattr(last, 'content') else ""
    
    # DEBUG logging
    print(f"\n[PARSE DEBUG] content type: {type(last).__name__}")
    print(f"[PARSE DEBUG] content (first 200): {content[:200]}")
    print(f"[PARSE DEBUG] has tool_calls: {hasattr(last, 'tool_calls') and bool(last.tool_calls)}\n")

    # ── Method 1: Try direct JSON parse (strip code fences first) ──
    cleaned = content.strip()
    if cleaned.startswith("```"):
        # Remove markdown code fences
        cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned)
        cleaned = re.sub(r'\n?```\s*$', '', cleaned)

    parsed = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # ── Method 2: Find JSON object boundaries (brace counting) ──
    if not parsed:
        start = content.find('{')
        if start != -1:
            # Count braces to find the matching close
            depth = 0
            in_string = False
            escape = False
            end = -1
            for i, char in enumerate(content[start:], start):
                if escape:
                    escape = False
                    continue
                if char == '\\' and in_string:
                    escape = True
                    continue
                if char == '"' and not escape:
                    in_string = not in_string
                    continue
                if in_string:
                    continue  # inside a string — braces don't count
                if char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1
                    if depth == 0:
                        end = i
                        break

            if end != -1:
                json_str = content[start:end + 1]
                try:
                    parsed = json.loads(json_str)
                except json.JSONDecodeError:
                    # JSON might have unescaped quotes in content — try fixing
                    fixed = json_str.replace('\n', '\\n').replace('\t', '\\t')
                    try:
                        parsed = json.loads(fixed)
                    except json.JSONDecodeError:
                        pass

    # ── Method 3: Look for "final": pattern as fallback ──
    if not parsed:
        final_match = re.search(r'"final"\s*:\s*"(.*)"\s*\}', content, re.DOTALL)
        if final_match:
            final_text = final_match.group(1)
            # Unescape
            final_text = final_text.replace('\\n', '\n').replace('\\"', '"')
            return {"messages": [AIMessage(content=final_text)], "task_type": state.get("task_type")}

    # ── Handle parsed result ──────────────────────────────────
    if not parsed:
        # Can't parse — treat as final answer
        return {"messages": [AIMessage(content=content)], "task_type": state.get("task_type")}

    if isinstance(parsed, dict):
        if "final" in parsed:
            return {"messages": [AIMessage(content=str(parsed["final"]))], "task_type": state.get("task_type")}

        if "tool" in parsed and "args" in parsed:
            tool_name = str(parsed["tool"])
            tool_args = parsed["args"]

            if tool_name not in TOOL_MAP:
                error_msg = AIMessage(content=f'[TOOL ERROR] Unknown tool "{tool_name}". Valid: {list(TOOL_MAP.keys())}')
                return {"messages": [error_msg], "task_type": state.get("task_type")}

            # Convert to structured tool call
            tool_call_msg = AIMessage(
                content="",
                tool_calls=[{
                    "name": tool_name,
                    "args": tool_args,
                    "id": f"call_{hash(str(tool_args)) & 0xFFFFFFFF:08x}",
                }],
            )
            return {"messages": [tool_call_msg], "task_type": state.get("task_type")}

    # Unrecognized structure — treat as final
    return {"messages": [AIMessage(content=content)], "task_type": state.get("task_type")}


# ─── Node 4: Approval gate ──────────────────────────────────
def human_approval(state: AgentState) -> Dict:
    last_message = state["messages"][-1]
    if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
        return {}

    sensitive_calls = [tc for tc in last_message.tool_calls if tc["name"] in SENSITIVE_TOOLS]
    if not sensitive_calls:
        return {}

    decision = interrupt({
        "type": "approval_required",
        "pending_calls": [
            {"tool": tc["name"], "args_preview": str(tc["args"])[:500]}
            for tc in sensitive_calls
        ],
        "message": f"Agent wants to execute {len(sensitive_calls)} sensitive action(s).",
    })

    if decision.get("type") == "reject":
        return {
            "messages": [
                ToolMessage(
                    content="Action rejected by human operator.",
                    tool_call_id=tc["id"],
                )
                for tc in sensitive_calls
            ]
        }
    return {}


# ─── Node 5: Execute tools ──────────────────────────────────
def execute_tools(state: AgentState) -> Dict:
    """Execute the pending tool call directly (no ToolNode)."""
    last = state["messages"][-1]
    if not hasattr(last, "tool_calls") or not last.tool_calls:
        return {}

    results = []
    for tc in last.tool_calls:
        tool_name = tc["name"]
        tool_args = tc["args"]
        tool_func = TOOL_MAP.get(tool_name)

        if tool_func is None:
            results.append(ToolMessage(
                content=f'ERROR: Unknown tool {tool_name}',
                tool_call_id=tc["id"],
            ))
            continue

        try:
            result = tool_func.invoke(tool_args)
            results.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
        except Exception as e:
            results.append(ToolMessage(
                content=f'ERROR executing {tool_name}: {str(e)}',
                tool_call_id=tc["id"],
            ))

    return {"messages": results}


# ─── Build the graph ────────────────────────────────────────
def build_agent_graph():
    """
    START → classify → agent → parse → 
        final answer → END
        tool call → approval → tools → agent (loop)
    """
    graph = StateGraph(AgentState)

    graph.add_node("classify", classify_task)
    graph.add_node("agent", agent_reasoning)
    graph.add_node("parse", parse_and_route)
    graph.add_node("approval", human_approval)
    graph.add_node("tools", execute_tools)

    graph.add_edge(START, "classify")
    graph.add_edge("classify", "agent")
    graph.add_edge("agent", "parse")

    # After parse: check if it's a tool call or final answer
    def route_after_parse(state: AgentState) -> str:
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "to_approval"
        return "done"

    graph.add_conditional_edges(
        "parse",
        route_after_parse,
        {"to_approval": "approval", "done": END},
    )

    def route_after_approval(state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, ToolMessage) and "rejected" in str(last.content).lower():
            return "back_to_agent"
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "to_tools"
        return "back_to_agent"

    graph.add_conditional_edges(
        "approval",
        route_after_approval,
        {"to_tools": "tools", "back_to_agent": "agent"},
    )

    graph.add_edge("tools", "agent")

    memory = MemorySaver()
    return graph.compile(checkpointer=memory)
