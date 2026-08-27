"""Host detection for Claude Code and Cursor."""

from .detect import CLAUDE_CODE, CURSOR, UNKNOWN, HostInfo, detect_host

__all__ = ["CLAUDE_CODE", "CURSOR", "UNKNOWN", "HostInfo", "detect_host"]
