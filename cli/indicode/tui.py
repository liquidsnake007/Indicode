"""
Indicode TUI
"""
import json
import math
import os
import random
import threading
from datetime import datetime

import httpx
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Center, VerticalScroll, Horizontal, Vertical
from textual.widgets import (
    Input, Static, Button, Label, Switch, TextArea,
)
from textual.widget import Widget
from textual.screen import ModalScreen
from textual import work
from textual.binding import Binding
from textual.events import Click

from .api import AgentClient

SESSIONS_FILE = os.path.expanduser("~/.indicode_sessions.json")

# TUI theme and shared display constants.

LOGO = """
██╗███╗   ██╗██████╗ ██╗ ██████╗ ██████╗ ██████╗ ███████╗
██║████╗  ██║██╔══██╗██║██╔════╝██╔═══██╗██╔══██╗██╔════╝
██║██╔██╗ ██║██║  ██║██║██║     ██║   ██║██║  ██║█████╗  
██║██║╚██╗██║██║  ██║██║██║     ██║   ██║██║  ██║██╔══╝  
██║██║ ╚████║██████╔╝██║╚██████╗╚██████╔╝██████╔╝███████╗
╚═╝╚═╝  ╚═══╝╚═════╝ ╚═╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
"""

SUBTITLE = "Sovereign On-Premise AI Workbench"


def load_sessions() -> list:
    try:
        with open(SESSIONS_FILE, "r") as f:
            sessions = json.load(f)
        seen = {}
        for s in sessions:
            tid = s.get("thread_id", "")
            if tid:
                seen[tid] = s
        return list(seen.values())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_sessions(sessions: list):
    try:
        seen = {}
        for s in sessions:
            tid = s.get("thread_id", "")
            if tid:
                seen[tid] = s
        with open(SESSIONS_FILE, "w") as f:
            json.dump(list(seen.values())[-50:], f, indent=2)
    except Exception:
        pass


# Ambient background widget: rotating braille globe with India highlighted.

class RotatingGlobe(Widget):
    """Rotating wireframe globe + twinkling stars + gentle snowfall."""

    _DOT_BITS = {
        (0, 0): 0x01, (1, 0): 0x02, (2, 0): 0x04, (3, 0): 0x40,
        (0, 1): 0x08, (1, 1): 0x10, (2, 1): 0x20, (3, 1): 0x80,
    }

    FPS = 15
    SPEED_DEG = 32.0
    TILT_DEG  = 18.0

    INDIA_POLYGON = [
        (74.0, 35.5), (76.5, 34.5), (78.5, 33.5), (79.5, 31.5),
        (80.5, 30.0), (82.5, 28.5), (85.0, 27.5), (88.0, 27.0),
        (89.5, 26.5), (92.0, 28.0), (94.5, 28.5), (96.5, 27.5),
        (96.0, 26.0), (94.5, 23.5), (93.0, 22.5), (92.5, 21.5),
        (91.5, 22.8), (90.0, 22.0), (88.0, 21.5), (86.5, 20.0),
        (84.5, 18.5), (82.0, 16.5), (80.5, 15.5), (80.2, 13.5),
        (79.5, 10.5), (77.8, 8.3),
        (76.5, 9.0),  (75.0, 12.0), (74.0, 14.5), (73.0, 17.0),
        (72.5, 19.5), (72.0, 21.0), (70.0, 21.5), (69.0, 22.5),
        (68.5, 23.5), (68.0, 24.5), (70.0, 25.5), (70.5, 27.0),
        (70.0, 29.0), (71.5, 30.5), (72.5, 32.0), (73.5, 33.5),
        (74.0, 34.5),
    ]
    INDIA_BBOX = (8.0, 68.0, 36.0, 97.0)

    PRIMARY_COLOR = "#5a7a9a"
    ACCENT_COLOR  = "#ffa657"

    # --- particles ------------------------------------------------------
    PARTICLE_BRIGHT_COLOR = "#eef5ff"    # crisp white
    PARTICLE_DIM_COLOR    = "#7e94b4"    # faded blue-grey

    # Ratio of fixed (non-falling) twinkling stars.
    FIXED_RATIO = 0.28

    # Falling snow
    SNOW_FALL_MIN     = 4.0
    SNOW_FALL_MAX     = 11.0
    SNOW_DRIFT_AMP_MIN  = 2.0
    SNOW_DRIFT_AMP_MAX  = 6.0
    SNOW_DRIFT_FREQ_MIN = 0.4
    SNOW_DRIFT_FREQ_MAX = 1.2

    # Twinkle rates (cycles / second)
    TWINKLE_FIXED_MIN    = 0.25   # stars: slow, obvious pulse
    TWINKLE_FIXED_MAX    = 0.90
    TWINKLE_FALLING_MIN  = 0.08   # snow: barely perceptible shimmer
    TWINKLE_FALLING_MAX  = 0.30

    # Density
    DENSITY_DIV = 260
    MAX_PARTICLES = 400

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._t = 0.0
        self._timer = None
        self._ct = math.cos(math.radians(self.TILT_DEG))
        self._st = math.sin(math.radians(self.TILT_DEG))
        self._primary_style = Style(color=self.PRIMARY_COLOR)
        self._accent_style  = Style(color=self.ACCENT_COLOR)
        self._bright_style  = Style(color=self.PARTICLE_BRIGHT_COLOR)
        self._dim_style     = Style(color=self.PARTICLE_DIM_COLOR)
        self._particles = []

    def on_mount(self) -> None:
        if os.environ.get("INDICODE_NO_ANIM") == "1":
            return
        self._timer = self.set_interval(1.0 / self.FPS, self._tick)

    def on_unmount(self) -> None:
        if self._timer:
            self._timer.stop()

    def pause(self) -> None:
        if self._timer:
            self._timer.pause()

    def resume(self) -> None:
        if self._timer:
            self._timer.resume()

    def _tick(self) -> None:
        dt = 1.0 / self.FPS
        self._t += dt
        self._ensure_particles()
        self._update_particles(dt)
        self.refresh()

    def _globe_geometry(self):
        w, h = self.size.width, self.size.height
        px_w, px_h = w * 2, h * 4
        cx, cy = px_w / 2.0, px_h / 2.0
        scale = min(px_w, px_h) * 0.32
        return px_w, px_h, cx, cy, scale

    # --- particles ------------------------------------------------------

    def _ensure_particles(self) -> None:
        px_w, px_h, _, _, _ = self._globe_geometry()
        if px_w <= 0 or px_h <= 0:
            return
        target = min(self.MAX_PARTICLES,
                     max(40, (px_w * px_h) // self.DENSITY_DIV))
        while len(self._particles) < target:
            self._particles.append(self._new_particle(spawn_anywhere=True))

    def _new_particle(self, spawn_anywhere: bool = False) -> dict:
        px_w, px_h, _, _, _ = self._globe_geometry()
        fixed = random.random() < self.FIXED_RATIO

        if fixed:
            y = random.uniform(0.0, float(px_h))
            vy = 0.0
            amp = 0.0
            drift_freq = 0.0
            twinkle_lo, twinkle_hi = self.TWINKLE_FIXED_MIN, self.TWINKLE_FIXED_MAX
        else:
            if spawn_anywhere:
                y = random.uniform(-px_h, px_h)
            else:
                y = random.uniform(-20.0, 0.0)
            vy = random.uniform(self.SNOW_FALL_MIN, self.SNOW_FALL_MAX)
            amp = random.uniform(self.SNOW_DRIFT_AMP_MIN, self.SNOW_DRIFT_AMP_MAX)
            drift_freq = random.uniform(self.SNOW_DRIFT_FREQ_MIN,
                                        self.SNOW_DRIFT_FREQ_MAX)
            twinkle_lo, twinkle_hi = self.TWINKLE_FALLING_MIN, self.TWINKLE_FALLING_MAX

        return {
            "x": random.uniform(0.0, float(px_w)),
            "y": y,
            "vy": vy,
            "fixed": fixed,
            "amp": amp,
            "freq": drift_freq,
            "phase": random.uniform(0.0, math.tau),
            "twinkle_freq": random.uniform(twinkle_lo, twinkle_hi),
            "twinkle_phase": random.uniform(0.0, math.tau),
        }

    def _update_particles(self, dt: float) -> None:
        px_w, px_h, _, _, _ = self._globe_geometry()
        for s in self._particles:
            s["twinkle_phase"] += s["twinkle_freq"] * math.tau * dt
            if not s["fixed"]:
                s["y"] += s["vy"] * dt
                s["phase"] += s["freq"] * dt
                if s["y"] > px_h + 2:
                    s["y"] = random.uniform(-20.0, 0.0)
                    s["x"] = random.uniform(0.0, float(px_w))
                    s["vy"] = random.uniform(self.SNOW_FALL_MIN, self.SNOW_FALL_MAX)

    # --- India tests ----------------------------------------------------

    @classmethod
    def _in_india(cls, lat: float, lon: float) -> bool:
        lat_min, lon_min, lat_max, lon_max = cls.INDIA_BBOX
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            return False
        inside = False
        poly = cls.INDIA_POLYGON
        n = len(poly)
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > lat) != (yj > lat)) and \
               (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    # --- render ---------------------------------------------------------

    def render(self) -> Text:
        w, h = self.size.width, self.size.height
        if w < 20 or h < 6:
            return Text("")

        px_w, px_h, cx, cy, scale = self._globe_geometry()
        rot = math.radians(self._t * self.SPEED_DEG)
        ct, st = self._ct, self._st

        # pixel codes: 0=wireframe, 1=India, 2=dim particle, 3=bright particle
        pixels: dict = {}

        def put(sx: int, sy: int, code: int) -> None:
            if 0 <= sx < px_w and 0 <= sy < px_h:
                key = (sx, sy)
                if code > pixels.get(key, -1):
                    pixels[key] = code

        def project(lat_deg: float, lon_deg: float):
            lat = math.radians(lat_deg)
            lon = math.radians(lon_deg) + rot
            cl, sl = math.cos(lat), math.sin(lat)
            so, co = math.sin(lon), math.cos(lon)
            x, y, z = cl * so, sl, cl * co
            y2 = y * ct - z * st
            z2 = y * st + z * ct
            if z2 <= 0.0:
                return None
            return int(cx + x * scale), int(cy - y2 * scale)

        def plot(lat: float, lon: float, code: int) -> None:
            pt = project(lat, lon)
            if pt is not None:
                put(pt[0], pt[1], code)

        # Latitude rings
        lon = 0.0
        while lon < 360.0:
            for lat in (-60, -40, -20, 0, 20, 40, 60):
                plot(lat, lon, 1 if self._in_india(lat, lon) else 0)
            lon += 2.0

        # Longitude meridians
        lat = -90.0
        while lat <= 90.0:
            for lon_v in range(0, 360, 30):
                plot(lat, lon_v, 1 if self._in_india(lat, lon_v) else 0)
            lat += 2.0

        # India fill
        lat = self.INDIA_BBOX[0]
        while lat <= self.INDIA_BBOX[2]:
            lon = self.INDIA_BBOX[1]
            while lon <= self.INDIA_BBOX[3]:
                if self._in_india(lat, lon):
                    plot(lat, lon, 1)
                lon += 0.6
            lat += 0.6

        # Stars + snow — each with its own twinkle.
        for s in self._particles:
            tw = 0.5 + 0.5 * math.sin(s["twinkle_phase"])
            if s["fixed"]:
                # Stars: full twinkle, including brief "off" flashes.
                if tw < 0.30:
                    continue
                code = 3 if tw > 0.62 else 2
                x = int(round(s["x"]))
                y = int(round(s["y"]))
            else:
                # Snow: gentle bright ↔ dim, always visible.
                code = 3 if tw > 0.55 else 2
                x = int(round(s["x"] + math.sin(s["phase"]) * s["amp"]))
                y = int(round(s["y"]))
            put(x, y, code)

        # Assemble braille cells
        out = Text()
        for cell_y in range(h):
            if cell_y:
                out.append("\n")
            base_y = cell_y * 4
            for cell_x in range(w):
                bits = 0
                top_code = -1
                base_x = cell_x * 2
                for (dr, dc), bit in self._DOT_BITS.items():
                    v = pixels.get((base_x + dc, base_y + dr), -1)
                    if v >= 0:
                        bits |= bit
                        if v > top_code:
                            top_code = v
                if bits == 0:
                    out.append(" ")
                else:
                    if top_code == 3:
                        style = self._bright_style
                    elif top_code == 2:
                        style = self._dim_style
                    elif top_code == 1:
                        style = self._accent_style
                    else:
                        style = self._primary_style
                    out.append(chr(0x2800 + bits), style=style)
        return out

# Modal screens.

class ApprovalModal(ModalScreen[bool]):
    CSS = """
    ApprovalModal { align: center middle; }
    #approval-box {
        width: 64; height: auto; max-height: 20;
        border: solid #007ACC; padding: 1 2; background: #252526;
    }
    #approval-buttons { height: auto; align-horizontal: center; padding-top: 1; }
    #approval-buttons Button { margin: 0 2; background: #007ACC; border: none; }
    """

    def __init__(self, pending: dict):
        super().__init__()
        self.pending = pending

    def compose(self) -> ComposeResult:
        calls = self.pending.get("pending_calls", [])
        lines = ["[bold #DCDCAA]⚠  APPROVAL REQUIRED[/]\n"]
        for i, c in enumerate(calls, 1):
            lines.append(f"[bold #D4D4D4]{i}. {c.get('tool')}[/]")
            preview = c.get('args_preview', '')[:200]
            lines.append(f"[#858585]{preview}...[/]\n")
        lines.append("\nApprove this action?  [bold](y)es / (n)o[/]")
        with Vertical(id="approval-box"):
            yield Static("\n".join(lines))
            with Horizontal(id="approval-buttons"):
                yield Button("Approve [y]", variant="success", id="yes")
                yield Button("Reject [n]", variant="error", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def on_key(self, event) -> None:
        if event.key in ("y", "enter"):
            self.dismiss(True)
        elif event.key in ("n", "escape"):
            self.dismiss(False)


class HelpModal(ModalScreen):
    CSS = """
    HelpModal { align: center middle; }
    #help-box {
        width: 62; height: auto; max-height: 80%;
        border: solid #007ACC; padding: 1 2; background: #252526;
    }
    """

    def compose(self) -> ComposeResult:
        help_text = """
[#007ACC bold]Keybindings[/]
  ctrl+q      Quit
  ctrl+n      New session
  ctrl+t      Toggle thinking display
  ctrl+h      Help
  escape      Close any modal

[#007ACC bold]Commands (type / in the input)[/]
  /help       Show this help
  /new        Start a new conversation
  /history    Show session history (click to resume)
  /prompt     Send a prompt from a file
  /rag        Index a file into the knowledge base
  /thinking   Toggle reasoning visibility
  /outputs    List generated files
  /thread     Show current thread ID
  /status     Show connection status
  /model      Show model routing info
  /clear      Clear the chat view
  /quit       Quit Indicode

[#858585]Press Escape to close[/]
"""
        with Vertical(id="help-box"):
            yield Static(help_text)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss()


class HistoryModal(ModalScreen):
    CSS = """
    HistoryModal { align: center middle; }
    #history-box {
        width: 72; height: auto; max-height: 80%;
        border: solid #007ACC; padding: 1 2; background: #252526;
    }
    #history-box Button {
        width: 100%; height: auto; margin: 0 0 1 0;
        content-align: left middle; background: #252526; border: none;
    }
    """

    def __init__(self, sessions: list):
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(id="history-box"):
            yield Static("[#007ACC bold]Session History[/] [#858585](click to resume)[/]\n")
            if not self.sessions:
                yield Static("[#858585]No saved sessions yet.[/]")
            else:
                recent = list(reversed(self.sessions[-15:]))
                for idx, s in enumerate(recent):
                    time_str = s.get("timestamp", "?")
                    msg_count = s.get("messages", 0)
                    preview = s.get("preview", "")[:40]
                    yield Button(
                        f"  {time_str}  ({msg_count} msgs)  {preview}...",
                        id=f"session-{idx}",
                    )
            yield Static("\n[#858585]Esc to close[/]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id.startswith("session-"):
            try:
                idx = int(btn_id[8:])
                recent = list(reversed(self.sessions[-15:]))
                if 0 <= idx < len(recent):
                    self.dismiss(recent[idx].get("thread_id"))
                    return
            except ValueError:
                pass
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


# Chat screen shown after the homepage submission.

class ChatScreen(ModalScreen):
    """Chat interface. Pushed on top of the homepage App screen."""

    CSS = """
    ChatScreen { layout: vertical; background: #111111; }

    #status-bar {
        dock: top; height: 3; padding: 0 2;
        background: #181818;
        border-bottom: solid #303030;
    }
    #url-label { width: 1fr; color: #777777; content-align: left middle; }
    #context-label { width: auto; color: #999999; content-align: center middle; margin: 0 2; }
    #model-indicator { width: auto; color: #62c8ff; content-align: center middle; margin: 0 2; }
    #think-label { width: auto; color: #999999; content-align: center middle; }
    Switch { width: auto; height: 1; margin: 0 0 0 1; }

    #chat { padding: 1 3; background: #111111; }

    #input-area {
        dock: bottom; height: 12; max-height: 12;
        background: #202020; border-top: solid #383838;
        padding: 1 2 0 2;
    }
    #command-hints {
        display: none;
        position: absolute;
        dock: bottom;
        offset: 0 -12;
        width: 1fr;
        height: 9;
        max-height: 9;
        overflow-y: auto;
        padding: 1 2;
        background: #252525;
        border: solid #454545;
    }
    TextArea { height: 9; border: solid #454545; background: #292929; padding: 0 1; max-height: 9; }
    TextArea:focus { border: solid #62c8ff; }
    #input-hint { color: #777777; width: 100%; padding: 0 1; height: 1; }

    .final {
        margin: 1 0; border: solid #383838;
        border-left: thick #75d69a; padding: 1 2; background: #202020;
    }
    .tool-line { margin: 0 0; padding: 0 2; color: #62c8ff; }
    .user-line { margin: 1 0; color: #D4D4D4; text-style: bold; }
    .status-line { color: #999999; margin: 0 0 1 0; }
    .error-line { color: #ff7777; margin: 1 0; }
    .reasoning-block { margin: 0 1; padding: 0 1; border: round #383838; color: #999999; }
    """

    _COMMANDS = [
        ("/help",     "Show help and keybindings"),
        ("/new",      "Start a new conversation"),
        ("/history",  "Show session history (click to resume)"),
        ("/prompt",   "Send a prompt from a file"),
        ("/rag",      "Index a file into the knowledge base"),
        ("/thinking", "Toggle reasoning visibility"),
        ("/outputs",  "List generated files"),
        ("/thread",   "Show current thread ID"),
        ("/status",   "Show connection status"),
        ("/model",    "Show model routing info"),
        ("/clear",    "Clear the chat view"),
        ("/quit",     "Quit Indicode"),
    ]

    def __init__(self, client: AgentClient, sessions: list, **kwargs):
        super().__init__(**kwargs)
        self.client = client
        self.show_thinking = True
        self._current_reasoning = None
        self._reasoning_text = ""
        self._reasoning_expanded = True
        self._task_type = "general"
        self._sessions = sessions
        self._current_session = None
        self._context_tokens = 0
        self._context_chars = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="status-bar"):
            yield Label(self.client.base_url, id="url-label")
            yield Label("ctx: 0/32K", id="context-label")
            yield Label("● general", id="model-indicator")
            yield Label("think:", id="think-label")
            yield Switch(value=True, id="think-toggle")

        yield VerticalScroll(id="chat")
        yield VerticalScroll(Static("", id="command-hint-content"), id="command-hints")

        with Vertical(id="input-area"):
            yield TextArea(
                "",
                id="input",
                placeholder="Message Indicode…  (/ commands, ctrl+enter to send)",
                show_line_numbers=False,
                soft_wrap=True,
            )
            yield Label("ctrl+enter to send  ·  esc to clear", id="input-hint")

    def on_mount(self) -> None:
        self._post_safe("[#858585]── Indicode ready ──[/]", css_class="status-line")

    # Run a UI callback on the app thread.

    def _ui(self, func) -> None:
        app = self.app
        if app._thread_id == threading.get_ident():
            func()
        else:
            app.call_from_thread(func)

    def _post_safe(self, text: str, css_class: str = None) -> None:
        def _mount():
            chat = self.query_one("#chat", VerticalScroll)
            w = Static(text, classes=css_class) if css_class else Static(text)
            chat.mount(w)
            chat.scroll_end(animate=False)
        self._ui(_mount)

    # Maintain an approximate prompt-context counter.

    def _update_context(self, text: str, reset: bool = False) -> None:
        """Update the approximate prompt-context size shown in the header."""
        if reset:
            self._context_chars = 0
        self._context_chars += len(str(text or ""))
        self._context_tokens = max(1, (self._context_chars + 3) // 4)
        max_context = 32000

        def _update():
            try:
                label = self.query_one("#context-label", Label)
                pct = min(100, (self._context_tokens / max_context) * 100)
                used = (
                    f"{self._context_tokens / 1000:.1f}K"
                    if self._context_tokens >= 1000
                    else str(self._context_tokens)
                )
                label.update(f"ctx: {used}/{max_context // 1000}K ({pct:.0f}%)")
            except Exception:
                pass
        self._ui(_update)

    # Handle chat screen events.

    def on_switch_changed(self, event: Switch.Changed) -> None:
        self.show_thinking = event.value

    def on_click(self, event: Click) -> None:
        if event.widget.has_class("reasoning-block"):
            self._current_reasoning = event.widget
            self._reasoning_text = getattr(event.widget, "reasoning_text", "")
            self._reasoning_expanded = not getattr(
                event.widget, "reasoning_expanded", False
            )
            event.widget.reasoning_expanded = self._reasoning_expanded
            self._refresh_reasoning_display(event.widget)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text
        hints = self.query_one("#command-hints", VerticalScroll)
        if not text or not text.startswith("/") or "\n" in text or len(text) > 25:
            self._set_command_hints("")
            hints.display = False
            return
        query = text.lower().strip()
        matches = [(cmd, desc) for cmd, desc in self._COMMANDS if cmd.startswith(query)]
        if not matches:
            self._set_command_hints("")
            hints.display = False
            return
        lines = ["[#62c8ff bold]COMMANDS[/]"]
        for cmd, desc in matches[:20]:
            lines.append(f"  [#d4d4d4 bold]{cmd:<14}[/] [#777777]{desc}[/]")
        self._set_command_hints("\n".join(lines))
        hints.display = True

    def _set_command_hints(self, text: str) -> None:
        hints = self.query_one("#command-hints", VerticalScroll)
        content = hints.query_one("#command-hint-content", Static)
        content.update(text)
        hints.scroll_home(animate=False)

    def on_key(self, event) -> None:
        if event.key in ("ctrl+j", "ctrl+enter", "ctrl+m"):
            ta = self.query_one("#input", TextArea)
            text = ta.text.strip()
            if not text:
                return
            ta.text = ""
            hints = self.query_one("#command-hints", VerticalScroll)
            self._set_command_hints("")
            hints.display = False

            if text.startswith("/"):
                self._handle_command(text)
            else:
                self._post_safe(f"[#D4D4D4 bold]you ›[/] {text}", css_class="user-line")
                self._update_context(text)
                self._run_agent(text)
            event.prevent_default()
            event.stop()

    # Handle slash commands.

    def _handle_command(self, text: str) -> None:
        cmd = text.lower().strip("/")
        if not cmd.strip():
            return
        parts = cmd.split(maxsplit=1)
        command = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        handlers = {
            "help": self.action_help, "h": self.action_help,
            "new": self.action_new_session, "n": self.action_new_session,
            "history": self.action_show_history,
            "thinking": self.action_toggle_thinking, "t": self.action_toggle_thinking,
            "outputs": self.action_list_outputs,
            "thread": self.action_show_thread,
            "status": self.action_show_status,
            "model": self.action_show_model,
            "clear": self.action_clear_chat,
            "quit": self.app.action_quit, "q": self.app.action_quit,
        }

        if command in ("prompt", "p") and args:
            filepath = os.path.expanduser(args.strip())
            if os.path.exists(filepath):
                with open(filepath, "r") as f:
                    prompt_text = f.read().strip()
                if prompt_text:
                    self._post_safe(
                        f"[#D4D4D4 bold]you ›[/] (from {os.path.basename(filepath)})",
                        css_class="user-line",
                    )
                    self._update_context(prompt_text)
                    self._run_agent(prompt_text)
            else:
                self._post_safe(f"[#F44747]File not found: {filepath}[/]",
                               css_class="error-line")
            return

        if command in ("rag", "ingest") and args:
            filepath = os.path.expanduser(args.strip().strip('"'))
            if not os.path.isfile(filepath):
                self._post_safe(
                    f"[#F44747]File not found: {filepath}[/]",
                    css_class="error-line",
                )
            else:
                self._post_safe(
                    f"[#62c8ff]Indexing {os.path.basename(filepath)}...[/]",
                    css_class="status-line",
                )
                self._ingest_rag(filepath)
            return

        if command in ("rag", "ingest"):
            self._post_safe(
                "[#F44747]Usage: /rag <path-to-file>[/]",
                css_class="error-line",
            )
            return

        if command in ("resume", "r") and args:
            self.client.thread_id = args.strip()
            self._post_safe(f"[#89D185]✓ Resumed: {args.strip()[:8]}...[/]",
                           css_class="status-line")
            return

        handler = handlers.get(command)
        if handler:
            handler()
        else:
            self._post_safe(f"[#F44747]Unknown: /{command}[/]  [#858585]Try /help[/]",
                           css_class="error-line")

    # Stream agent events in a worker thread.

    @work(thread=True)
    def _run_agent(self, message: str) -> None:
        self._reasoning_text = ""
        icons = {
            "search_knowledge": "🔍", "parse_document": "📄",
            "analyze_image": "👁", "write_file": "📝",
            "run_code": "⚡", "grep_codebase": "🔎",
            "list_files": "📁", "read_file": "📖",
        }

        try:
            for ev in self.client.stream_chat(message):
                t = ev.get("type")

                if t == "thread":
                    if self._current_session is None:
                        self._current_session = {
                            "thread_id": ev.get("thread_id"),
                            "timestamp": datetime.now().strftime("%m/%d %H:%M"),
                            "messages": 1,
                            "preview": message[:50],
                        }

                elif t == "classified":
                    self._task_type = ev.get("task_type", "general")
                    self._update_indicator(self._task_type)
                    self._post_safe(
                        f"[#007ACC bold]┌─ {self._task_type}[/]",
                        css_class="status-line",
                    )

                elif t == "reasoning":
                    content = ev.get("content", "")
                    self._reasoning_text += content
                    if self._current_reasoning is None:
                        self._create_reasoning()
                    if self._current_reasoning is not None:
                        self._current_reasoning.reasoning_text = self._reasoning_text
                    self._refresh_reasoning_display()

                elif t == "reasoning_end":
                    if self._current_reasoning:
                        self._finalize_reasoning()

                elif t == "tool_call":
                    self._current_reasoning = None
                    tool = ev.get("tool", "?")
                    icon = icons.get(tool, "🔧")
                    self._post_safe(
                        f"[#569CD6 bold]│ {icon} {tool}[/]",
                        css_class="tool-line",
                    )

                elif t == "tool_result":
                    preview = ev.get("content_preview", "")[:100]
                    self._update_context(preview)
                    self._post_safe(
                        f"[#858585]│   ↳ {preview}…[/]",
                        css_class="tool-line",
                    )

                elif t == "rag_provenance":
                    for item in ev.get("items", []):
                        filename = item.get("filename", "unknown")
                        source_file = item.get("source_file", "unknown")
                        chunk = item.get("chunk_index", "?")
                        score = item.get("rerank_score")
                        score_text = (
                            f" | score {score:.3f}"
                            if isinstance(score, (int, float))
                            else ""
                        )
                        self._post_safe(
                            f"[#c586c0]│   ↳ retrieved: {filename}"
                            f" (chunk {chunk}){score_text}\n"
                            f"[#858585]│      {source_file}[/]",
                            css_class="tool-line",
                        )

                elif t == "approval_required":
                    self._current_reasoning = None
                    decision = self.app.call_from_thread(
                        self._ask_approval, ev.get("pending", {})
                    )
                    self._post_safe(
                        "[#89D185]✓ approved — executing…[/]" if decision
                        else "[#F44747]✗ rejected[/]",
                        css_class="status-line",
                    )
                    result = self.client.approve(
                        "approve" if decision else "reject"
                    )
                    while isinstance(result, dict) and result.get("pending_approval"):
                        pending = result["pending_approval"]
                        self._post_safe(
                            "[#858585]├─ next action requires approval…[/]",
                            css_class="status-line",
                        )
                        decision = self.app.call_from_thread(
                            self._ask_approval, pending
                        )
                        self._post_safe(
                            "[#89D185]✓ approved — executing…[/]" if decision
                            else "[#F44747]✗ rejected[/]",
                            css_class="status-line",
                        )
                        result = self.client.approve(
                            "approve" if decision else "reject"
                        )

                    if isinstance(result, dict) and result.get("response"):
                        self._post_safe(
                            f"[#89D185 bold]└─[/]\n{result['response']}",
                            css_class="final",
                        )
                    else:
                        self._post_safe(
                            "[#89D185 bold]└─[/]\nTask completed.",
                            css_class="final",
                        )

                elif t == "final":
                    self._current_reasoning = None
                    content = ev.get("content", "")
                    self._update_context(content)
                    self._post_safe(
                        f"[#89D185 bold]└─[/]\n{content}",
                        css_class="final",
                    )

                elif t == "error":
                    self._post_safe(
                        f"[#F44747]✗ {ev.get('message', '')}[/]",
                        css_class="error-line",
                    )

                elif t == "done":
                    if self._current_session:
                        tid = self._current_session.get("thread_id")
                        if not any(s.get("thread_id") == tid for s in self._sessions):
                            self._sessions.append(self._current_session)
                            save_sessions(self._sessions)
                        self._current_session = None

        except Exception as e:
            self._post_safe(
                f"[#F44747]✗ connection error: {e}[/]",
                css_class="error-line",
            )

        self._ui(self.scroll_chat)

    @work(thread=True)
    def _ingest_rag(self, filepath: str) -> None:
        try:
            result = self.client.ingest_file(filepath)
            self._post_safe(
                f"[#75d69a]✓ Indexed {result.get('filename', os.path.basename(filepath))}[/]",
                css_class="status-line",
            )
        except Exception as error:
            self._post_safe(
                f"[#ff7777]✗ RAG ingestion failed: {error}[/]",
                css_class="error-line",
            )

    # Render streamed reasoning text.

    def _create_reasoning(self) -> None:
        def _create():
            chat = self.query_one("#chat", VerticalScroll)
            self._reasoning_expanded = True
            self._current_reasoning = Static(
                "[#858585 italic]💭 Thinking… (click to collapse)[/]",
                classes="reasoning-block",
            )
            self._current_reasoning.reasoning_text = self._reasoning_text
            self._current_reasoning.reasoning_expanded = True
            chat.mount(self._current_reasoning)
            self._refresh_reasoning_display(self._current_reasoning)
            chat.scroll_end(animate=False)
        self._ui(_create)

    def _toggle_reasoning(self) -> None:
        self._reasoning_expanded = not self._reasoning_expanded
        self._refresh_reasoning_display()

    def _refresh_reasoning_display(self, reasoning_widget=None) -> None:
        reasoning_widget = reasoning_widget or self._current_reasoning

        def _refresh():
            if reasoning_widget is None:
                return
            expanded = getattr(reasoning_widget, "reasoning_expanded", False)
            text = getattr(reasoning_widget, "reasoning_text", "")
            if expanded:
                if len(text) > 1500:
                    text = text[:1500] + "\n… (truncated)"
                reasoning_widget.update(
                    f"[#858585 italic]💭 Thinking (click to collapse)\n{text}[/]"
                )
            else:
                reasoning_widget.update(
                    f"[#858585 italic]💭 Thinking ({len(text)} chars — click to expand)[/]"
                )
        self._ui(_refresh)

    def _finalize_reasoning(self) -> None:
        def _finalize():
            if self._current_reasoning is not None and not self._reasoning_expanded:
                self._current_reasoning.reasoning_text = self._reasoning_text
                self._current_reasoning.reasoning_expanded = False
                self._current_reasoning.update(
                    f"[#858585 italic]💭 Thinking ({len(self._reasoning_text)} chars — click to expand)[/]"
                )
        self._ui(_finalize)

    def _update_indicator(self, task_type: str) -> None:
        def _update():
            try:
                indicator = self.query_one("#model-indicator", Label)
                models = {
                    "coding": "● qwen3:8b /think",
                    "document": "● qwen3:8b",
                    "vision": "● qwen3-vl:4b",
                    "search": "● qwen3:8b",
                    "general": "● qwen3:8b",
                }
                indicator.update(models.get(task_type, "● qwen3:8b"))
            except Exception:
                pass
        self._ui(_update)

    # Handle chat actions.

    def action_help(self) -> None:
        self.app.push_screen(HelpModal())

    def action_new_session(self) -> None:
        self.client.thread_id = None
        self._current_session = None
        self._update_context("", reset=True)
        self._post_safe("[#858585]── new session ──[/]", css_class="status-line")

    def action_toggle_thinking(self) -> None:
        switch = self.query_one("#think-toggle", Switch)
        switch.value = not switch.value

    def action_show_history(self) -> None:
        def _on_result(thread_id):
            if thread_id:
                self.client.thread_id = thread_id
                self._post_safe(
                    "[#89D185]✓ Session loaded — conversation below:[/]",
                    css_class="status-line",
                )
                self._load_conversation(thread_id)
        self.app.push_screen(HistoryModal(self._sessions), callback=_on_result)

    def _load_conversation(self, thread_id: str) -> None:
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(f"{self.client.base_url}/messages/{thread_id}")
                if resp.status_code != 200:
                    self._post_safe(
                        "[#858585](Could not load conversation)[/]",
                        css_class="status-line",
                    )
                    return
                data = resp.json()
        except Exception:
            return

        def _clear():
            chat = self.query_one("#chat", VerticalScroll)
            chat.remove_children()
        self._ui(_clear)

        history_text = []
        for msg in data.get("messages", []):
            history_text.append(msg.get("content", ""))
            history_text.append(msg.get("args_preview", ""))
        self._update_context("\n".join(history_text), reset=True)

        icons = {
            "search_knowledge": "🔍", "parse_document": "📄",
            "analyze_image": "👁", "write_file": "📝",
            "run_code": "⚡", "grep_codebase": "🔎",
        }
        for msg in data.get("messages", []):
            role = msg.get("role")
            if role == "user":
                self._post_safe(f"[#D4D4D4 bold]you ›[/] {msg['content']}",
                               css_class="user-line")
            elif role == "assistant":
                self._post_safe(f"[#89D185 bold]└─[/]\n{msg['content']}",
                               css_class="final")
            elif role == "tool_call":
                tool = msg.get("tool", "?")
                icon = icons.get(tool, "🔧")
                self._post_safe(f"[#569CD6 bold]│ {icon} {tool}[/]",
                               css_class="tool-line")
            elif role == "tool_result":
                preview = msg.get("content", "")[:100]
                self._post_safe(f"[#858585]│   ↳ {preview}…[/]",
                               css_class="tool-line")

    def action_list_outputs(self) -> None:
        self._post_safe(
            "[#007ACC bold]Generated files:[/]\n[#858585]Check ~/indicode/workspace/outputs/[/]",
            css_class="status-line",
        )

    def action_show_thread(self) -> None:
        self._post_safe(
            f"[#007ACC bold]Thread:[/] {self.client.thread_id or '(new)'}",
            css_class="status-line",
        )

    def action_show_status(self) -> None:
        self._post_safe(
            f"[#007ACC bold]Connection:[/] {self.client.base_url}\n"
            f"[#007ACC bold]Thread:[/] {self.client.thread_id or '(new)'}\n"
            f"[#007ACC bold]Context:[/] ~{self._context_tokens} tokens\n"
            f"[#007ACC bold]Sessions:[/] {len(self._sessions)}",
            css_class="status-line",
        )

    def action_show_model(self) -> None:
        self._post_safe(
            "[#007ACC bold]Model routing:[/]\n"
            "  coding    → qwen3:8b /think\n"
            "  document  → qwen3:8b\n"
            "  vision    → qwen3-vl:4b\n"
            "  search    → qwen3:8b\n"
            "  general   → qwen3:8b",
            css_class="status-line",
        )

    def action_clear_chat(self) -> None:
        def _clear():
            chat = self.query_one("#chat", VerticalScroll)
            chat.remove_children()
        self._ui(_clear)

    def _ask_approval(self, pending: dict) -> bool:
        return self.app.push_screen_wait(ApprovalModal(pending))

    def scroll_chat(self) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        chat.scroll_end(animate=False)


# Main application and homepage.

class IndicodeTUI(App):
    CSS = """
    Screen { background: #000000; }

    /* Globe is the full-screen container. Its own render() (the wireframe)
       draws first; children mounted inside it draw on top. */
    #home-globe {
        width: 100%;
        height: 1fr;
        align: center middle;
    }

    #home-content {
        width: 76%;
        max-width: 96;
        min-width: 42;
        height: auto;
    }

    #logo-text {
        color: #62c8ff;
        text-style: bold;
        content-align: center middle;
        width: 100%;
        height: auto;
    }
    #subtitle {
        color: #8d8d8d;
        text-style: italic;
        content-align: center middle;
        width: 100%;
        height: auto;
        margin: 1 0 3 0;
    }
    #input-row {
        width: 100%;
        height: auto;
        align: center middle;
    }
    #home-input {
        border: solid #383838;
        background: #1b1b1b;
        padding: 0 1;
        width: 100%;
    }
    #home-input:focus { border: solid #62c8ff; }
    #home-hints {
        display: none;
        width: 100%;
        height: 7;
        margin-top: 1;
        padding: 0 1;
        color: #62c8ff;
        background: #101010;
        border: solid #252525;
    }
    #model-info {
        color: #707070;
        content-align: center middle;
        width: 100%;
        height: auto;
        margin-top: 1;
    }
    #tip-text {
        dock: bottom;
        color: #777777;
        height: 2;
        padding: 0 2;
        background: #0b0b0b;
        border-top: solid #222222;
        content-align: center middle;
        width: 100%;
    }
    """

    TITLE = "Indicode"
    SUB_TITLE = "Sovereign AI Workbench"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, base_url: str):
        super().__init__()
        self.client = AgentClient(base_url)
        self.sessions = load_sessions()

    def compose(self) -> ComposeResult:
        # The globe is the parent. Its render() draws the wireframe;
        # the content we yield inside the with-block draws on top.
        with RotatingGlobe(id="home-globe"):
            with Vertical(id="home-content"):
                yield Static(LOGO, id="logo-text")
                yield Static(SUBTITLE, id="subtitle")
                with Horizontal(id="input-row"):
                    yield Input(
                        "",
                        placeholder="Ask anything...",
                        id="home-input",
                    )
                yield Static("", id="home-hints")
                yield Static("qwen3:8b  ·  ollama  ·  sovereign", id="model-info")

        yield Static(
            "●  Type /help for commands  ·  /prompt to send from a file",
            id="tip-text",
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "home-input":
            return
        text = event.value
        hints = self.query_one("#home-hints", Static)
        if not text.startswith("/") or "\n" in text or len(text) > 25:
            hints.update("")
            hints.display = False
            return
        query = text.lower().strip()
        matches = [
            (command, description)
            for command, description in ChatScreen._COMMANDS
            if command.startswith(query)
        ]
        if not matches:
            hints.update("")
            hints.display = False
            return
        lines = ["[#62c8ff bold]COMMANDS[/]"]
        for command, description in matches[:6]:
            lines.append(f"  [#d4d4d4 bold]{command:<14}[/] [#777777]{description}[/]")
        hints.update("\n".join(lines))
        hints.display = True

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Homepage input — check for commands before switching to chat."""
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self.query_one("#home-hints", Static).display = False

        # Handle slash commands on the homepage
        if text.startswith("/"):
            cmd = text.lower().strip("/").split()[0] if text.lower().strip("/") else ""

            # Commands that work directly on the homepage
            if cmd in ("help", "h"):
                self.push_screen(HelpModal())
                return
            elif cmd in ("quit", "q", "exit"):
                self.action_quit()
                return
            elif cmd in ("history", "hist"):
                def _on_result(thread_id):
                    if thread_id:
                        self.client.thread_id = thread_id
                        self._goto_chat(resume_thread=thread_id)
                self.push_screen(HistoryModal(self.sessions), callback=_on_result)
                return
            elif cmd in ("new", "n"):
                self.client.thread_id = None
                return
            # Other commands go to the chat screen which has full handling

        # Regular message or unrecognized command — switch to chat
        self._goto_chat(text)

    def _goto_chat(self, text: str = "", resume_thread: str = None):
        try:
            self.query_one("#home-globe", RotatingGlobe).pause()
        except Exception:
            pass

        for widget_id in ("home-globe", "tip-text"):
            try:
                self.query_one(f"#{widget_id}").display = False
            except Exception:
                pass

        chat = ChatScreen(self.client, self.sessions)
        self.push_screen(chat)
        self.call_after_refresh(
            lambda: self._start_chat(chat, text, resume_thread)
        )

    def _start_chat(self, chat: ChatScreen, message: str = "", resume_thread: str = None):
        """Route the first message after the chat screen is mounted."""
        if resume_thread:
            chat._load_conversation(resume_thread)
            return
        if message.startswith("/"):
            chat._handle_command(message)
        else:
            chat._post_safe(
                f"[#D4D4D4 bold]you ›[/] {message}",
                css_class="user-line",
            )
            chat._update_context(message)
            chat._run_agent(message)

    def action_quit(self) -> None:
        save_sessions(self.sessions)
        self.exit()