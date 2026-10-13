"""Draw the rendered diagrams in `docs/images/` with VectorCraft, over its MCP server.

    python tools/draw_diagrams.py                  # every diagram
    python tools/draw_diagrams.py fence backends   # just these

The ASCII diagrams in `docs/architecture.md` are the reference, and stay so: they
are read in editors and terminals as often as on a page. These are the same
pictures for the web. When a diagram there changes, change its function here to
match and run this again -- the SVGs are generated, never edited by hand.

## What it needs

VectorCraft, from the ArtCraft suite, which is free and needs no account. The
CLI is found from `VECTORCRAFT_CLI`, then `vectorcraft-cli` on the PATH, then
the default Windows install. Nothing else: the MCP client below is a few lines
of JSON-RPC over the server's stdin and stdout, not a dependency.

The server is started with `docs/images/` as both its read and its write root,
so the only files it can touch are the ones it is here to write.

## Why the fonts are embedded

The diagrams are set in Inter and JetBrains Mono, which few readers have
installed. Each SVG carries the two as `@font-face` data, subset to the
characters it draws in each weight, so its text stays text -- selectable,
searchable -- and lays out the same everywhere.

GitHub serves repository images under `default-src 'none'`, which looks as
though it should block a `data:` font. It does not, for an SVG shown as an
image: tested on GitHub in Chromium and against the raw URL in Firefox, with a
control line in a font that does not exist beside lines in the embedded ones.
Safari was not tested. `--outline-text` draws text as paths instead, which no
browser can get wrong, at seven times the size.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "images"
DEFAULT_CLI = Path(r"C:\Program Files\VectorCraft\vectorcraft-cli.exe")
#: How long one tool call may take before the run gives up.
CALL_SECONDS = 120

# ---- palette ----------------------------------------------------------------------------
INK, MUTED, FAINT, LINE = "#0F172A", "#64748B", "#94A3B8", "#475569"
INDIGO, CARD_EDGE, SHADOW = "#6366F1", "#E2E8F0", "#E2E8F0"
#: fill, stroke, title, subtitle -- by what a box stands for.
KIND: dict[str, tuple[str, str, str, str]] = {
    "core":    ("#F8FAFC", "#CBD5E1", "#0F172A", "#64748B"),
    "agent":   ("#EEF2FF", "#818CF8", "#312E81", "#4F46E5"),
    "human":   ("#FFFBEB", "#F59E0B", "#78350F", "#B45309"),
    "done":    ("#ECFDF5", "#10B981", "#064E3B", "#047857"),
    "failed":  ("#FFF1F2", "#FB7185", "#881337", "#BE123C"),
    "record":  ("#312E81", "#312E81", "#FFFFFF", "#C7D2FE"),
}
SANS, MONO = "Inter", "JetBrains Mono"
Point = tuple[float, float]


def find_cli() -> Path:
    named = os.environ.get("VECTORCRAFT_CLI") or shutil.which("vectorcraft-cli")
    cli = Path(named) if named else DEFAULT_CLI
    if not cli.is_file():
        raise SystemExit(f"VectorCraft's CLI was not found at {cli}; set VECTORCRAFT_CLI.")
    return cli


class VectorCraft:
    """One headless VectorCraft session, driven through its MCP tools."""

    def __init__(self, cli: Path, root: Path) -> None:
        self.proc = subprocess.Popen(
            [str(cli), "mcp", "--automation-read-root", str(root),
             "--automation-write-root", str(root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.replies: queue.Queue[dict[str, Any]] = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "draw_diagrams", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self) -> None:
        if self.proc.stdout is None:
            return
        for line in self.proc.stdout:
            self.replies.put(json.loads(line))

    def _send(self, message: dict[str, Any]) -> None:
        if self.proc.stdin is None:
            raise RuntimeError("VectorCraft's stdin is closed")
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            reply = self.replies.get(timeout=CALL_SECONDS)
            if reply.get("id") == self.next_id:
                return reply

    def tool(self, name: str, **arguments: Any) -> Any:
        reply = self._rpc("tools/call", {"name": name, "arguments": arguments})
        result = reply.get("result") or {}
        text = "".join(c.get("text", "") for c in result.get("content", []))
        if "error" in reply or result.get("isError"):
            raise RuntimeError(f"{name}: {reply.get('error') or text[:400]}")
        try:
            return json.loads(text)
        except ValueError:
            return text

    def run(self, command: str, **params: Any) -> Any:
        return self.tool("command_run", id=command, params=params)

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait()


class Sheet:
    """Drawing primitives in the house style, on one artboard."""

    def __init__(self, vc: VectorCraft, width: float, height: float, title: str,
                 description: str) -> None:
        self.vc, self.width, self.height = vc, width, height
        vc.run("file.new", title=title, width=width, height=height, units="Pixels")
        vc.run("file.info", title=title, description=description)
        vc.tool("draw_shape", shape="rectangle", x=0, y=0, width=width, height=height, radius=24,
                fill="#FFFFFF", stroke=CARD_EDGE, strokeWidth=1)

    # -- type --------------------------------------------------------------------------
    def text(self, s: str, x: float, y: float, size: float, color: str, *, font: str = SANS,
             style: str = "Regular", width: float | None = None, lines: int = 1,
             align: str = "left", tracking: float = 0) -> int:
        """Point type with its baseline at `y`, or area type with its top at `y` when
        `width` is given (wrapped to `lines` lines)."""
        if width is None:
            made = self.vc.tool("add_text", text=s, x=x, y=y, size=size, font=font, color=color)
        else:
            made = self.vc.tool("add_text", text=s, x=x, y=y, width=width,
                                height=size * (1.25 * lines + 0.35), size=size, font=font,
                                color=color)
        tid = int(made["id"]) if isinstance(made, dict) else int(made)
        self.vc.run("text.setStyle", ids=[tid], font=font, style=style, size=size,
                    justify=align, tracking=tracking)
        return tid

    def note(self, s: str, x: float, y: float, *, color: str = MUTED, width: float | None = None,
             lines: int = 1, align: str = "left", font: str = SANS) -> int:
        return self.text(s, x, y, 11.5, color, font=font, style="Medium", width=width,
                         lines=lines, align=align)

    def heading(self, kicker: str, title: str, subtitle: str) -> None:
        self.text(kicker, 56, 40, 11.5, INDIGO, style="Bold", tracking=120)
        self.text(title, 56, 76, 26, INK, style="Bold")
        self.text(subtitle, 56, 104, 13.5, MUTED)

    # -- shapes ------------------------------------------------------------------------
    def box(self, x: float, y: float, w: float, h: float, name: str, sub: str, kind: str, *,
            dashed: bool = False, mono: bool = True, sub_lines: int = 1) -> None:
        fill, stroke, title, subtitle = KIND[kind]
        self.vc.tool("draw_shape", shape="rectangle", x=x, y=y + 3, width=w, height=h,
                     radius=14, fill=SHADOW, stroke="none")
        self.vc.tool("draw_shape", shape="rectangle", x=x, y=y, width=w, height=h, radius=14,
                     fill=fill, stroke=stroke, strokeWidth=1.5)
        if dashed:
            self.vc.run("stroke.set", dash=[5, 4], cap="round")
        top = y + (h - 16 - 14 * sub_lines - 12) / 2
        self.text(name, x, top, 16, title, font=MONO if mono else SANS, style="Bold", width=w,
                  align="center")
        self.text(sub, x + 12, top + 28, 12, subtitle, width=w - 24, lines=sub_lines,
                  align="center")

    def pill(self, x: float, y: float, w: float, label: str, *, fill: str = INK,
             color: str = "#FFFFFF", font: str = SANS) -> None:
        self.vc.tool("draw_shape", shape="rectangle", x=x, y=y, width=w, height=30, radius=15,
                     fill=fill, stroke="none")
        self.text(label, x, y + 8, 13, color, font=font, style="SemiBold", width=w,
                  align="center")

    def head(self, x: float, y: float, direction: str, color: str) -> None:
        s = 6
        points = {"right": [[x, y], [x - 2 * s, y - s], [x - 2 * s, y + s]],
                  "left": [[x, y], [x + 2 * s, y - s], [x + 2 * s, y + s]],
                  "down": [[x, y], [x - s, y - 2 * s], [x + s, y - 2 * s]],
                  "up": [[x, y], [x - s, y + 2 * s], [x + s, y + 2 * s]]}[direction]
        self.vc.tool("draw_path", points=points, closed=True, fill=color, stroke="none")

    def wire(self, points: list[Point], direction: str, *, color: str = LINE,
             dash: list[float] | None = None, arrow: bool = True) -> None:
        """A polyline, ending in an arrowhead that sits on its last point."""
        pts = [[px, py] for px, py in points]
        if arrow:
            back = {"right": (-10, 0), "left": (10, 0), "down": (0, -10), "up": (0, 10)}
            dx, dy = back[direction]
            pts[-1] = [pts[-1][0] + dx, pts[-1][1] + dy]
        self.vc.tool("draw_path", points=pts, fill="none", stroke=color, strokeWidth=1.75)
        self.vc.run("stroke.set", cap="round", join="round", **({"dash": dash} if dash else {}))
        if arrow:
            self.head(points[-1][0], points[-1][1], direction, color)

    def rule(self, y: float) -> None:
        self.vc.tool("draw_path", points=[[56, y], [self.width - 56, y]], fill="none",
                     stroke=CARD_EDGE, strokeWidth=1)

    def legend(self, y: float, items: list[tuple[str, str, str]], source: str) -> None:
        """Samples along the bottom: (kind, label, how) -- how is "box", "dashed" for a
        dashed box, or "line" for a dashed line in the kind's stroke."""
        self.rule(y - 22)
        x = 56.0
        for kind, label, how in items:
            fill, stroke, _, _ = KIND[kind]
            if how == "line":
                self.vc.tool("draw_path", points=[[x, y + 9], [x + 30, y + 9]], fill="none",
                             stroke=FAINT, strokeWidth=1.75)
                self.vc.run("stroke.set", dash=[6, 5], cap="round")
                x += 12
            else:
                self.vc.tool("draw_shape", shape="rectangle", x=x, y=y, width=18, height=18,
                             radius=5, fill=fill, stroke=stroke, strokeWidth=1.5)
            if how == "dashed":
                self.vc.run("stroke.set", dash=[3, 3], cap="round")
            self.note(label, x + 28, y + 13.5, color=LINE)
            x += 28 + len(label) * 6.3 + 36
        self.note(f"Details: {source}", self.width - 56 - 420, y + 2, color=FAINT, width=420,
                  align="right")

    def export(self, path: Path, *, outline: bool = False) -> None:
        options: dict[str, Any] = {"decimals": 2}
        if not outline:
            options["embedFonts"] = True
        self.vc.tool("export", path=str(path), outlineText=outline, options=options)


# ---- the diagrams --------------------------------------------------------------------------

def run_lifecycle(sheet: Sheet) -> None:
    """The phase machine, from `docs/architecture.md`."""
    bw, bh = 220, 76
    cols, rows = [100, 400, 700, 1000], [184, 384, 584]

    def cx(c: int) -> float:
        return cols[c] + bw / 2

    def cy(r: int) -> float:
        return rows[r] + bh / 2

    def box(c: int, r: int, name: str, sub: str, kind: str, dashed: bool = False) -> None:
        sheet.box(cols[c], rows[r], bw, bh, name, sub, kind, dashed=dashed)

    sheet.heading("SUPERVISOR-HARNESS", "The lifecycle of a run",
                  "Every arrow is a PHASE_CHANGED event on the run's log, so a run resumes at "
                  "whatever phase it reached.")
    sheet.pill(cx(0) - 60, 128, 120, "your task")
    sheet.wire([(cx(0), 158), (cx(0), rows[0])], "down")

    box(0, 0, "created", "run logged, resumable", "core")
    box(1, 0, "analyzing", "parallel lenses, drift checked", "agent")
    box(2, 0, "synthesizing", "findings become proposed tasks", "core")
    box(3, 0, "awaiting_approval", "you approve each task", "human")
    for c in range(3):
        sheet.wire([(cols[c] + bw, cy(0)), (cols[c + 1], cy(0))], "right")
    sheet.wire([(cx(3), rows[0] + bh), (cx(3), rows[1])], "down")
    sheet.note("approved by you,", cx(3) - 214, 304, width=200, align="right")
    sheet.note("or under an envelope grant", cx(3) - 214, 320, width=200, align="right")

    box(3, 1, "executing", "agents work inside their scope", "agent")
    box(2, 1, "verifying", "definition of done, with evidence", "agent")
    box(1, 1, "checkpoint", "every criterion proven?", "core")
    box(0, 1, "awaiting_owner", "an open escalation waits for you", "human")
    for c in (3, 2, 1):
        sheet.wire([(cols[c], cy(1)), (cols[c - 1] + bw, cy(1))], "left")
    sheet.note("passed", cols[0] + bw + 18, cy(1) - 22)

    loop_y = rows[1] + bh + 44
    sheet.wire([(cx(1), rows[1] + bh), (cx(1), loop_y), (cx(3), loop_y), (cx(3), rows[1] + bh)],
               "up", color=INDIGO)
    sheet.note("not passed: reopen the tasks that fell short, with the checkpoint's corrections",
               cx(1), loop_y + 18, color=INDIGO, width=cx(3) - cx(1), align="center")

    box(0, 2, "improving", "lessons for the next run", "core")
    box(1, 2, "complete", "verified report delivered", "done")
    box(3, 2, "failed", "unrecoverable, from any phase", "failed", dashed=True)
    sheet.wire([(cx(0), rows[1] + bh), (cx(0), rows[2])], "down")
    sheet.note("resolved, or none open", cx(0) + 12, rows[1] + bh + 66)
    sheet.wire([(cols[0] + bw, cy(2)), (cols[1], cy(2))], "right")
    sheet.wire([(cols[1] + bw, cy(2)), (cols[2] + 14, cy(2))], "right")
    sheet.pill(cols[2] + 14, cy(2) - 15, 196, "report + verified DoD", fill="#10B981")

    bypass = rows[0] + bh + 46
    sheet.wire([(cx(2), rows[0] + bh), (cx(2), bypass), (52, bypass), (52, cy(2)),
                (cols[0], cy(2))], "right", color=FAINT, dash=[6, 5])
    sheet.note("report mode, or no tasks proposed", 64, bypass - 18)

    sheet.legend(700, [("agent", "agents at work, drift-supervised", "box"),
                       ("human", "waits for you", "box"), ("core", "harness bookkeeping", "box"),
                       ("done", "finish line", "box"), ("core", "report-only path", "line")],
                 "docs/architecture.md")


def event_log(sheet: Sheet) -> None:
    """What is written where, and what survives a crash."""
    sheet.heading("SUPERVISOR-HARNESS", "What is written where",
                  "The event log is the only thing that is not derived. Everything else can be "
                  "rebuilt from it, and is.")
    col = 420.0
    sheet.pill(col - 110, 140, 220, "something happens")
    sheet.note("a turn, a directive, a decision, a verdict", col + 126, 160)

    sheet.wire([(col, 170), (col, 244)], "down", color=INDIGO)
    sheet.note("emit / aemit", col + 16, 202, color=INDIGO, font=MONO)
    sheet.note("one advisory lock, one fsync, one batch per turn", col + 16, 220)

    sheet.box(col - 160, 244, 320, 92, "runs/<id>/events.jsonl", "AUTHORITATIVE", "record")
    sheet.note("append-only, ordered by seq, never rewritten", col + 186, 284, color=LINE)
    sheet.note("credential-shaped strings redacted on the way in", col + 186, 302, color=LINE)

    sheet.wire([(col, 336), (col, 414)], "down")
    sheet.note("fold(events)", col + 16, 370, color=INK, font=MONO)
    sheet.note("replay in seq order, one event at a time", col + 16, 388)

    sheet.box(col - 120, 414, 240, 76, "RunState", "the run as it stands", "core")
    sheet.note("agents, tasks, findings, turns, facts, directives,", col + 146, 446, color=LINE)
    sheet.note("and its own damage signals", col + 146, 464, color=LINE)

    left, right, fork = col - 195, col + 195, 534.0
    sheet.wire([(col, 490), (col, fork)], "down", arrow=False)
    sheet.wire([(col, fork), (left, fork), (left, 572)], "down")
    sheet.wire([(col, fork), (right, fork), (right, 572)], "down")

    sheet.box(left - 125, 572, 250, 76, "state.json", "a cache, + last_seq", "core", dashed=True)
    sheet.note("read only when last_seq reaches the log's", left - 125, 668, width=250,
               align="center")
    sheet.note("own tail; otherwise refolded from it", left - 125, 684, width=250, align="center")

    sheet.box(right - 125, 572, 250, 76, "index.sqlite3", "a projection", "core", dashed=True)
    sheet.note("one row per run, agent, task, criterion,", right - 125, 668, width=250,
               align="center")
    sheet.note("finding, message and event", right - 125, 684, width=250, align="center")

    sheet.text("A crash costs what had not been emitted yet,", 900, 572, 14, INK, style="SemiBold")
    sheet.text("never something that had.", 900, 592, 14, INK, style="SemiBold")
    sheet.note("A snapshot from a process that died mid-run is", 900, 620)
    sheet.note("behind, not wrong: the next read refolds. Losing", 900, 638)
    sheet.note("the index costs the answers until supervisor", 900, 656)
    sheet.note("reindex is run, not the run itself.", 900, 674)

    sheet.legend(760, [("record", "authoritative: the record", "box"),
                       ("core", "in memory, folded from the log", "box"),
                       ("core", "on disk, derived and rebuildable", "dashed")],
                 "docs/architecture.md, What is written where")


def backends(sheet: Sheet) -> None:
    """Where host-delegated and autonomous runs diverge, and where they meet."""
    sheet.heading("SUPERVISOR-HARNESS", "Where the two backends diverge",
                  "Both run the same supervision path. The difference is who calls the model; "
                  "host-delegated, the harness is not in the model path at all.")
    columns = [(330.0, "HOST-DELEGATED  (default)", "supervisor_start", "work packet",
                ("Claude Code / Cursor", "its own tools, its own permission model"),
                ("supervisor_report", "")),
               (950.0, "AUTONOMOUS", "supervisor run", "CompletionRequest",
                ("provider", "openrouter, ollama, anthropic, bedrock"),
                ("tool rounds", "against the sandboxed toolset"))]
    for x, header, entry, request, model, back in columns:
        sheet.text(header, x - 200, 150, 12, INDIGO, style="Bold", width=400, align="center",
                   tracking=100)
        sheet.vc.tool("draw_path", points=[[x - 150, 172], [x + 150, 172]], fill="none",
                      stroke="#C7D2FE", strokeWidth=1.5)
        sheet.pill(x - 100, 190, 200, entry, font=MONO)
        sheet.wire([(x, 220), (x, 258)], "down")
        sheet.box(x - 120, 258, 240, 76, "harness", "writes the brief", "core")
        sheet.wire([(x, 334), (x, 410)], "down")
        sheet.note(request, x + 14, 377, color=INK, font=MONO)
        sheet.box(x - 150, 410, 300, 76, model[0], model[1], "agent", mono=False)
        sheet.wire([(x, 486), (x, 566)], "down")
        sheet.note(back[0], x + 14, 522, color=INK, font=MONO if not back[1] else SANS)
        if back[1]:
            sheet.note(back[1], x + 14, 540)

    sheet.box(120, 566, 1040, 92, "the same code, both sides",
              "record the turn  ·  assess drift  ·  issue a directive  ·  route the messages  ·  "
              "verify", "record", mono=False)

    sheet.legend(720, [("core", "the harness", "box"), ("agent", "who calls the model", "box"),
                       ("record", "shared supervision", "box")],
                 "docs/architecture.md, Where the two backends diverge")


def fence(sheet: Sheet) -> None:
    """Four ceilings, each narrower than the one above, over a floor nothing narrows."""
    sheet.heading("SUPERVISOR-HARNESS", "The fence",
                  "Four ceilings, each narrower than the one above it. Authority only ever "
                  "narrows, and every narrowing is recorded.")
    layers = [
        ("THE WORKSPACE", "", "One run, one workspace root: every glob, the tool fence and the "
         "baseline commit are relative to it.", "#F8FAFC", "#CBD5E1"),
        ("THE RUN ENVELOPE", "policy.scope_envelope", "Granted once, before any task exists. The "
         "plan may narrow it; nothing widens it, not approval, not a task. It carries a date, "
         "and a stale grant is asked again.", "#F5F7FF", "#C7D2FE"),
        ("THE TASK'S SCOPE", "", "Proposed by the synthesis model and attenuated to the envelope. "
         "Editing it at approval clamps it the same way.", "#EEF2FF", "#A5B4FC"),
        ("THE AGENT'S SCOPE", "", "Attenuated at spawn to the envelope, to its task's scope and "
         "to its spawner's, so a verifier is never handed a wider fence than the work it "
         "judges.", "#E0E7FF", "#6366F1"),
    ]
    top, bottom, inset, step = 140.0, 630.0, 56.0, 110.0
    for i, (name, tag, desc, fill, stroke) in enumerate(layers):
        x, y = inset + 40 * i, top + step * i
        w, h = sheet.width - 2 * x, bottom - y - 20 * i
        sheet.vc.tool("draw_shape", shape="rectangle", x=x, y=y, width=w, height=h,
                      radius=18 - 2 * i, fill=fill, stroke=stroke, strokeWidth=1.5)
        sheet.text(name, x + 24, y + 32, 13, "#312E81", style="Bold", tracking=80)
        if tag:
            sheet.note(tag, x + w - 24 - 300, y + 20, color=INDIGO, width=300, align="right",
                       font=MONO)
        sheet.text(desc, x + 24, y + 46, 13, LINE, width=min(w - 48, 860), lines=2)

    sheet.vc.tool("draw_shape", shape="rectangle", x=56, y=650, width=sheet.width - 112,
                  height=96, radius=14, fill=INK, stroke="none")
    sheet.text("THE FLOOR", 80, 682, 13, "#FFFFFF", style="Bold", tracking=80)
    sheet.text("under all four, and not narrowed by anything above it", 180, 682, 13, "#94A3B8",
               style="Medium")
    sheet.text("No agent writes to .git, .hg or .svn (a hook is code the tool runs) or to the "
               "harness's own store (the log every claim in the run is judged against), "
               "whatever its scope says, in either backend.", 80, 696, 13, "#E2E8F0",
               width=sheet.width - 160, lines=2)

    sheet.note("A scope that exceeds its ceiling is narrowed to the intersection, not refused, "
               "and the ceiling that bit is recorded on the log.", 56, 782)
    sheet.note("Details: docs/architecture.md, The fence", sheet.width - 56 - 300, 770,
               color=FAINT, width=300, align="right")


#: name -> (draw, width, height, title, description)
DIAGRAMS: dict[str, tuple[Callable[[Sheet], None], int, int, str, str]] = {
    "run-lifecycle": (run_lifecycle, 1280, 770, "The lifecycle of a supervisor-harness run",
                      "created, analyzing, synthesizing, awaiting_approval, executing, "
                      "verifying, checkpoint (not passed: back to executing), awaiting_owner "
                      "while an escalation is open, improving, complete; report mode goes from "
                      "synthesizing to improving; failed is reachable from any phase."),
    "event-log": (event_log, 1280, 830, "What a supervisor-harness run writes where",
                  "Every event is appended to runs/<id>/events.jsonl under a lock and fsynced; "
                  "RunState is folded from it, and state.json and index.sqlite3 are derived "
                  "from RunState and rebuildable."),
    "backends": (backends, 1280, 790, "Where the two supervisor-harness backends diverge",
                 "Host-delegated: the harness hands a work packet to Claude Code or Cursor, "
                 "which report back. Autonomous: the harness calls a provider and runs tool "
                 "rounds itself. Both meet in the same supervision code."),
    "fence": (fence, 1280, 810, "The supervisor-harness fence",
              "Nested ceilings: the workspace, the run envelope, the task's scope and the "
              "agent's scope, each narrower than the last, over a floor no agent writes to: "
              "version control directories and the harness's own store."),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("names", nargs="*", metavar="name",
                        help=f"diagrams to draw (default: all of {', '.join(DIAGRAMS)})")
    parser.add_argument("--outline-text", action="store_true",
                        help="draw text as paths instead of embedding the fonts")
    args = parser.parse_args(argv)
    unknown = [n for n in args.names if n not in DIAGRAMS]
    if unknown:
        parser.error(f"no diagram named {', '.join(unknown)}; there are {', '.join(DIAGRAMS)}")
    OUT.mkdir(parents=True, exist_ok=True)
    vc = VectorCraft(find_cli(), OUT)
    try:
        for name in args.names or list(DIAGRAMS):
            draw, width, height, title, description = DIAGRAMS[name]
            sheet = Sheet(vc, width, height, title, description)
            draw(sheet)
            sheet.export(OUT / f"{name}.svg", outline=args.outline_text)
            print(f"wrote docs/images/{name}.svg")
    finally:
        vc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
