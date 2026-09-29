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

# Parse model markdown into structured tokens for the file generators.

import re as _re

def _parse_markdown_inline(text: str) -> list:
    """Parse inline markdown (**bold**, *italic*, `code`) into (text, style) tuples."""
    tokens = []
    pattern = r'(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)'
    parts = _re.split(pattern, text)
    for part in parts:
        if not part:
            continue
        if part.startswith('**') and part.endswith('**') and len(part) > 4:
            tokens.append((part[2:-2], 'bold'))
        elif part.startswith('*') and part.endswith('*') and len(part) > 2:
            tokens.append((part[1:-1], 'italic'))
        elif part.startswith('`') and part.endswith('`') and len(part) > 2:
            tokens.append((part[1:-1], 'code'))
        else:
            tokens.append((part, 'normal'))
    return tokens


def _parse_markdown_blocks(content: str) -> list:
    """Parse markdown content into structured blocks:
    [('heading', level, text), ('para', text), ('bullet', text),
     ('table', [[cell, ...], ...]), ('hr',)]
    """
    blocks = []
    lines = content.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Empty line — skip
        if not line:
            i += 1
            continue

        # Headings
        if line.startswith('#### '):
            blocks.append(('heading', 3, line[5:].strip()))
            i += 1
        elif line.startswith('### '):
            blocks.append(('heading', 2, line[4:].strip()))
            i += 1
        elif line.startswith('## '):
            blocks.append(('heading', 1, line[3:].strip()))
            i += 1
        elif line.startswith('# '):
            blocks.append(('heading', 0, line[2:].strip()))
            i += 1

        # Horizontal rule
        elif line in ('---', '***', '___'):
            blocks.append(('hr',))
            i += 1

        # Table row
        elif line.startswith('|') and '|' in line[1:]:
            # Collect all consecutive table rows
            table_rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                row_line = lines[i].strip()
                cells = [c.strip() for c in row_line.split('|')]
                # Remove empty first/last from the pipe boundaries
                if cells and cells[0] == '':
                    cells = cells[1:]
                if cells and cells[-1] == '':
                    cells = cells[:-1]
                # Skip separator rows (---, :---, etc.)
                if not all(c.replace('-', '').replace(':', '').replace(' ', '') == '' for c in cells):
                    # Strip inline markdown from cells
                    clean_cells = []
                    for c in cells:
                        parsed = _parse_markdown_inline(c)
                        clean_text = ''.join(t for t, s in parsed)
                        clean_cells.append(clean_text)
                    table_rows.append(clean_cells)
                i += 1
            if table_rows:
                blocks.append(('table', table_rows))

        # Bullet points
        elif line.startswith('- ') or line.startswith('* '):
            text = line[2:].strip()
            blocks.append(('bullet', text))
            i += 1

        # Numbered list
        elif _re.match(r'^\d+\.\s', line):
            match = _re.match(r'^(\d+)\.\s+(.*)', line)
            if match:
                blocks.append(('numbered', match.group(1), match.group(2)))
            i += 1

        # Bold-only line (subheading)
        elif line.startswith('**') and line.endswith('**') and len(line) > 4:
            blocks.append(('subheading', line[2:-2].strip()))
            i += 1

        # Regular paragraph
        else:
            # Merge consecutive non-empty, non-special lines into one paragraph
            para_lines = [line]
            i += 1
            while i < len(lines):
                next_line = lines[i].strip()
                if (not next_line or
                    next_line.startswith('#') or
                    next_line.startswith('|') or
                    next_line.startswith('- ') or
                    next_line.startswith('* ') or
                    next_line.startswith('**') and next_line.endswith('**') or
                    _re.match(r'^\d+\.\s', next_line)):
                    break
                para_lines.append(next_line)
                i += 1
            blocks.append(('para', ' '.join(para_lines)))

    return blocks

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

# Tool: search internal knowledge.
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

    provenance = [
        {
            "filename": item.get("filename"),
            "source_file": item.get("source"),
            "chunk_index": item.get("chunk_index"),
            "vector_score": item.get("vector_score"),
            "rerank_score": item.get("rerank_score"),
        }
        for item in result["results"]
    ]

    parts = [
        f"RAG_PROVENANCE: {json.dumps(provenance, separators=(',', ':'))}",
        f"Found {len(result['results'])} relevant passages:\n",
    ]
    for i, r in enumerate(result["results"], 1):
        parts.append(f"[Passage {i} | Source: {r['filename']}]\n{r['text']}\n")
    return "\n---\n".join(parts)


# Tool: parse documents with Docling OCR.
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


# Tool: analyze images with the vision model.
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


# Tool: generate approved deliverables.
@tool
def write_file(filename: str, content: str) -> str:
    """Write generated content to a file. Office files use the built-in
    styled renderers; .docx/.pptx/.xlsx content is structured text/markdown.
    Output goes to /workspace/outputs/.
    NOTE: This tool requires human approval before executing."""
    output_path = Path(OUTPUT_DIR) / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)

    suffix = Path(filename).suffix.lower()

    if suffix == ".docx":
        return _generate_docx(filename, content)
    elif suffix == ".xlsx":
        return _generate_xlsx(filename, content)
    elif suffix == ".pptx":
        return _generate_pptx(filename, content)
    elif suffix == ".py":
        output_path.write_text(content, encoding="utf-8")
        return f"✓ Written: {output_path}"
    else:
        output_path.write_text(content, encoding="utf-8")
        return f"✓ Written: {output_path}"


def _generate_docx(filename: str, content: str) -> str:
    from docx import Document
    from docx.shared import Pt, RGBColor, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    import datetime

    ACCENT = RGBColor(0x1F, 0x4E, 0x79)
    TEXT = RGBColor(0x33, 0x33, 0x33)
    MUTED = RGBColor(0x88, 0x88, 0x88)
    WHITE = RGBColor(0xFF, 0xFF, 0xFF)

    doc = Document()
    doc.core_properties.title = filename.rsplit(".", 1)[0].replace("_", " ").title()
    doc.core_properties.author = "Indicode Sovereign AI Workbench"

    # Page setup
    for section in doc.sections:
        section.top_margin = Inches(0.8)
        section.bottom_margin = Inches(0.8)
        section.left_margin = Inches(0.9)
        section.right_margin = Inches(0.9)

        # Footer with page number
        footer = section.footer
        fp = footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = fp.add_run("CONFIDENTIAL — Internal Use Only   |   Page ")
        run.font.size = Pt(8)
        run.font.color.rgb = MUTED
        run.italic = True

    # Base style
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    style.font.color.rgb = TEXT
    style.paragraph_format.space_after = Pt(6)
    style.paragraph_format.line_spacing = 1.15

    blocks = _parse_markdown_blocks(content)
    title_done = False

    def add_inline_text(paragraph, text):
        """Add text with inline bold/italic formatting (no raw **)."""
        for token, fmt in _parse_markdown_inline(text):
            run = paragraph.add_run(token)
            if fmt == 'bold':
                run.bold = True
            elif fmt == 'italic':
                run.italic = True
            elif fmt == 'code':
                run.font.name = "Consolas"
                run.font.size = Pt(10)

    for block in blocks:
        btype = block[0]

        if btype == 'heading':
            level, text = block[1], block[2]
            if level == 0:
                h = doc.add_heading(text, level=0)
                h.runs[0].font.color.rgb = ACCENT
                h.runs[0].font.size = Pt(22)
                title_done = True
                # Add generation date below title
                p = doc.add_paragraph()
                r = p.add_run(f"Generated by Indicode  ·  {datetime.datetime.now().strftime('%d %B %Y')}")
                r.font.size = Pt(9)
                r.font.color.rgb = MUTED
                r.italic = True
                p.paragraph_format.space_after = Pt(14)
            else:
                h = doc.add_heading(text, level=min(level, 3))
                h.runs[0].font.color.rgb = ACCENT
                h.runs[0].font.size = Pt(16 if level == 1 else 14)
                h.paragraph_format.space_before = Pt(12)
                h.paragraph_format.space_after = Pt(4)

        elif btype == 'subheading':
            p = doc.add_paragraph()
            run = p.add_run(block[1])
            run.bold = True
            run.font.color.rgb = ACCENT
            run.font.size = Pt(12)
            p.paragraph_format.space_before = Pt(8)

        elif btype == 'bullet':
            p = doc.add_paragraph(style="List Bullet")
            add_inline_text(p, block[1])
            p.paragraph_format.space_after = Pt(3)

        elif btype == 'numbered':
            p = doc.add_paragraph(style="List Number")
            add_inline_text(p, block[2])
            p.paragraph_format.space_after = Pt(3)

        elif btype == 'para':
            p = doc.add_paragraph()
            add_inline_text(p, block[1])

        elif btype == 'table':
            rows = block[1]
            if not rows:
                continue
            n_cols = max(len(r) for r in rows)
            table = doc.add_table(rows=0, cols=n_cols)
            table.style = "Table Grid"
            for ri, row_data in enumerate(rows):
                row = table.add_row()
                is_header = (ri == 0)
                for ci in range(n_cols):
                    cell = row.cells[ci]
                    cell_text = row_data[ci] if ci < len(row_data) else ""
                    cell.text = cell_text
                    for para in cell.paragraphs:
                        for run in para.runs:
                            run.font.size = Pt(10)
                            if is_header:
                                run.bold = True
                                run.font.color.rgb = WHITE
                    if is_header:
                        # Shade header row
                        from docx.oxml.ns import qn
                        from docx.oxml import OxmlElement
                        shd = OxmlElement('w:shd')
                        shd.set(qn('w:fill'), '1F4E79')
                        cell._tc.get_or_add_tcPr().append(shd)
            doc.add_paragraph()  # spacing after table

    output_path = Path(OUTPUT_DIR) / filename
    doc.save(str(output_path))
    return f"✓ Word document created: {output_path}"

def _generate_xlsx(filename: str, content: str) -> str:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    import datetime

    ACCENT = "1F4E79"
    HEADER_FILL = PatternFill(start_color=ACCENT, end_color=ACCENT, fill_type="solid")
    HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
    BODY_FONT = Font(size=10)
    ALT_FILL = PatternFill(start_color="F0F4F8", end_color="F0F4F8", fill_type="solid")
    THIN = Side(style="thin", color="CCCCCC")
    BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
    SECTION_FONT = Font(bold=True, size=13, color=ACCENT)

    blocks = _parse_markdown_blocks(content)

    # Group blocks into sections (split on headings)
    sections = []
    current_section = {"title": None, "blocks": []}

    for block in blocks:
        if block[0] in ('heading', 'subheading'):
            if current_section["blocks"]:
                sections.append(current_section)
            title = block[2] if block[0] == 'heading' else block[1]
            current_section = {"title": title, "blocks": []}
        else:
            current_section["blocks"].append(block)
    if current_section["blocks"]:
        sections.append(current_section)

    # If no sections, treat everything as one section
    if not sections:
        sections = [{"title": "Sheet1", "blocks": blocks}]

    wb = openpyxl.Workbook()

    for si, section in enumerate(sections):
        if si == 0:
            ws = wb.active
        else:
            ws = wb.create_sheet()

        # Sheet title from section heading (max 31 chars)
        sheet_title = (section["title"] or f"Sheet{si+1}")[:31].replace("/", "-")
        ws.title = sheet_title

        row_num = 0
        max_cols = 0
        section_start_row = 1

        # Add section title if it's a subheading (main title is the sheet name)
        if section["title"] and si == 0:
            cell = ws.cell(row=1, column=1, value=section["title"])
            cell.font = Font(bold=True, size=14, color=ACCENT)
            row_num = 3  # leave space after the title

        for block in section["blocks"]:
            btype = block[0]

            if btype == 'table':
                rows = block[1]
                # Header row for THIS table (not just the first table in the file)
                table_header_row = row_num + 1
                for ri, row_data in enumerate(rows):
                    row_num += 1
                    is_header = (ri == 0)  # First row of THIS table is the header
                    for ci, cell_text in enumerate(row_data):
                        col_num = ci + 1
                        cell = ws.cell(row=row_num, column=col_num, value=cell_text)
                        cell.border = BORDER
                        if is_header:
                            cell.fill = HEADER_FILL
                            cell.font = HEADER_FONT
                            cell.alignment = Alignment(
                                horizontal="center", vertical="center", wrap_text=True
                            )
                        else:
                            cell.font = BODY_FONT
                            if (row_num - table_header_row) % 2 == 0:
                                cell.fill = ALT_FILL
                            cell.alignment = Alignment(vertical="top", wrap_text=True)
                    max_cols = max(max_cols, len(row_data))
                row_num += 1  # blank row after table

            elif btype in ('para', 'bullet'):
                row_num += 1
                text = block[1]
                parsed = _parse_markdown_inline(text)
                clean = ''.join(t for t, s in parsed)
                cell = ws.cell(row=row_num, column=1, value=clean)
                cell.font = Font(size=10, italic=True, color="666666")

        # Column widths
        for col in range(1, max_cols + 1):
            col_letter = get_column_letter(col)
            max_len = 0
            for row in range(1, row_num + 1):
                val = ws.cell(row=row, column=col).value
                if val:
                    max_len = max(max_len, len(str(val)))
            ws.column_dimensions[col_letter].width = min(max_len + 4, 45)

        # Freeze header row (first data row, after any section title)
        if row_num > 2:
            ws.freeze_panes = f"A{2 if si == 0 and section['title'] else 2}"

        # Footer
        if row_num > 0:
            footer_row = row_num + 2
            fc = ws.cell(row=footer_row, column=1)
            fc.value = f"Generated by Indicode  ·  {datetime.datetime.now().strftime('%d %B %Y, %H:%M')}"
            fc.font = Font(size=8, italic=True, color="888888")

    # Workbook metadata
    wb.properties.title = filename.rsplit(".", 1)[0].replace("_", " ").title()
    wb.properties.creator = "Indicode Sovereign AI Workbench"

    output_path = Path(OUTPUT_DIR) / filename
    wb.save(str(output_path))

    sheet_names = [ws.title for ws in wb.worksheets]
    return f"✓ Excel file created: {output_path} ({len(sheet_names)} sheets: {', '.join(sheet_names)})"

def _generate_pptx(filename: str, content: str) -> str:
    from pptx import Presentation
    from pptx.util import Pt, Inches, Emu
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    import datetime

    ACCENT = RGBColor(0x1F, 0x4E, 0x79)
    ACCENT_LIGHT = RGBColor(0x2E, 0x75, 0xB6)
    MUTED = RGBColor(0x66, 0x66, 0x66)
    WHITE = RGBColor(0xFF, 0xFF, 0xFF)
    BG = RGBColor(0xF5, 0xF7, 0xFA)

    prs = Presentation()
    prs.slide_width = Inches(13.33)
    prs.slide_height = Inches(7.5)

    blocks = _parse_markdown_blocks(content)

    def add_styled_slide(title_text):
        """Create a content slide with accent bar, title, footer."""
        slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank layout

        # Top accent bar
        bar = slide.shapes.add_shape(1, Inches(0), Inches(0), prs.slide_width, Inches(0.08))
        bar.fill.solid()
        bar.fill.fore_color.rgb = ACCENT
        bar.line.fill.background()
        bar.shadow.inherit = False

        # Left accent strip
        strip = slide.shapes.add_shape(1, Inches(0), Inches(0.08), Inches(0.06), prs.slide_height - Inches(0.5))
        strip.fill.solid()
        strip.fill.fore_color.rgb = ACCENT_LIGHT
        strip.line.fill.background()
        strip.shadow.inherit = False

        # Title (underlined)
        title_box = slide.shapes.add_textbox(Inches(0.6), Inches(0.35), Inches(11.5), Inches(1.0))
        tf = title_box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = title_text
        p.font.size = Pt(32)
        p.font.bold = True
        p.font.color.rgb = ACCENT

        # Title underline
        underline = slide.shapes.add_shape(1, Inches(0.6), Inches(1.25), Inches(2.5), Inches(0.04))
        underline.fill.solid()
        underline.fill.fore_color.rgb = ACCENT_LIGHT
        underline.line.fill.background()
        underline.shadow.inherit = False

        # Footer
        footer = slide.shapes.add_textbox(Inches(0.5), prs.slide_height - Inches(0.4), prs.slide_width - Inches(1), Inches(0.3))
        ftf = footer.text_frame
        fp = ftf.paragraphs[0]
        fp.text = f"CONFIDENTIAL — Internal Use Only"
        fp.font.size = Pt(9)
        fp.font.color.rgb = MUTED
        fp.alignment = PP_ALIGN.CENTER

        return slide

    def add_title_slide(title_text, subtitle_text=None):
        """Create a full-width title slide."""
        slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank

        # Full background
        bg = slide.shapes.add_shape(1, Inches(0), Inches(0), prs.slide_width, prs.slide_height)
        bg.fill.solid()
        bg.fill.fore_color.rgb = ACCENT
        bg.line.fill.background()
        bg.shadow.inherit = False

        # Title
        title_box = slide.shapes.add_textbox(Inches(1), Inches(2.2), Inches(11.3), Inches(1.5))
        tf = title_box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = title_text
        p.font.size = Pt(44)
        p.font.bold = True
        p.font.color.rgb = WHITE
        p.alignment = PP_ALIGN.CENTER

        # Subtitle
        if subtitle_text:
            sub_box = slide.shapes.add_textbox(Inches(1.5), Inches(3.8), Inches(10.3), Inches(1.0))
            stf = sub_box.text_frame
            stf.word_wrap = True
            sp = stf.paragraphs[0]
            sp.text = subtitle_text
            sp.font.size = Pt(20)
            sp.font.color.rgb = RGBColor(0xCC, 0xDD, 0xEE)
            sp.alignment = PP_ALIGN.CENTER

        # Generation stamp
        stamp = slide.shapes.add_textbox(Inches(1), Inches(5.8), Inches(11.3), Inches(0.5))
        stf = stamp.text_frame
        sp = stf.paragraphs[0]
        sp.text = f"Generated by Indicode  ·  {datetime.datetime.now().strftime('%d %B %Y')}"
        sp.font.size = Pt(12)
        sp.font.color.rgb = RGBColor(0xAA, 0xCC, 0xEE)
        sp.alignment = PP_ALIGN.CENTER

        return slide

    def add_content_to_slide(slide, blocks_slice, start_y=1.5):
        """Add content blocks to a slide with REAL tables and larger text."""
        y = start_y
        for block in blocks_slice:
            btype = block[0]

            if btype == 'bullet':
                box = slide.shapes.add_textbox(Inches(0.8), Inches(y), Inches(11.5), Inches(0.6))
                tf = box.text_frame
                tf.word_wrap = True
                p = tf.paragraphs[0]
                text_parts = _parse_markdown_inline(block[1])
                full_text = ''.join(t for t, s in text_parts)
                p.text = f"•  {full_text}"
                p.font.size = Pt(24)  # was 20 — larger
                p.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
                p.space_after = Pt(8)
                y += 0.65  # was 0.55 — more spacing

            elif btype == 'para':
                box = slide.shapes.add_textbox(Inches(0.8), Inches(y), Inches(11.5), Inches(0.8))
                tf = box.text_frame
                tf.word_wrap = True
                p = tf.paragraphs[0]
                text_parts = _parse_markdown_inline(block[1])
                full_text = ''.join(t for t, s in text_parts)
                p.text = full_text
                p.font.size = Pt(22)  # was 18 — larger
                p.font.color.rgb = RGBColor(0x44, 0x44, 0x44)
                p.space_after = Pt(10)
                y += 0.8

            elif btype == 'subheading':
                box = slide.shapes.add_textbox(Inches(0.8), Inches(y), Inches(11.5), Inches(0.5))
                tf = box.text_frame
                p = tf.paragraphs[0]
                p.text = block[1]
                p.font.size = Pt(26)  # was 22
                p.font.bold = True
                p.font.color.rgb = ACCENT
                y += 0.7

            elif btype == 'table':
                # ── REAL PowerPoint table (not text with |) ──────
                rows = block[1]
                if not rows:
                    continue

                n_rows = len(rows)
                n_cols = max(len(r) for r in rows)

                # Calculate table dimensions
                table_left = Inches(0.8)
                table_top = Inches(y)
                table_width = Inches(11.5)
                # Estimate height based on row count
                row_height = Inches(0.45)
                table_height = row_height * n_rows

                # Create the actual table shape
                table_frame = slide.shapes.add_table(
                    n_rows, n_cols, table_left, table_top, table_width, table_height
                )
                table = table_frame.table

                # Set column widths (equal distribution)
                col_width = int(table_width / n_cols)
                for i in range(n_cols):
                    table.columns[i].width = col_width

                # Fill cells
                for ri, row_data in enumerate(rows):
                    for ci in range(n_cols):
                        cell = table.cell(ri, ci)
                        cell_text = row_data[ci] if ci < len(row_data) else ""

                        # Set cell text
                        cell.text = cell_text

                        # Style the cell
                        p = cell.text_frame.paragraphs[0]
                        if ri == 0:  # header row
                            p.font.size = Pt(16)
                            p.font.bold = True
                            p.font.color.rgb = WHITE
                            # Header background
                            cell.fill.solid()
                            cell.fill.fore_color.rgb = ACCENT
                        else:
                            p.font.size = Pt(14)
                            p.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
                            # Alternating row colors
                            if ri % 2 == 0:
                                cell.fill.solid()
                                cell.fill.fore_color.rgb = RGBColor(0xF0, 0xF4, 0xF8)
                            else:
                                cell.fill.solid()
                                cell.fill.fore_color.rgb = WHITE

                        # Cell margins
                        cell.margin_left = Inches(0.08)
                        cell.margin_right = Inches(0.08)
                        cell.margin_top = Inches(0.04)
                        cell.margin_bottom = Inches(0.04)

                y += table_height + Inches(0.3)

            elif btype == 'numbered':
                box = slide.shapes.add_textbox(Inches(0.8), Inches(y), Inches(11.5), Inches(0.6))
                tf = box.text_frame
                p = tf.paragraphs[0]
                text_parts = _parse_markdown_inline(block[2])
                full_text = ''.join(t for t, s in text_parts)
                p.text = f"{block[1]}.  {full_text}"
                p.font.size = Pt(24)
                p.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
                y += 0.65

            if y > 6.8:
                break

    # Process blocks into slides
    current_slide = None
    current_title = ""
    slide_content = []
    slide_count = 0

    # Check if first block is a title
    first_heading = None
    for block in blocks:
        if block[0] == 'heading' and block[1] == 0:
            first_heading = block[2]
            break

    # Create title slide if there's a main title
    if first_heading:
        add_title_slide(first_heading, "Equipment Inspection & Maintenance")
        slide_count += 1
        blocks = blocks[1:]  # Skip the title heading

    # Group content into slides (new slide on each heading)
    for block in blocks:
        btype = block[0]

        if btype == 'heading' and block[1] <= 1:
            # Save current slide
            if current_slide and slide_content:
                add_content_to_slide(current_slide, slide_content)
            # Start new slide
            current_title = block[2]
            current_slide = add_styled_slide(current_title)
            slide_content = []
            slide_count += 1

        elif btype == 'subheading':
            # Save current slide
            if current_slide and slide_content:
                add_content_to_slide(current_slide, slide_content)
            # New slide with subheading as title
            current_title = block[1]
            current_slide = add_styled_slide(current_title)
            slide_content = []
            slide_count += 1

        else:
            if current_slide is None:
                # No heading yet — create a generic slide
                current_slide = add_styled_slide("Content")
                slide_count += 1
            slide_content.append(block)

            # If too much content, start a new slide
            if len(slide_content) >= 5:
                add_content_to_slide(current_slide, slide_content)
                current_slide = add_styled_slide(current_title + " (cont.)")
                slide_content = []
                slide_count += 1

    # Final slide
    if current_slide and slide_content:
        add_content_to_slide(current_slide, slide_content)

    # If no slides were created, make one
    if slide_count == 0:
        add_title_slide("Untitled", "Generated by Indicode")

    output_path = Path(OUTPUT_DIR) / filename
    prs.save(str(output_path))
    return f"✓ PowerPoint created: {output_path} ({slide_count} slides)"

# Tool: run approved Python code in the sandbox.
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


# Tool: read a workspace file.
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
    """Search the ENTIRE workspace (inputs + outputs + all subdirectories) for
    an exact pattern or regex. Returns file:line references.
    Use path="" to search everything, or path="outputs" to narrow to outputs only.
    ALWAYS try the full workspace first before narrowing."""
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