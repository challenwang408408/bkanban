"""Read-only adapters for agent-native task state.

HAPi is still the session association layer.  This module only fills the gap
where HAPi reports a locally-controlled agent as ``thinking=false`` while the
agent is actively executing a turn.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .models import SessionState
from .sessions import metadata


_START_SIGNALS = {"task_started", "turn_started"}
_STOP_SIGNALS = {
    "task_complete",
    "task_completed",
    "task_finished",
    "turn_complete",
    "turn_completed",
    "task_aborted",
    "turn_aborted",
    "task_cancelled",
    "turn_cancelled",
}
_path_cache: dict[str, Path] = {}
_state_cache: dict[tuple[str, Path], tuple[int, int, int, SessionState | None]] = {}
_cache_lock = threading.Lock()
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


def _database_paths() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home / ".codex" / "state_5.sqlite",
        home / ".codex" / "sqlite" / "state_5.sqlite",
    )


def _rollout_path(codex_session_id: str) -> Path | None:
    if not _SESSION_ID_RE.fullmatch(codex_session_id):
        return None
    with _cache_lock:
        cached = _path_cache.get(f"codex:{codex_session_id}")
    if cached and cached.is_file():
        return cached

    for database in _database_paths():
        if not database.is_file():
            continue
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=0.2)
            try:
                row = connection.execute(
                    "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1",
                    (codex_session_id,),
                ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            continue
        if not row or not row[0]:
            continue
        path = Path(str(row[0])).expanduser()
        if path.is_file():
            with _cache_lock:
                _path_cache[f"codex:{codex_session_id}"] = path
            return path
    return None


def _reverse_lines(path: Path, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    """Yield complete JSONL records from newest to oldest without loading the file."""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        remainder = b""
        while position:
            size = min(chunk_size, position)
            position -= size
            handle.seek(position)
            block = handle.read(size) + remainder
            parts = block.split(b"\n")
            remainder = parts[0]
            for line in reversed(parts[1:]):
                if line.strip():
                    yield line
        if remainder.strip():
            yield remainder


def _task_state_from_rollout(path: Path) -> SessionState | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    with _cache_lock:
        cached = _state_cache.get(("codex", path))
    signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    if cached and cached[:3] == signature:
        return cached[3]

    state: SessionState | None = None
    try:
        for raw_line in _reverse_lines(path):
            try:
                event = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            signal = str(payload.get("type") or event.get("type") or "").lower()
            if signal in _START_SIGNALS:
                state = SessionState.WORKING
                break
            if signal in _STOP_SIGNALS:
                state = SessionState.IDLE
                break
    except OSError:
        return None

    with _cache_lock:
        _state_cache[("codex", path)] = (*signature, state)
    return state


def _claude_transcript_path(agent_flavor: str, session_id: str) -> Path | None:
    if not _SESSION_ID_RE.fullmatch(session_id):
        return None
    cache_key = f"claude:{agent_flavor}:{session_id}"
    with _cache_lock:
        cached = _path_cache.get(cache_key)
    if cached and cached.is_file():
        return cached
    if agent_flavor.startswith("tclaude"):
        config_root = os.environ.get("TCLAUDE_CONFIG_DIR")
        default_root = Path.home() / ".tclaude"
    else:
        config_root = os.environ.get("CLAUDE_CONFIG_DIR")
        default_root = Path.home() / ".claude"
    root = (Path(config_root).expanduser() if config_root else default_root) / "projects"
    try:
        path = next(root.glob(f"**/{session_id}.jsonl"), None)
    except OSError:
        path = None
    if path and path.is_file():
        with _cache_lock:
            _path_cache[cache_key] = path
        return path
    return None


def _real_claude_user_event(event: dict[str, Any]) -> bool:
    if event.get("type") != "user":
        return False
    if event.get("isMeta") or event.get("isSidechain") or "toolUseResult" in event:
        return False
    message = event.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    blocks = [item for item in content if isinstance(item, dict)]
    if any(item.get("type") == "tool_result" for item in blocks):
        return False
    return any(
        item.get("type") in {"text", "input_text"}
        and bool(str(item.get("text") or "").strip())
        for item in blocks
    )


def _task_state_from_claude(path: Path) -> SessionState | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    key = ("claude", path)
    with _cache_lock:
        cached = _state_cache.get(key)
    signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    if cached and cached[:3] == signature:
        return cached[3]

    state: SessionState | None = None
    try:
        for raw_line in _reverse_lines(path):
            try:
                event = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            if _real_claude_user_event(event):
                state = SessionState.WORKING
                break
            if event.get("type") != "assistant":
                continue
            message = event.get("message")
            message = message if isinstance(message, dict) else {}
            stop_reason = str(message.get("stop_reason") or "").lower()
            if stop_reason in {"end_turn", "stop_sequence"}:
                state = SessionState.IDLE
                break
            # A streamed assistant record or tool call after the latest prompt
            # means that turn has started but has not reached an end boundary.
            state = SessionState.WORKING
            break
    except OSError:
        return None

    with _cache_lock:
        _state_cache[key] = (*signature, state)
    return state


def local_agent_state(summary: dict[str, Any]) -> SessionState | None:
    """Return an explicit native task boundary, or ``None`` when unsupported."""
    meta = metadata(summary)
    agent_flavor = str(meta.get("flavor") or "").lower()
    if "codex" in agent_flavor:
        codex_session_id = str(meta.get("codexSessionId") or "")
        if not codex_session_id:
            return None
        path = _rollout_path(codex_session_id)
        return _task_state_from_rollout(path) if path else None
    if "claude" in agent_flavor:
        claude_session_id = str(meta.get("claudeSessionId") or "")
        if not claude_session_id:
            return None
        path = _claude_transcript_path(agent_flavor, claude_session_id)
        return _task_state_from_claude(path) if path else None
    return None
