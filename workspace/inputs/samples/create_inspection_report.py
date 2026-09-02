from PIL import Image, ImageDraw, ImageFont, ImageFilter
import img2pdf
import os
from pathlib import Path

BASE = Path.home() / "indicode/workspace/inputs/samples"

# ── Step 1: Render the text content to a PNG image ──

REPORT_TEXT = """EQUIPMENT INSPECTION REPORT
Report No: IR-2024-0178
Date: 15 March 2024
Equipment: Heat Exchanger HX-301, Unit 3
Inspector: R. Kumar, Senior Inspector

FINDINGS:
1. Tube bundle shows external corrosion on 12 tubes
   (positions 3, 7, 15, 22, 28, 34, 41, 48, 52, 58, 63, 70).
   Wall thickness measured at 2.1mm (spec: 3.5mm minimum).
   REPLACEMENT REQUIRED.

2. Shell-side nozzle N2 has weld seam crack extending
   15cm circumferentially. Crack depth 4mm (spec: max 2mm).
   REPAIR REQUIRED before next startup.

3. Gasket surface on channel head shows pitting corrosion,
   depth 0.8mm. MONITOR - acceptable for one more cycle.

4. Baffle plate 7 shows erosion thinning at edges.
   Thickness 60% of original. REPLACE during next turnaround.

RECOMMENDATIONS:
- Priority 1: Replace tube bundle (estimated 3 weeks downtime)
- Priority 2: Repair N2 nozzle weld (estimated 1 week)
- Priority 3: Monitor gasket surface, replace at turnaround
- Priority 4: Replace baffle plate 7 at next turnaround

APPROVAL: Requires Plant Manager and Safety Officer
sign-off before tube bundle replacement can proceed.
"""

# Create a blank white image (letter-size proportions: 8.5 x 11 inches at 150 DPI)
img = Image.new('RGB', (1275, 1650), '#fdfdfd')
draw = ImageDraw.Draw(img)

# Use a monospace font to mimic a typed report
font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
if not os.path.exists(font_path):
    # Fallback: use default PIL font (won't look as good but works)
    font = ImageFont.load_default()
    font_size = 14
else:
    font_size = 22
    font = ImageFont.truetype(font_path, font_size)

# Draw the text line by line
x_margin = 100
y = 100
line_height = 36

for line in REPORT_TEXT.split('\n'):
    draw.text((x_margin, y), line, fill='#1a1a1a', font=font)
    y += line_height

# ── Step 2: Degrade the image to simulate a real scan ──

# Slight rotation (real scans are never perfectly straight)
img = img.rotate(-0.8, fillcolor='#fdfdfd', expand=False)

# Add subtle noise (photocopiers/scanners produce speckle)
import numpy as np
arr = np.array(img)
noise = np.random.normal(0, 6, arr.shape[:2]).astype(np.int8)
arr[:,:,0] = np.clip(arr[:,:,0].astype(np.int16) + noise, 0, 255).astype(np.uint8)
arr[:,:,1] = np.clip(arr[:,:,1].astype(np.int16) + noise, 0, 255).astype(np.uint8)
arr[:,:,2] = np.clip(arr[:,:,2].astype(np.int16) + noise, 0, 255).astype(np.uint8)
img = Image.fromarray(arr)

# Slight blur (scanner optics aren't perfect)
img = img.filter(ImageFilter.GaussianBlur(radius=0.5))

# ── Step 3: Save as PNG (the actual "scanned page") ──
png_path = str(BASE / "inspection_page.png")
img.save(png_path, quality=95)
print(f"Created scan-like PNG: {png_path}")

# ── Step 4: Wrap the PNG in a PDF (image-only, no text layer) ──
pdf_path = str(BASE / "inspection_report_hx301.pdf")
with open(pdf_path, "wb") as f:
    f.write(img2pdf.convert(png_path))
print(f"Created image-only PDF: {pdf_path}")

# Verify: open the PDF and confirm there's no extractable text
import fitz  # PyMuPDF
doc = fitz.open(pdf_path)
text = doc[0].get_text()
print(f"Extractable text in PDF: '{text}'")  # Should print empty or whitespace
print("PASS" if len(text.strip()) < 5 else "FAIL — text layer exists, OCR won't be triggered")