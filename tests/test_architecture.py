"""The architecture criteria from `docs/quality-assessment.md`, as a test.

Criterion 1 says package dependencies form a DAG. Finding **Q-A1** was that they
did not: `core` imported `agents.registry`, `agents.roles` and `agents.brief`,
and `agents.brief` imported back into `core` for a single constant. One import
of one string made the two packages mutually dependent.

A criterion written in a document is a criterion until someone breaks it and
nobody notices. This is the criterion executed, and it matters most for the work
that comes next: **item 2 splits `core/supervisor.py`**, which means moving
imports between modules in the package with the most edges. A split that
reintroduces a cycle would be found here rather than by whoever hits the
`ImportError` months later, when the fix is no longer a one-line move.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "supervisor_harness"


def package_graph() -> dict[str, set[str]]:
    """Which top-level subpackage imports which, from relative imports only.

    Absolute imports of the package by name would be a different smell and there
    are none; every internal import here is relative, so `node.level` is the
    reliable signal.
    """
    edges: dict[str, set[str]] = collections.defaultdict(set)

    for path in sorted(SRC.rglob("*.py")):
        module = path.relative_to(SRC).with_suffix("").as_posix().replace("/__init__", "")
        package = module.split("/")[0] if "/" in module else "<root>"

        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.ImportFrom) and node.level):
                continue
            # `from ..models import X` in `agents/brief.py`: level 2 climbs out
            # of `agents`, level 1 stays in it.
            parts = module.split("/")[:-1]
            base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
            target = "/".join(base + ([node.module.replace(".", "/")] if node.module else []))
            target_package = target.split("/")[0] if "/" in target else (target or "<root>")
            if target_package != package:
                edges[package].add(target_package)

    return dict(edges)


def test_package_dependencies_have_no_cycles() -> None:
    """Criterion 1. Q-A1 was the one violation, and it was a single import."""
    edges = package_graph()

    cycles = sorted(
        tuple(sorted((a, b)))
        for a in edges
        for b in edges[a]
        if a in edges.get(b, set())
    )

    assert not cycles, (
        "package cycle(s): " + ", ".join(f"{a} <-> {b}" for a, b in sorted(set(cycles)))
    )


def test_the_layers_point_one_way() -> None:
    """Criterion 3: `store` and `providers` are below `core`, not beside it.

    Stated as what each package may *not* import rather than as a full ordering,
    because that is the part a refactor breaks. `store` reaching up into `core`
    would make durability depend on supervision, which is the direction that
    makes an event log impossible to test on its own.
    """
    edges = package_graph()

    assert "core" not in edges.get("store", set()), "store must not depend on core"
    assert "agents" not in edges.get("store", set()), "store must not depend on agents"
    assert "core" not in edges.get("providers", set()), "providers must not depend on core"
    assert "store" not in edges.get("providers", set()), "providers must not depend on store"
    assert "core" not in edges.get("agents", set()), "agents must not depend on core (Q-A1)"


def test_models_is_a_leaf_of_the_internal_graph() -> None:
    """`models` is where shared vocabulary goes, so it must import nothing local.

    This is what makes it the right home for `BASELINE_FACT`, and the reason
    moving that constant there fixed Q-A1 rather than relocating it. A `models`
    that imported from `core` or `store` would just move the cycle.
    """
    module = ast.parse((SRC / "models.py").read_text(encoding="utf-8"))
    relative = {
        node.module
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom) and node.level and node.module
    }

    assert relative <= {"ids"}, f"models imports from the package: {sorted(relative)}"


def test_the_baseline_key_lives_with_the_dict_it_keys() -> None:
    """Q-A1, pinned where it would regress.

    The obvious "tidy-up" is to move the constant back next to the code that
    computes its value -- `core/baseline.py`, which reads better and recreates
    the cycle the moment `agents.brief` follows it there.

    A re-export from `core.baseline` was tried and dropped: it left an unused
    import for the linter to complain about, and it kept alive exactly the
    import path that caused Q-A1.
    """
    assert 'BASELINE_FACT = "baseline commit"' in (SRC / "models.py").read_text(
        encoding="utf-8"
    ), "the definition moved out of models"

    baseline = (SRC / "core" / "baseline.py").read_text(encoding="utf-8")
    assert "BASELINE_FACT = " not in baseline, "the constant moved back into core"

    brief = (SRC / "agents" / "brief.py").read_text(encoding="utf-8")
    assert "from ..core" not in brief, "agents reaches into core again (Q-A1)"
