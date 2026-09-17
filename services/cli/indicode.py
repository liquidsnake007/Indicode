#!/usr/bin/env python3
"""
Indicode — Sovereign AI Workbench CLI
A terminal client for the on-premise Indicode agent.
"""
import httpx
import json
import sys
import os
import time
import argparse
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from rich.prompt import Confirm, Prompt
from rich.live import Live
from rich.text import Text
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory

console = Console()

# Configuration.
AGENT_URL = os.environ.get("INDICODE_AGENT_URL", "http://agent:8003")
HISTORY_FILE = os.path.expanduser("~/.indicode_history")

def print_banner(console):
    """Indicode banner — blue, verified Ansi Shadow font."""
    lines = [
        "██╗███╗   ██╗██████╗ ██╗ ██████╗ ██████╗ ██████╗ ███████╗",
        "██║████╗  ██║██╔══██╗██║██╔════╝██╔═══██╗██╔══██╗██╔════╝",
        "██║██╔██╗ ██║██║  ██║██║██║     ██║   ██║██║  ██║█████╗  ",
        "██║██║╚██╗██║██║  ██║██║██║     ██║   ██║██║  ██║██╔══╝  ",
        "██║██║ ╚████║██████╔╝██║╚██████╗╚██████╔╝██████╔╝███████╗",
        "╚═╝╚═╝  ╚═══╝╚═════╝ ╚═╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝",
    ]

    console.print()
    for line in lines:
        console.print(line, style="bold blue")
    console.print()
    console.print("               Sovereign On-Premise AI Workbench", style="dim italic")
    console.print()

class IndicodeCLI:
    def __init__(self, agent_url: str):
        self.agent_url = agent_url
        self.thread_id = None
        self.session_start = time.time()
        self.messages_sent = 0

    # API calls.
    def chat(self, message: str) -> dict:
        """Send a message to the agent, handle the response."""
        payload = {"message": message, "thread_id": self.thread_id}
        with httpx.Client(timeout=600.0) as client:
            response = client.post(
                f"{self.agent_url}/chat",
                json=payload,
            )
            response.raise_for_status()
            result = response.json()

        if self.thread_id is None:
            self.thread_id = result["thread_id"]
        return result

    def approve(self, decision: str, edits: dict = None) -> dict:
        """Approve or reject a pending action."""
        payload = {
            "thread_id": self.thread_id,
            "decision": decision,
            "edits": edits or {},
        }
        with httpx.Client(timeout=600.0) as client:
            response = client.post(
                f"{self.agent_url}/approve",
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    # Display helpers.
    def show_response(self, result: dict):
        """Render the agent's response nicely."""
        task_type = result.get("task_type", "unknown")
        tools_used = result.get("tool_calls_made", [])

        # Header line: task type + tools
        header_parts = [f"[bold cyan]{task_type}[/]"]
        if tools_used:
            unique_tools = list(dict.fromkeys(tools_used))
            header_parts.append(" → ".join(unique_tools))
        console.print(f"[dim]┌─[/dim] " + " [dim]·[/dim] ".join(header_parts))

        if result.get("pending_approval"):
            self.show_approval(result["pending_approval"])
        elif result.get("response"):
            console.print(Panel(
                Markdown(result["response"]),
                border_style="green",
                padding=(1, 2),
            ))
        else:
            console.print("[dim](tool execution complete — no text response)[/dim]")

        console.print(f"[dim]└─[/dim]")

    def show_approval(self, approval: dict):
        """Display the approval prompt and handle the decision."""
        calls = approval.get("pending_calls", [])
        console.print(Panel(
            "[bold yellow]⚠  APPROVAL REQUIRED[/]",
            border_style="yellow",
        ))

        for i, call in enumerate(calls, 1):
            tool_name = call.get("tool", "unknown")
            args_preview = call.get("args_preview", "")[:400]
            console.print(f"  [bold]{i}.[/] [magenta]{tool_name}[/]")
            # Show a truncated preview of the args
            console.print(f"     [dim]{args_preview}...[/dim]")

        console.print()

        # Interactive approval
        choice = Prompt.ask(
            "  Approve this action?",
            choices=["y", "n", "v"],
            default="y",
        )

        if choice == "y":
            console.print("[green]  ✓ Approved — executing...[/]")
            result = self.approve("approve")
            self.show_response(result)
        elif choice == "n":
            console.print("[red]  ✗ Rejected[/]")
            result = self.approve("reject")
            if result.get("response"):
                console.print(Panel(Markdown(result["response"]), border_style="red"))
        elif choice == "v":
            # View full args
            for i, call in enumerate(calls, 1):
                console.print(Panel(
                    call.get("args_preview", ""),
                    title=f"[magenta]{call.get('tool')}[/] — full args",
                ))
            choice2 = Prompt.ask(
                "  Approve after viewing?",
                choices=["y", "n"],
                default="y",
            )
            if choice2 == "y":
                result = self.approve("approve")
                self.show_response(result)
            else:
                result = self.approve("reject")
                if result.get("response"):
                    console.print(Panel(Markdown(result["response"]), border_style="red"))

    # Session management.
    def resume_session(self, thread_id: str):
        """Resume an existing conversation."""
        self.thread_id = thread_id
        with httpx.Client(timeout=10.0) as client:
            try:
                response = client.get(f"{self.agent_url}/state/{thread_id}")
                if response.status_code == 200:
                    state = response.json()
                    console.print(
                        f"[green]✓[/] Resumed thread {thread_id[:8]}... "
                        f"({state.get('message_count', 0)} messages)"
                    )
                else:
                    console.print(f"[red]✗[/] Thread not found")
                    self.thread_id = None
            except Exception:
                console.print("[red]✗[/] Could not reach agent service")

    # Main loop.
    def chat_streaming(self, message: str):
        """Send a message and display progress live via SSE."""
        payload = {"message": message, "thread_id": self.thread_id}
        events = []
        in_reasoning = False
        tool_icons = {
            "search_knowledge": "🔍", "parse_document": "📄",
            "analyze_image": "👁", "write_file": "📝",
            "run_code": "⚡", "read_file": "📖",
            "grep_codebase": "🔎", "list_files": "📁",
        }

        with httpx.Client(timeout=600.0) as client:
            with client.stream(
                "POST", f"{self.agent_url}/chat/stream", json=payload
            ) as response:
                buffer = ""
                for chunk in response.iter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        event_str, buffer = buffer.split("\n\n", 1)
                        if not event_str.startswith("data: "):
                            continue
                        try:
                            event = json.loads(event_str[6:])
                        except json.JSONDecodeError:
                            continue

                        events.append(event)
                        etype = event.get("type")

                        if etype == "thread":
                            if self.thread_id is None:
                                self.thread_id = event["thread_id"]

                        elif etype == "classified":
                            console.print(
                                f"[dim]┌─[/dim] [bold cyan]{event['task_type']}[/]"
                            )

                        elif etype == "reasoning_start":
                            in_reasoning = True
                            sys.stdout.write("\033[2;3m")  # dim+italic ANSI

                        elif etype == "reasoning":
                            # Raw ANSI write for speed (rich is too slow per-token)
                            sys.stdout.write(event.get("content", ""))
                            sys.stdout.flush()

                        elif etype == "reasoning_end":
                            if in_reasoning:
                                sys.stdout.write("\033[0m\n")  # reset style + newline
                                sys.stdout.flush()
                                in_reasoning = False

                        elif etype == "tool_call":
                            tool = event.get("tool", "?")
                            icon = tool_icons.get(tool, "🔧")
                            console.print(
                                f"[dim]│[/dim] {icon} [magenta]{tool}[/] [dim]...[/]"
                            )

                        elif etype == "tool_exec":
                            pass  # already shown by tool_call

                        elif etype == "tool_result":
                            preview = event.get("content_preview", "")[:120]
                            console.print(
                                f"[dim]│[/dim]   [dim]↳ {preview}...[/]"
                            )

                        elif etype == "tool_error":
                            console.print(
                                f"[dim]│[/dim]   [red]✗ {event.get('error', '')}[/]"
                            )

                        elif etype == "approval_required":
                            # Graph paused — show the interactive approval prompt
                            self.show_approval(event.get("pending", {}))

                        elif etype == "final":
                            content = event.get("content", "")
                            if content:
                                console.print(f"[dim]└─[/dim]")
                                console.print(Panel(
                                    Markdown(content),
                                    border_style="green",
                                    padding=(1, 2),
                                ))

                        elif etype == "error":
                            console.print(
                                f"[red]✗[/] {event.get('message', 'unknown error')}"
                            )

        return events

    def run(self):
        """Main REPL loop."""
        print_banner(console)
        console.print(f"[dim]Agent: {self.agent_url}[/dim]")
        console.print(f"[dim]Type your request, or 'help' for commands.[/dim]\n")

        while True:
            try:
                user_input = pt_prompt(
                    "indicode> ",
                    history=FileHistory(HISTORY_FILE),
                ).strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Goodbye.[/dim]")
                break

            if not user_input:
                continue

            # ─── Built-in commands ───────────────────────
            if user_input in ("quit", "exit", "q"):
                console.print("[dim]Goodbye.[/dim]")
                break
            elif user_input == "help":
                self.show_help()
                continue
            elif user_input == "clear":
                self.thread_id = None
                console.clear()
                print_banner(console)
                continue
            elif user_input == "new":
                self.thread_id = None
                console.print("[green]✓[/] Started new conversation")
                continue
            elif user_input.startswith("resume "):
                self.resume_session(user_input[7:].strip())
                continue
            elif user_input == "thread":
                if self.thread_id:
                    console.print(f"Current thread: {self.thread_id}")
                else:
                    console.print("No active thread (new conversation)")
                continue
            elif user_input == "outputs":
                self.show_outputs()
                continue
            elif user_input == "status":
                self.show_status()
                continue

            # ─── Send to agent ───────────────────────────
            self.messages_sent += 1
            try:
                self.chat_streaming(user_input)
            except httpx.ConnectError:
                console.print(
                    f"[red]✗[/] Cannot reach agent at {self.agent_url}\n"
                    f"     Is the agent container running? Try: docker compose ps agent"
                )
                continue
            except Exception as e:
                console.print(f"[red]✗[/] Error: {str(e)[:200]}")
                continue

    def show_help(self):
        """Show the help panel."""
        help_table = Table(title="Indicode Commands", show_header=False, box=None)
        help_table.add_column(style="cyan")
        help_table.add_column()
        help_table.add_row("help", "Show this help")
        help_table.add_row("new", "Start a new conversation")
        help_table.add_row("resume <id>", "Resume a conversation by thread ID")
        help_table.add_row("thread", "Show current thread ID")
        help_table.add_row("outputs", "List generated output files")
        help_table.add_row("status", "Show session status")
        help_table.add_row("clear", "Clear screen and start fresh")
        help_table.add_row("quit", "Exit Indicode")
        console.print(help_table)

    def show_outputs(self):
        """List files in the outputs directory."""
        output_dir = Path("/workspace/outputs")
        if not output_dir.exists():
            console.print("[dim]No outputs directory (not running in container)[/dim]")
            return
        files = list(output_dir.glob("*"))
        if not files:
            console.print("[dim]No output files yet[/dim]")
            return
        table = Table(title="Generated Files")
        table.add_column("File", style="cyan")
        table.add_column("Size", justify="right")
        table.add_column("Modified")
        for f in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
            stat = f.stat()
            size = f"{stat.st_size / 1024:.1f} KB"
            mtime = time.strftime("%H:%M:%S", time.localtime(stat.st_mtime))
            table.add_row(f.name, size, mtime)
        console.print(table)

    def show_status(self):
        """Show session statistics."""
        elapsed = time.time() - self.session_start
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        table = Table(title="Session", show_header=False, box=None)
        table.add_column(style="cyan")
        table.add_column()
        table.add_row("Thread", self.thread_id or "(new conversation)"[:20] + "..." if self.thread_id else "(new)")
        table.add_row("Messages sent", str(self.messages_sent))
        table.add_row("Uptime", f"{mins}m {secs}s")
        table.add_row("Agent URL", self.agent_url)
        console.print(table)


def main():
    parser = argparse.ArgumentParser(description="Indicode — Sovereign AI Workbench CLI")
    parser.add_argument("--agent-url", default="http://agent:8003",
                       help="URL of the Indicode agent service")
    parser.add_argument("--resume", metavar="THREAD_ID",
                       help="Resume a previous conversation")
    args = parser.parse_args()

    cli = IndicodeCLI(args.agent_url)

    if args.resume:
        cli.resume_session(args.resume)

    cli.run()


if __name__ == "__main__":
    main()