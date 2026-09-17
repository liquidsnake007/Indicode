"""
LangGraph agent with explicit JSON tool-calling.
"""
import json
import os
import re
from typing import Annotated, TypedDict, Optional, List, Dict
from langchain_core.messages import (
    BaseMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage
)
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END, START
from langgraph.graph.message import add_messages
from langgraph.checkpoint.sqlite import SqliteSaver
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

# Use the raw client to stream reasoning tokens directly.
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
4. write_file(filename: str, content: str) — Write a file to outputs (.docx/.xlsx/.pptx/.py). REQUIRES APPROVAL. For .pptx, separate slides with "## Slide: Title" lines.
5. run_code(code: str) — Execute Python in sandbox. Use to verify calculations or test code. REQUIRES APPROVAL.
6. read_file(file_path: str) — Read a file from the workspace. Use after grep/list to inspect specific files.
7. grep_codebase(pattern: str, path: str, file_glob: str) — Search for exact text/regex across files. Returns file:line matches. Use for "where is X defined/used", finding symbols, equipment IDs, error messages.
8. list_files(path: str) — List files/directories. Use to explore project structure before grepping or reading.

Tool selection guide:
- Policy/SOP/procedure questions → search_knowledge
- Exact lookups ("where is X", "find Y") → grep_codebase (searches the ENTIRE workspace)
- Exploring code → ALWAYS list_files(path="") FIRST to see the full directory structure, THEN grep_codebase, THEN read_file
- NEVER guess where files are. Use list_files to discover the structure before searching.
- The workspace contains inputs/ (source documents) AND outputs/ (generated files). Search BOTH.
- Scanned/handwritten/image documents → parse_document or analyze_image
- Producing deliverables → write_file """

TOOL_FORMAT = """To call a tool, respond with ONLY this JSON (no other text):
{"tool": "tool_name", "args": {"param1": "value1"}}

To give your FINAL answer (no more tool calls), respond with ONLY:
{"final": "your complete answer text here"}

Rules:
- ALWAYS use tools when they are needed. NEVER say "I can't do X" — check if a tool can do it.
- NEVER show code in your response. Use run_code to execute it and write_file to save it.
- Call exactly ONE tool per response, then wait for its result.
- When you see [TOOL RESULT], the tool has ALREADY EXECUTED. Read the result.
- If a [TOOL RESULT] is already in the conversation, output {"final": ...} — do NOT call the same tool again.
- Each tool may be called at most ONCE per question (unless the result was an error and you're fixing the args).
- After you have enough information, always use the "final" format.
- NEVER write JSON as part of other text — it must be the ENTIRE response."""

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    task_type: Optional[str]
    tool_plan: Optional[List[str]]
    reasoning_text: Optional[str]


# Classify requests with keywords and a model fallback.
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
    # Find the latest user message.
    last_human = None
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            last_human = msg.content
            break
    user_text = (last_human or "").lower()

    # Use deterministic keywords before the model fallback.
    if any(kw in user_text for kw in VISION_KEYWORDS):
        return {"task_type": "vision"}
    if any(kw in user_text for kw in CODING_KEYWORDS):
        return {"task_type": "coding"}
    if any(kw in user_text for kw in DOCUMENT_KEYWORDS):
        return {"task_type": "document"}
    if any(kw in user_text for kw in SEARCH_KEYWORDS):
        return {"task_type": "search"}

    # Classify ambiguous requests with the fallback model.
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

# Generate agent responses in the explicit JSON format.
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
                # Render in the SAME JSON format we want the model to output
                call_desc = json.dumps({"tool": tc["name"], "args": tc["args"]})
                api_messages.append({"role": "assistant", "content": call_desc})
            # Clean text responses (final answers)
            elif msg.content and not msg.content.startswith('{"'):
                api_messages.append({"role": "assistant", "content": msg.content})
        elif isinstance(msg, ToolMessage):
            api_messages.append({"role": "user", "content": f"[TOOL RESULT for my previous call] {msg.content}"})
    return api_messages

def agent_reasoning(state: AgentState) -> Dict:
    """
    The core agent node — token-level streaming with retry on empty content.
    """
    task_type = state.get("task_type") or "general"
    model_alias = {
        "coding": MODEL_CODER,
        "document": MODEL_GENERAL,
        "vision": MODEL_VISION,
        "search": MODEL_GENERAL,
        "general": MODEL_GENERAL,
    }.get(task_type, MODEL_GENERAL)

    max_tokens = {
        "document": 6000,
        "coding": 3000,
        "vision": 2000,
        "search": 2000,
        "general": 2000,
    }.get(task_type, 2000)

    writer = _get_writer()

    from pathlib import Path
    workspace_files = []
    for f in Path("/workspace").rglob("*"):
        if f.is_file() and ".sandbox" not in str(f):
            rel = f.relative_to("/workspace")
            workspace_files.append(f"  {rel}")
    file_listing = "\n".join(workspace_files[:30]) or "  (empty)"
    if len(workspace_files) > 30:
        file_listing += f"\n  ... and {len(workspace_files) - 30} more files"

    task_prompts = {
        "coding": """You are Indicode, an industrial coding assistant.

        Workflow (follow this EXACT sequence, then STOP):
        1. Call run_code ONCE to verify the calculation works.
        2. After seeing the result, call write_file ONCE to save the code as a .py file.
        3. After both results are visible, output {"final": "..."} immediately.

        STOPPING RULES (these override everything else):
        - If you see a [TOOL RESULT for my previous call] from run_code in the conversation, do NOT call run_code again. Move to step 2.
        - If you see a [TOOL RESULT for my previous call] from write_file in the conversation, do NOT call any more tools. Output {"final": "..."} now.
        - Never call the same tool twice. Never call more than 2 tools total for a coding task.

        If you have already called run_code and write_file and both succeeded, your ONLY valid response is {"final": "..."}.

        Never show code in your response text. Never say "I can't write files" — use the write_file tool.""",
        "document": """You are an industrial document assistant.

        CRITICAL RULES:
        1. When the user asks for a document, call write_file ONCE with the full content.
        2. After write_file succeeds, output {"final": "..."} immediately.
        3. NEVER echo the user's prompt back to them.
        4. NEVER repeat the document content in your final answer — just confirm the file was created.

        STOPPING RULES:
        - If you see a [TOOL RESULT for my previous call] from write_file, output {"final": "..."} NOW.
        - If you see a [TOOL RESULT for my previous call] from parse_document or search_knowledge, call write_file NEXT.
        - Maximum 3 tool calls total for any document task.

        Document content format (for the write_file content parameter):
        - Use # for the title, ## for section headings
        - Use | table | rows | for tables
        - Use **bold** for emphasis
        - Use - for bullet points""",
        "vision": "You are an industrial vision assistant. Use analyze_image for images.",
        "search": "You are a knowledge search assistant. Call search_knowledge ONCE, then answer with citations.",
        "general": """You are an industrial AI assistant.

        Answer ordinary questions directly and helpfully. Tools are optional,
        not mandatory: use them only when the answer depends on internal
        documents, workspace files, images, calculations, or creating a file.
        If no tool is needed, answer now using {\"final\": \"...\"}. Never
        refuse or ask the user to tell you to continue merely because no tool
        applies.""",
    }

    default_prompt = task_prompts.get("general", "You are an industrial AI assistant.")
    task_prompt = task_prompts.get(task_type, default_prompt)

    system_prompt = f"""{task_prompt}
                        {TOOL_DESCRIPTIONS}
                        Available files in the workspace:
                        {file_listing}
                        Use these EXACT filenames when calling tools. Do not guess filenames.
                        {TOOL_FORMAT}
                    """

    api_messages = _convert_messages_for_api(state["messages"], system_prompt)

    # ── STREAM the LLM call ──────────────────────────────────
    writer({"type": "reasoning_start"})

    content_chunks = []
    reasoning_chunks = []
    saw_reasoning = False

    try:
        stream = _raw_llm_client.chat.completions.create(
            model=model_alias,
            messages=api_messages,
            max_tokens=max_tokens,
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
                writer({"type": "reasoning", "content": reasoning})

            if delta.content:
                content_chunks.append(delta.content)

    except Exception as e:
        # Streaming failed — fall back to non-streaming invoke
        writer({"type": "error", "message": f"stream error: {e}"})
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=model_alias,
            base_url=LITELLM_BASE_URL,
            api_key=LITELLM_API_KEY,
            max_tokens=max_tokens,
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
    reasoning_text = "".join(reasoning_chunks)

    # Retry when thinking consumes the response budget.
    if not full_content.strip() and saw_reasoning:
        writer({"type": "retry", "message": "Empty content, retrying with direct instruction"})

        # Extract the original request for the retry.
        user_request = ""
        for msg in reversed(state["messages"]):
            if isinstance(msg, HumanMessage):
                user_request = msg.content[:500]  # truncate for retry prompt
                break

        # Build a short retry prompt that asks for an immediate tool call.
        retry_messages = [
            {"role": "system", "content": f"""{task_prompt}

{TOOL_DESCRIPTIONS}

{TOOL_FORMAT}

CRITICAL: Output the JSON tool call NOW. Do not think extensively. Generate the write_file call immediately."""},
            {"role": "user", "content": f"Create the document now. User's request: {user_request}"},
        ]

        try:
            retry_stream = _raw_llm_client.chat.completions.create(
                model=model_alias,
                messages=retry_messages,
                max_tokens=max_tokens,
                temperature=0,
                stream=True,
            )
            retry_content = []
            for chunk in retry_stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                if delta.content:
                    retry_content.append(delta.content)

            full_content = "".join(retry_content)
            if full_content.strip():
                # Use the retry response when it contains content.
                writer({"type": "retry_success"})
        except Exception:
            pass  # Retry also failed, fall through to the empty check below

    # Handle still-empty content
    if not full_content.strip():
        full_content = '{"final": "I generated reasoning but no actionable content. The prompt may be too long — try a shorter version or use /prompt to send from a file."}'
        if not saw_reasoning:
            full_content = '{"final": ""}'

    response = AIMessage(content=full_content)
    return {"messages": [response], "reasoning_text": reasoning_text}


# Parse the model response and route tool calls.
def parse_and_route(state: AgentState) -> Dict:
    """
    Parses the agent's response. Handles:
    1. Direct JSON: {"tool": ..., "args": {...}} or {"final": "..."}
    2. JSON embedded in text (with preamble)
    3. JSON with nested objects (brace counting)
    4. Narrative tool-call format
    5. Echo-back detection (model parroting the user's prompt)
    """
    last = state["messages"][-1]
    content = last.content if hasattr(last, 'content') else ""

    # Keep parser diagnostics available during development.
    print(f"\n[PARSE DEBUG] content type: {type(last).__name__}")
    print(f"[PARSE DEBUG] content (first 200): {content[:200]}")
    print(f"[PARSE DEBUG] has tool_calls: {hasattr(last, 'tool_calls') and bool(last.tool_calls)}\n")

    # Reject responses that simply echo the user's prompt.
    if len(content) > 200:
        for msg in reversed(state["messages"][:-1]):
            if isinstance(msg, HumanMessage) and msg.content:
                # Compare the leading portions of the response and prompt.
                content_start = content[:150].strip().lower()
                prompt_start = msg.content[:150].strip().lower()
                # Treat a matching prefix as an echo.
                if content_start[:80] == prompt_start[:80]:
                    print(f"[PARSE DEBUG] ECHO DETECTED — rejecting")
                    return {
                        "messages": [AIMessage(
                            content='{"final": "The model echoed your prompt instead of generating a document. Please try again — if the prompt is very long, use /prompt to send it from a file."}'
                        )],
                        "task_type": state.get("task_type"),
                    }
                break  # only check the most recent user message

    # ── Method 1: Try direct JSON parse (strip code fences) ──
    cleaned = content.strip()
    if cleaned.startswith("```"):
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
                    continue
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
            final_text = final_text.replace('\\n', '\n').replace('\\"', '"')
            return {"messages": [AIMessage(content=final_text)], "task_type": state.get("task_type")}

    # ── Method 4: Recognize narrative tool-call format ──
    if not parsed and content.strip().startswith("[I called tool"):
        narr_match = re.search(
            r'\[I called tool "([^"]+)" with args: (\{.*\})\]',
            content, re.DOTALL
        )
        if narr_match:
            tool_name = narr_match.group(1)
            try:
                tool_args = json.loads(narr_match.group(2))
                parsed = {"tool": tool_name, "args": tool_args}
            except json.JSONDecodeError:
                pass

    # ── Handle parsed result ──────────────────────────────────
    if not parsed:
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

            tool_call_msg = AIMessage(
                content="",
                tool_calls=[{
                    "name": tool_name,
                    "args": tool_args,
                    "id": f"call_{hash(str(tool_args)) & 0xFFFFFFFF:08x}",
                }],
            )
            return {"messages": [tool_call_msg], "task_type": state.get("task_type")}

    return {"messages": [AIMessage(content=content)], "task_type": state.get("task_type")}

# Pause sensitive tool calls for human approval.
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


# Execute approved tool calls.
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


# Build the persistent agent graph.
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

    import sqlite3
    db_path = os.environ.get("AGENT_DB_PATH", "/app/state/agent_state.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    memory = SqliteSaver(conn)
    return graph.compile(checkpointer=memory)