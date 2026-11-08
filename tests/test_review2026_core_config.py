"""Workspace config cannot widen the grant or spend the owner's budget (review 11, 12, 13)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supervisor_harness.config import (
    KNOWN_STAGES,
    PROTECTED_PROVIDER_KEYS,
    PROTECTED_SETTINGS,
    load_config,
    write_example,
)


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SUPERVISOR_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nohome"))


def _workspace(tmp_path: Path, data: object) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "supervisor.config.json").write_text(json.dumps(data), encoding="utf-8")
    return ws


def _owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: object) -> None:
    home = tmp_path / "owner"
    home.mkdir()
    (home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("SUPERVISOR_HOME", str(home))


def test_a_workspace_cannot_empty_the_owners_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner(tmp_path, monkeypatch, {"policy": {
        "scope_envelope": ["src/**"], "scope_envelope_forbidden": ["secrets/**"],
        "envelope_max_age_days": 3}})
    ws = _workspace(tmp_path, {"policy": {
        "scope_envelope": [], "scope_envelope_forbidden": [], "envelope_max_age_days": 0}})

    cfg = load_config(ws)

    assert cfg.policy.scope_envelope == ["src/**"]
    assert cfg.policy.scope_envelope_forbidden == ["secrets/**"]
    assert cfg.policy.envelope_max_age_days == 3
    assert any("envelope_max_age_days" in r for r in cfg.rejected_settings)
    assert any("scope_envelope" in r for r in cfg.rejected_settings)


def test_a_workspace_may_still_narrow_the_envelope_and_forbid_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner(tmp_path, monkeypatch, {"policy": {
        "scope_envelope": ["src/**"], "scope_envelope_forbidden": ["secrets/**"]}})
    ws = _workspace(tmp_path, {"policy": {
        "scope_envelope": ["src/auth/**", "docs/**"], "scope_envelope_forbidden": ["vendor/**"]}})

    cfg = load_config(ws)

    assert cfg.policy.scope_envelope == ["src/auth/**"], "docs/** is outside the owner's"
    assert cfg.policy.scope_envelope_forbidden == ["secrets/**", "vendor/**"]


def test_with_no_owner_envelope_a_workspace_may_set_one(tmp_path: Path) -> None:
    cfg = load_config(_workspace(tmp_path, {"policy": {"scope_envelope": ["tests/**"]}}))
    assert cfg.policy.scope_envelope == ["tests/**"]


@pytest.mark.parametrize("key,value", [
    ("backend", "autonomous"),
    ("policy.drift_check_every", 0),
    ("policy.clean_check", False),
    ("policy.max_parallel_agents", 50),
    ("policy.execution_max_turns", 500),
    ("policy.default_max_turns", 500),
    ("policy.max_checkpoint_iterations", 50),
    ("policy.max_task_attempts", 50),
])
def test_budget_and_safety_settings_are_the_owners(
    tmp_path: Path, key: str, value: object
) -> None:
    data: dict = {}
    node = data
    *parents, leaf = key.split(".")
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value
    before = load_config(tmp_path / "nowhere")

    cfg = load_config(_workspace(tmp_path, data))

    assert (cfg.backend if key == "backend" else getattr(cfg.policy, leaf)) == (
        before.backend if key == "backend" else getattr(before.policy, leaf))
    assert any(r.startswith(key) for r in cfg.rejected_settings)


def test_a_workspace_cannot_change_a_providers_default_model(tmp_path: Path) -> None:
    before = load_config(tmp_path / "nowhere")
    cfg = load_config(_workspace(tmp_path, {"providers": {"anthropic": {"default_model": "x"}}}))
    assert cfg.providers["anthropic"].default_model == before.providers["anthropic"].default_model
    assert "default_model" in PROTECTED_PROVIDER_KEYS


def test_a_file_that_is_not_utf8_is_ignored_and_reported(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "supervisor.config.json").write_bytes(b'{"routing": {"default": "h\xe9st"}}')

    cfg = load_config(ws)

    assert any("supervisor.config.json" in r and "unreadable" in r for r in cfg.rejected_settings)


def test_out_of_range_bounds_are_raised_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner(tmp_path, monkeypatch, {"policy": {
        "max_parallel_agents": 0, "execution_max_turns": 0, "default_max_turns": -3}})

    cfg = load_config(tmp_path / "ws")

    assert cfg.policy.max_parallel_agents == 1
    assert cfg.policy.execution_max_turns == 1
    assert cfg.policy.default_max_turns == 1
    assert sum("must be at least 1" in r for r in cfg.rejected_settings) == 3


def test_the_generated_workspace_file_names_what_it_may_not_set(tmp_path: Path) -> None:
    path = write_example(tmp_path / "supervisor.config.json")
    note = json.loads(path.read_text(encoding="utf-8"))["_note"]
    for setting in PROTECTED_SETTINGS:
        assert ".".join(setting) in note
    assert load_config(tmp_path).rejected_settings == []


def test_coordination_is_a_known_stage() -> None:
    assert "coordination" in KNOWN_STAGES
