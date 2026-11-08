"""Mechanical lessons are recorded once per run (review finding 8)."""

from __future__ import annotations

import pytest

from supervisor_harness.core import phases
from supervisor_harness.core.supervisor import Supervisor
from supervisor_harness.models import Lesson, LessonCategory, RunState


async def test_entering_improvement_twice_records_the_mechanical_lessons_once(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    lesson = Lesson(category=LessonCategory.PROCESS, statement="a mechanical lesson",
                    trigger="t", why="w", how_to_apply="h")
    monkeypatch.setattr(phases, "mechanical_lessons", lambda state: [lesson])
    recorded: list[Lesson] = []
    real = supervisor.store.add_lesson

    def spy(item: Lesson, **kwargs: int) -> Lesson:
        recorded.append(item)
        return real(item, **kwargs)

    monkeypatch.setattr(supervisor.store, "add_lesson", spy)
    session = supervisor.store.create(RunState(id="run_Im", prompt="p"))

    await supervisor._run_improvement(session)
    await supervisor._run_improvement(session)

    assert [r.statement for r in recorded].count("a mechanical lesson") == 1


async def test_the_configured_occurrence_cap_reaches_the_store(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor.config.policy.lesson_max_occurrences = 2
    seen: list[int] = []
    real = supervisor.store.add_lesson

    def spy(item: Lesson, *, max_occurrences: int = 20) -> Lesson:
        seen.append(max_occurrences)
        return real(item, max_occurrences=max_occurrences)

    monkeypatch.setattr(supervisor.store, "add_lesson", spy)
    session = supervisor.store.create(RunState(id="run_Occ", prompt="p"))
    lesson = Lesson(category=LessonCategory.PROCESS, statement="cap me", trigger="t",
                    why="w", how_to_apply="h")

    for _ in range(4):
        supervisor._record_lesson(session, lesson)

    assert seen == [2, 2, 2, 2]
    assert max(le.occurrences for le in supervisor.store.lessons()) == 2
