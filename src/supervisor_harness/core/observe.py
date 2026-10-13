"""Looking at what a run built: a page, rendered as a person opening it would see it.

The first green-field run verified every task of a browser Minesweeper whose
board rendered as a single column. Every judge in it read code: asked whether
"each row's cells sit side by side, so the cells of a column line up", the
local model read the styles and the render code and passed the one-column board
three times out of three. Shown a screenshot, the same model failed it twice
out of two, and passed a correct board twice out of two. What a person would see
has to be shown, not inferred.

So a goal expectation that names a page is looked at: the page is built with
the run's own command, served from the run's tree over a local server, rendered
in a headless browser and photographed. Playwright is an optional extra
(``pip install supervisor-harness[observe]``); without it -- or without a
browser it can start: its own Chromium, else an installed Edge or Chrome -- the
expectation is judged from the code, and the evidence says so.
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .dod import resolve_runner, run_bounded, shell_split, unsafe_command

#: The size of the window the page is shown in: a laptop screen, roughly.
VIEWPORT = {"width": 1000, "height": 750}
#: How long a page is given to settle after it loads, for its script to draw.
SETTLE_MS = 800


@dataclass
class Observation:
    """A page as rendered, or why it could not be: ``png`` is ``None`` then."""

    png: bytes | None
    note: str


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return


def look(tree: Path, page: str, build: str = "", *, build_timeout: float = 600.0,
         allow_build: bool = True) -> Observation:
    """Build, serve and photograph ``page`` (relative to ``tree``, query allowed)."""
    # `file:` opens the page straight from disk, as a person double-clicking it
    # would: a page that must run with no server is shown that way, not served.
    from_disk = page.strip().startswith("file:")
    path, _, query = page.strip().removeprefix("file:").partition("?")
    path = path.strip().lstrip("/")
    if build.strip() and allow_build:
        failed = _build(tree, build.strip(), build_timeout)
        if failed:
            return Observation(None, failed)
    target = (tree / path).resolve()
    if not path or not target.is_relative_to(tree.resolve()) or not target.is_file():
        return Observation(None, f"`{path}` is not a file in the tree"
                                 + (" after the build" if build.strip() else ""))
    try:
        sync_api = importlib.import_module("playwright.sync_api")
    except ImportError:
        return Observation(None, "no renderer: the `observe` extra (Playwright) is not installed")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(tree)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = (target.as_uri() if from_disk
           else f"http://127.0.0.1:{server.server_address[1]}/{path}")
    url += f"?{query}" if query else ""
    errors: list[str] = []
    try:
        with sync_api.sync_playwright() as playwright:
            browser = _launch(playwright)
            try:
                tab = browser.new_page(viewport=VIEWPORT)
                tab.on("pageerror", lambda exc: errors.append(str(exc)))
                # A script or stylesheet the browser refused -- the whole story of a
                # page that needs a server, opened from disk -- is a console error.
                tab.on("console", lambda msg: _console(msg, errors))
                tab.goto(url, wait_until="load", timeout=30_000)
                tab.wait_for_timeout(SETTLE_MS)
                png = tab.screenshot()
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - a page that cannot be shown is judged from code
        return Observation(None, f"`{path}` could not be rendered: {exc}"[:400])
    finally:
        server.shutdown()
        server.server_close()
    note = f"`{page}`, " + ("opened from disk" if from_disk else "served from the run's tree")
    if errors:
        note += f"; the page reported: {'; '.join(errors[:3])}"[:600]
    return Observation(png, note)


def _console(message: Any, errors: list[str]) -> None:
    if message.type == "error":
        errors.append(message.text)


def _build(tree: Path, command: str, timeout: float) -> str:
    """Run the page's build as a criterion's command is run; why it failed, or ``""``."""
    refusal = unsafe_command(command)
    if refusal:
        return f"the build `{command}` was not run: {refusal}"
    argv = shell_split(command)
    executable, _ = resolve_runner(argv[0])
    if executable is None:
        return f"the build `{command}` was not run: {argv[0]!r} is not on PATH"
    argv[0] = executable
    try:
        done = run_bounded(argv, tree, timeout)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return f"the build `{command}` did not finish: {exc}"
    if done.returncode != 0:
        tail = (done.stdout + done.stderr).strip()[-400:]
        return f"the build `{command}` failed (exit {done.returncode}): {tail}"
    return ""


def _launch(playwright: Any) -> Any:
    """Playwright's own Chromium, else an installed Edge or Chrome."""
    last: Exception | None = None
    for options in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
        try:
            return playwright.chromium.launch(**options)
        except Exception as exc:  # noqa: BLE001 - try the next browser
            last = exc
    raise RuntimeError(f"no browser could be started: {last}")
