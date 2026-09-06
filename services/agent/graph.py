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
    write_file, run_code, read_file, list_files, grep_codebase
)

from openai import OpenAI as RawOpenAI

# Module-level raw client for token-level streaming (bypasses langchain's wrapper
# so we get direct access to delta.reasoning_content for the thinking tokens)
_raw_llm_client = RawOpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY)

TOOL_MAP = {
    "search_knowledge": search_knowledge,
    "parse_document": parse_document,
    "analyze_image": analyze_image,
    "write_file": write_file,
    "run_code": run_code,
    "read_file": read_file,
    "list_files": list_files,
    "grep_codebase": grep_codebase
}

TOOL_DESCRIPTIONS = """Available tools (call exactly ONE per response):
1. search_knowledge(query: str) — Search internal SOPs, manuals, correspondence semantically. Use for policy/procedure questions ("what is the approval process").
2. parse_document(file_path: str) — Parse a PDF/scanned doc via OCR. Returns full text content.
3. analyze_image(image_path: str, question: str) — Analyze an image (P&ID, photo, handwriting). Use for visual documents.
4. write_file(filename: str, content: str) — Write a file to outputs (.docx/.xlsx/.pptx/.py). REQUIRES APPROVAL.
5. run_code(code: str) — Execute Python in sandbox. Use to verify calculations or test code. REQUIRES APPROVAL.
6. read_file(file_path: str) — Read a file from the workspace. Use after grep/list to inspect specific files.
7. grep_codebase(pattern: str, path: str, file_glob: str) — Search for exact text/regex across files. Returns file:line matches. Use for "where is X defined/used", finding symbols, equipment IDs, error messages.
8. list_files(path: str) — List files/directories. Use to explore project structure before grepping or reading.

Tool selection guide:
- Policy/SOP/procedure questions ("how does X work", "what is the process for") → search_knowledge
- Exact lookups ("where is calculate_thickness", "find HX-301", "which file has the leak") → grep_codebase
- Exploring an unfamiliar codebase → list_files, then grep_codebase, then read_file
- Scanned/handwritten/image documents → parse_document or analyze_image
- Producing deliverables → write_file"""

TOOL_FORMAT = """To call a tool, respond with ONLY this JSON (no other text):
{"tool": "tool_name", "args": {"param1": "value1"}}

To give your FINAL answer (no more tool calls), respond with ONLY:
{"final": "your complete answer text here"}

Rules:
- Call exactly ONE tool per response, then wait for its result.
- When you see [TOOL RESULT], the tool has ALREADY EXECUTED. Read the result.
- If a [TOOL RESULT] is already in the conversation, you MUST output {"final": ...} — do NOT call the same tool again.
- NEVER call a tool whose result is already visible in the conversation.
- Each tool may be called at most ONCE per question.
- After you have enough information, always use the "final" format.
- NEVER write JSON as part of other text — it must be the ENTIRE response."""


class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    task_type: Optional[str]
    tool_plan: Optional[List[str]]
    reasoning_text: Optional[str]   # ← NEW: batched reasoning from last LLM call


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
def _get_writer():
    """Get the LangGraph stream writer. Requires langgraph >= 0.2.45."""
    from langgraph.config import get_stream_writer
    return get_stream_writer()


def _convert_messages_for_api(state_messages, system_prompt):
    """Convert langchain message objects to OpenAI API dicts.
    Tool-call AIMessages are rendered as visible assistant actions so the
    model knows it already called a tool and can move to the final answer."""
    api_messages = [{"role": "system", "content": system_prompt}]
    for msg in state_messages:
        if isinstance(msg, HumanMessage):
            api_messages.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            # Tool-call messages: render the call as a visible assistant action
            if msg.tool_calls:
                tc = msg.tool_calls[0]
                call_desc = f'[I called tool "{tc["name"]}" with args: {tc["args"]}]'
                api_messages.append({"role": "assistant", "content": call_desc})
            # Clean text responses (final answers)
            elif msg.content and not msg.content.startswith('{"'):
                api_messages.append({"role": "assistant", "content": msg.content})
        elif isinstance(msg, ToolMessage):
            api_messages.append({"role": "user", "content": f"[TOOL RESULT for my previous call] {msg.content}"})
    return api_messages

def agent_reasoning(state: AgentState) -> Dict:
    """
    The core agent node — now with token-level streaming.
    Reasoning tokens (the model's thinking) and content tokens are
    emitted as custom stream events while the full response accumulates.
    """
    task_type = state.get("task_type") or "general"
    model_alias = {
        "coding": MODEL_CODER,
        "document": MODEL_GENERAL,
        "vision": MODEL_VISION,
        "search": MODEL_GENERAL,
        "general": MODEL_GENERAL,
    }.get(task_type, MODEL_GENERAL)

    writer = _get_writer()

    # ── Build the system prompt (same as before) ────────────
    from pathlib import Path
    input_files = [f.name for f in Path("/workspace/inputs").rglob("*") if f.is_file()]
    file_listing = "\n".join(f"  - inputs/{f}" for f in input_files) or "  (none)"

    task_prompts = {
        "coding": "You are an industrial coding assistant. Write code, verify with run_code.",
        "document": "You are an industrial document assistant. Parse docs, search SOPs, create deliverables.",
        "vision": "You are an industrial vision assistant. Use analyze_image for images.",
        "search": "You are a knowledge search assistant. Use search_knowledge.",
        "general": "You are an industrial AI assistant.",
    }

    system_prompt = f"""{task_prompts.get(task_type, task_prompts['general'])}
                        {TOOL_DESCRIPTIONS}
                        Available files in the workspace:
                        {file_listing}
                        Use these EXACT filenames when calling tools. Do not guess filenames.
                        {TOOL_FORMAT}
                    """

    api_messages = _convert_messages_for_api(state["messages"], system_prompt)

    # ── STREAM the LLM call instead of one blocking invoke ──
    writer({"type": "reasoning_start"})
    
    content_chunks = []
    reasoning_chunks = []
    saw_reasoning = False
    
    try:
        stream = _raw_llm_client.chat.completions.create(
            model=model_alias,
            messages=api_messages,
            max_tokens=1500,
            temperature=0,
            stream=True,
        )

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                saw_reasoning = True
                reasoning_chunks.append(reasoning)
                writer({"type": "reasoning", "content": reasoning})   # ← per-token emit

            if delta.content:
                content_chunks.append(delta.content)
                # Content tokens are NOT emitted — they're JSON tool calls
                # that look ugly raw. parse_and_route cleans them up.

    except Exception as e:
        # Streaming failed — fall back to non-streaming invoke
        writer({"type": "error", "message": f"stream error: {e}"})
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=model_alias,
            base_url=LITELLM_BASE_URL,
            api_key=LITELLM_API_KEY,
            max_tokens=1500,
            temperature=0,
        )
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
        writer({"type": "reasoning_end"})
        return {"messages": [response], "reasoning_text": ""}

    writer({"type": "reasoning_end"})
    full_content = "".join(content_chunks)
    reasoning_text = "".join(reasoning_chunks)   # ← NEW

    # Handle empty content (model put everything in reasoning)
    if not full_content.strip() and saw_reasoning:
        full_content = '{"final": "I was thinking but produced no answer. Please rephrase."}'
    elif not full_content.strip():
        full_content = '{"final": ""}'

    response = AIMessage(content=full_content)
    return {"messages": [response], "reasoning_text": reasoning_text}


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
            return {
                "messages": [AIMessage(content=str(parsed["final"]))],
                "task_type": state.get("task_type"),
            }

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
            result_str = str(result)
            results.append(ToolMessage(          # ← ADD THIS LINE (and the next)
                content=result_str,
                tool_call_id=tc["id"],
            ))
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
