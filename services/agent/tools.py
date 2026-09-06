"""
LangGraph tool definitions — each wraps a Phase 2 service.
The agent plans multi-step work by calling these; it does NOT
answer once and stop.
"""
import httpx
import json
import base64
from pathlib import Path
from typing import Optional
from langchain_core.tools import tool
import mimetypes
from openai import OpenAI

from config import (
    RAG_SERVICE_URL, DOCLING_SERVICE_URL, LITELLM_BASE_URL,
    LITELLM_API_KEY, MODEL_VISION, MAX_TOKENS_VISION,
    OUTPUT_DIR, INPUT_DIR, WORKSPACE_DIR,
)

client = OpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY)

def _resolve_path(file_path: str) -> Path:
    """Resolve a file path, handling partial matches and common variations."""
    if file_path.startswith("/"):
        path = Path(file_path)
        if path.exists():
            return path
    else:
        path = Path(WORKSPACE_DIR) / file_path
        if path.exists():
            return path
        path = Path(INPUT_DIR) / file_path
        if path.exists():
            return path

    # Try fuzzy matching — search all files for partial name match
    search_name = Path(file_path).name.lower()
    for candidate in Path(INPUT_DIR).rglob("*"):
        if candidate.is_file():
            # Exact name match
            if candidate.name.lower() == search_name:
                return candidate
            # Partial match (one contains the other)
            if search_name in candidate.name.lower() or candidate.name.lower() in search_name:
                # Only match if extensions are compatible or missing
                if not Path(file_path).suffix or candidate.suffix.lower() == Path(file_path).suffix.lower():
                    return candidate
            # Name without extension match
            if candidate.stem.lower() == Path(file_path).stem.lower():
                return candidate

    # No match — return the original path (tool will report not found)
    return Path(INPUT_DIR) / Path(file_path).name

# ═════════════════════════════════════════════════════════════
# TOOL: search_knowledge — RAG retrieval over internal docs
# ═════════════════════════════════════════════════════════════
@tool
def search_knowledge(query: str) -> str:
    """Search the organization's internal knowledge base (SOPs, manuals,
    past correspondence, inspection reports). Returns the most relevant
    passages with source attribution. Use this BEFORE drafting any
    approval note or answering policy questions."""
    response = httpx.post(
        f"{RAG_SERVICE_URL}/search",
        json={"query": query},
        timeout=30.0,
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("results"):
        return "No relevant documents found in the knowledge base."

    # Format for the LLM: passages + sources
    parts = [f"Found {len(result['results'])} relevant passages:\n"]
    for i, r in enumerate(result["results"], 1):
        parts.append(f"[Passage {i} | Source: {r['filename']}]\n{r['text']}\n")
    return "\n---\n".join(parts)


# ═════════════════════════════════════════════════════════════
# TOOL: parse_document — Docling OCR for scanned PDFs/images
# ═════════════════════════════════════════════════════════════
@tool
def parse_document(file_path: str) -> str:
    """Parse a document (PDF, scanned PDF, image) into text using OCR.
    Use this when you need to read a scanned inspection report or any
    image-based document. file_path is relative to /workspace/inputs/."""
    path = _resolve_path(file_path)

    if not path.exists():
        return f"ERROR: File not found: {file_path}. Available: {[f.name for f in INPUT_DIR.rglob('*') if f.is_file()]}"

    # Read the file and send to Docling
    with open(path, "rb") as f:
        response = httpx.post(
            f"{DOCLING_SERVICE_URL}/parse",
            files={"file": (path.name, f, "application/octet-stream")},
            timeout=300.0,  # OCR can take time on first parse
        )
    response.raise_for_status()
    result = response.json()

    markdown = result.get("markdown", "")
    if result.get("from_cache"):
        return f"[CACHED — instant] Parsed {path.name}:\n\n{markdown}"
    return f"Parsed {path.name}:\n\n{markdown}"


# ═════════════════════════════════════════════════════════════
# TOOL: analyze_image — vision model for P&IDs, photos, handwriting
# ═════════════════════════════════════════════════════════════
@tool
def analyze_image(image_path: str, question: str) -> str:
    """Analyze an image (engineering drawing, P&ID, photograph of
    equipment, handwritten note) using the vision model. Ask a specific
    question about the image. image_path is relative to /workspace/inputs/."""
    path = _resolve_path(image_path)

    if not path.exists():
        return f"ERROR: Image not found: {image_path}. Available images: {[f.name for f in INPUT_DIR.rglob('*') if f.suffix in ('.jpg','.jpeg','.png','.gif','.bmp')]}"

    # Detect actual MIME type from the file extension
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type is None or not mime_type.startswith("image/"):
        # Fallback: sniff the magic bytes
        with open(path, "rb") as f:
            header = f.read(8)
        if header.startswith(b"\x89PNG"):
            mime_type = "image/png"
        elif header.startswith(b"\xff\xd8\xff"):
            mime_type = "image/jpeg"
        elif header.startswith(b"GIF8"):
            mime_type = "image/gif"
        elif header.startswith(b"BM"):
            mime_type = "image/bmp"
        else:
            return f"ERROR: Unsupported image format: {path.name}"

    # Read and encode
    with open(path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    # Call through LiteLLM (sovereign-vision alias) with correct MIME
    response = client.chat.completions.create(
        model=MODEL_VISION,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{img_b64}"}},
            ],
        }],
        max_tokens=MAX_TOKENS_VISION,
        temperature=0,
    )

    content = response.choices[0].message.content
    if not content:
        return "ERROR: Vision model returned empty content (possibly exceeded token budget)."
    return content


# ═════════════════════════════════════════════════════════════
# TOOL: write_file — generate deliverables (REQUIRES APPROVAL)
# ═════════════════════════════════════════════════════════════
@tool
def write_file(filename: str, content: str) -> str:
    """Write generated content to a file. For Word documents (.docx),
    content should be structured markdown. For Python scripts (.py),
    content is the source code. Output goes to /workspace/outputs/.
    NOTE: This tool requires human approval before executing."""
    output_path = Path(OUTPUT_DIR) / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if filename.endswith(".docx"):
        return _generate_docx(filename, content)
    elif filename.endswith(".xlsx"):
        return _generate_xlsx(filename, content)
    elif filename.endswith(".py"):
        output_path.write_text(content, encoding="utf-8")
        return f"✓ Written: {output_path}"
    else:
        output_path.write_text(content, encoding="utf-8")
        return f"✓ Written: {output_path}"


def _generate_docx(filename: str, markdown_content: str) -> str:
    """Convert structured markdown to a Word document."""
    from docx import Document
    from docx.shared import Inches, Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()

    # Parse the markdown-ish content
    lines = markdown_content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("# "):
            # Title
            doc.add_heading(line[2:], level=0)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=1)
        elif line.startswith("### "):
            doc.add_heading(line[4:], level=2)
        elif line.startswith("- ") or line.startswith("* "):
            doc.add_paragraph(line[2:], style="List Bullet")
        elif line.startswith("**") and line.endswith("**"):
            # Bold paragraph
            p = doc.add_paragraph()
            run = p.add_run(line.strip("*"))
            run.bold = True
        elif line.startswith("|") and "|" in line[1:]:
            # Table row
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if not hasattr(_generate_docx, "_current_table"):
                _generate_docx._current_table = None
            # Skip separator rows
            if all(c in ("-", ":", "") for c in cells):
                i += 1
                continue
            if _generate_docx._current_table is None:
                table = doc.add_table(rows=0, cols=len(cells))
                table.style = "Table Grid"
                _generate_docx._current_table = table
            _generate_docx._current_table.add_row().cells  # placeholder
            row = _generate_docx._current_table.add_row()
            for j, cell_text in enumerate(cells):
                if j < len(row.cells):
                    row.cells[j].text = cell_text
        else:
            if line:  # skip empty
                doc.add_paragraph(line)
            else:
                _generate_docx._current_table = None  # reset table on blank

        i += 1

    output_path = Path(OUTPUT_DIR) / filename
    doc.save(str(output_path))
    _generate_docx._current_table = None  # reset
    return f"✓ Word document created: {output_path}"


def _generate_xlsx(filename: str, content: str) -> str:
    """Convert structured content to Excel."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"

    # Parse rows (tab or | separated)
    for row_line in content.split("\n"):
        if "|" in row_line:
            cells = [c.strip() for c in row_line.split("|")[1:-1]]
        elif "\t" in row_line:
            cells = row_line.split("\t")
        else:
            cells = [row_line]
        if cells and any(cells):
            ws.append(cells)

    output_path = Path(OUTPUT_DIR) / filename
    wb.save(str(output_path))
    return f"✓ Excel file created: {output_path}"


# ═════════════════════════════════════════════════════════════
# TOOL: run_code — sandboxed Python execution (REQUIRES APPROVAL)
# ═════════════════════════════════════════════════════════════
@tool
def run_code(code: str) -> str:
    """Execute Python code in a restricted sandbox. Use this to verify
    calculations or test generated code before delivering it.
    NOTE: This tool requires human approval before executing."""
    import subprocess
    import tempfile

    # Write code to a temp file inside the workspace
    code_dir = Path(OUTPUT_DIR) / ".sandbox"
    code_dir.mkdir(exist_ok=True)
    script_path = code_dir / "temp_script.py"
    script_path.write_text(code, encoding="utf-8")

    # Execute in restricted Docker container (no network, read-only FS except output)
    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm",
                "--network", "none",           # no network access
                "--memory", "512m",            # memory limit
                "--cpus", "0.5",               # CPU limit
                "-v", f"{code_dir}:/sandbox:ro",  # read-only mount
                "python:3.11-slim",
                "python", "/sandbox/temp_script.py",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        output = ""
        if result.stdout:
            output += f"STDOUT:\n{result.stdout}\n"
        if result.stderr:
            output += f"STDERR:\n{result.stderr}\n"
        if result.returncode != 0:
            output += f"Exit code: {result.returncode}\n"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: Code execution timed out (30s limit)"
    except Exception as e:
        return f"ERROR: {str(e)}"


# ═════════════════════════════════════════════════════════════
# TOOL: read_file — read any file from the workspace
# ═════════════════════════════════════════════════════════════
@tool
def read_file(file_path: str) -> str:
    """Read the contents of a file in the workspace. Use this to
    examine files, configs, or previously generated outputs."""
    if not file_path.startswith("/"):
        file_path = f"{WORKSPACE_DIR}/{file_path}"
    path = Path(file_path)

    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:8000]
    except Exception as e:
        return f"ERROR reading file: {e}"


@tool
def grep_codebase(pattern: str, path: str = "", file_glob: str = "*") -> str:
    """Search for an exact pattern (function name, variable, string, symbol,
    equipment ID) across files. Returns matching lines with file:line references.
    Use for 'where is X defined/used' questions. Supports regex.
    file_glob filters by pattern e.g. '*.py' or '*.java'."""
    import subprocess
    root = f"/workspace/{path}" if path else "/workspace"
    try:
        result = subprocess.run(
            ["grep", "-rn", "--include", file_glob, "-E", pattern, root],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout[:6000]
        if not output:
            return f"No matches for '{pattern}' in {root}"
        return output if len(output) < 6000 else output[:6000] + "\n... (truncated)"
    except Exception as e:
        return f"grep error: {e}"

@tool
def list_files(path: str = "") -> str:
    """List files and directories. Use with grep_codebase to explore a project."""
    import os
    root = f"/workspace/{path}" if path else "/workspace"
    try:
        entries = sorted(os.listdir(root))
        return "\n".join(f"{'📁' if os.path.isdir(os.path.join(root, e)) else '📄'} {e}"
                         for e in entries[:100])
    except Exception as e:
        return f"Error: {e}"