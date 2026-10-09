"""The planner as a conversation: batch I's remedy, applied to the half that plans.

Batch I found what crippled a local model as an implementer -- one JSON answer
per call, no tools, thinking off, the harness's temperature -- and replaced it
with a native-tool conversation. The planning half kept that configuration, and
after go-live run 22 nearly every failure started there: 39 of 70 task scopes
were the whole run envelope, tasks were built on code that does not exist (a
`loadPlant` call `PlantDetailView` never makes, a `src/styles/tokens.css` the
project does not have), callers were missed, and the synthesis was sent back
for unenforceable criteria in every recent run. A planner that cannot open a
file can only guess what a task will touch.

With ``policy.planner_loop = "conversation"`` the synthesis is a conversation
with the read-only tools -- search, list_files, read_file -- and ends by
calling ``propose_plan``, whose parameters are the synthesis schema itself, so
everything downstream reads its answer exactly as before. Criteria the harness
cannot enforce are sent back inside the same conversation, once. In an empty
workspace there is nothing to read, and the planner plans from the request.

What it may do is a reader's: no write, no command. What it is asked for is
unchanged; only how it is asked.
"""

from __future__ import annotations

from typing import Any

from ..contracts import SYNTHESIS_SCHEMA

PLAN_TOOL: dict[str, Any] = {
    "name": "propose_plan",
    "description": ("Your answer: the merged view and, when the request asks for work, the "
                    "tasks. Call it once, when the plan is ready."),
    "parameters": SYNTHESIS_SCHEMA,
}

PLANNER_TOOLS = """\

## Reading before you plan

You can read the workspace with search, list_files and read_file. Use them to \
check what each task would change before you propose it:

- Name in each task's scope_paths the files it changes or creates. Not a \
directory it will not touch, and never the whole project: two tasks with the same \
scope cannot be kept apart.
- Build a task on code you have read. If it changes a function, find the \
function; if it changes a caller, find the callers.
- An inspection criterion's expect reads `path/to/file: text that must be \
present`, and the path is one the task creates or one you have seen.
- Do not change how the project is built, tested or released -- its scripts, \
its CI -- unless the request asks for exactly that change. If the request and the \
project disagree about how the work is checked, say so in open_questions.

In an empty workspace there is nothing to read: plan from the request. When the \
plan is ready, call propose_plan with it.
"""

NUDGE = "Read what you need to ground the plan, then call propose_plan."
NOW = ("Your reading time is up. Call propose_plan now with the plan as it stands; "
       "name in each task's scope the files you know it changes.")


def send_back(weak: list[str]) -> str:
    """The criteria the harness cannot enforce, returned in the conversation."""
    listed = "\n".join(f"- {line}" for line in weak)
    return ("These definition-of-done criteria cannot be checked by the harness:\n\n"
            f"{listed}\n\nFix each one -- a command the harness can run, or an inspection "
            "that reads `path: text` -- and call propose_plan again with the whole plan.")
