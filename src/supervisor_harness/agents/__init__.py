"""Agent roles, discovery and briefing."""

from .brief import (
    build_analysis_brief,
    build_execution_brief,
    build_verification_brief,
    render_directive,
    render_inbox,
)
from .registry import AgentRegistry, AvailableAgent
from .roles import (
    ALL_ROLES,
    ANALYSIS_ROLES,
    EXECUTION_ROLES,
    ROLES_BY_ID,
    VERIFICATION_ROLES,
    Role,
    get_role,
    role_for_task,
    score_lenses,
    select_lenses,
    task_complexity,
)

__all__ = [
    "ALL_ROLES",
    "ANALYSIS_ROLES",
    "EXECUTION_ROLES",
    "ROLES_BY_ID",
    "VERIFICATION_ROLES",
    "AgentRegistry",
    "AvailableAgent",
    "Role",
    "build_analysis_brief",
    "build_execution_brief",
    "build_verification_brief",
    "get_role",
    "render_directive",
    "render_inbox",
    "role_for_task",
    "score_lenses",
    "select_lenses",
    "task_complexity",
]
