from PIL import Image, ImageDraw, ImageFont

# Create a white canvas
img = Image.new("RGB", (1200, 800), "white")
draw = ImageDraw.Draw(img)

# Fonts
try:
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        24
    )
    small_font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        18
    )
    title_font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        30
    )
except Exception:
    font = ImageFont.load_default()
    small_font = ImageFont.load_default()
    title_font = ImageFont.load_default()

# ─────────────────────────────────────────────
# Title
# ─────────────────────────────────────────────

draw.text(
    (300, 30),
    "P&ID: Heat Exchanger Loop - Unit 3",
    fill="black",
    font=title_font
)

draw.text(
    (420, 70),
    "Drawing No: PID-U3-HX-001 Rev 2",
    fill="black",
    font=small_font
)

# ─────────────────────────────────────────────
# Heat exchanger HX-301
# ─────────────────────────────────────────────

draw.rectangle(
    [100, 280, 280, 500],
    outline="black",
    width=4
)

draw.text(
    (135, 370),
    "HX-301",
    fill="black",
    font=font
)

draw.text(
    (105, 515),
    "Heat Exchanger",
    fill="black",
    font=small_font
)

# ─────────────────────────────────────────────
# Pump P-101
# ─────────────────────────────────────────────

draw.ellipse(
    [470, 250, 570, 350],
    outline="black",
    width=4
)

draw.text(
    (495, 285),
    "P",
    fill="black",
    font=font
)

draw.text(
    (485, 365),
    "P-101",
    fill="black",
    font=font
)

# ─────────────────────────────────────────────
# Tank TK-201
# ─────────────────────────────────────────────

draw.rectangle(
    [800, 230, 1050, 520],
    outline="black",
    width=4
)

draw.text(
    (875, 350),
    "TK-201",
    fill="black",
    font=font
)

draw.text(
    (855, 535),
    "Storage Tank",
    fill="black",
    font=small_font
)

# ─────────────────────────────────────────────
# Hot-side pipe
# ─────────────────────────────────────────────

draw.line(
    [280, 330, 470, 300],
    fill="black",
    width=5
)

draw.line(
    [570, 300, 800, 300],
    fill="black",
    width=5
)

# Arrow indicating flow
draw.polygon(
    [(420, 290), (440, 300), (420, 310)],
    fill="black"
)

draw.text(
    (300, 245),
    "Hot Inlet 120 C",
    fill="black",
    font=small_font
)

draw.text(
    (610, 245),
    "Hot Outlet 80 C",
    fill="black",
    font=small_font
)

# ─────────────────────────────────────────────
# Cold-side pipe
# ─────────────────────────────────────────────

draw.line(
    [800, 450, 570, 450],
    fill="black",
    width=5
)

draw.line(
    [470, 450, 280, 450],
    fill="black",
    width=5
)

# Arrow indicating flow
draw.polygon(
    [(620, 440), (600, 450), (620, 460)],
    fill="black"
)

draw.text(
    (610, 480),
    "Cold Inlet 30 C",
    fill="black",
    font=small_font
)

draw.text(
    (300, 480),
    "Cold Outlet 45 C",
    fill="black",
    font=small_font
)

# ─────────────────────────────────────────────
# Additional equipment labels
# ─────────────────────────────────────────────

draw.text(
    (90, 150),
    "PROCESS AREA: UNIT 3",
    fill="black",
    font=small_font
)

draw.text(
    (850, 150),
    "PRESSURE: 12 bar",
    fill="black",
    font=small_font
)

# ─────────────────────────────────────────────
# Save
# ─────────────────────────────────────────────

output = "pid_hx301.png"

img.save(output)

print(f"Created: {output}")
