"""HAPi SessionSummary 状态与消息流的防御式解析。"""
from __future__ import annotations

from typing import Any

from .models import ConversationRound, SessionState


_SKIP_PREFIXES = (
    "<command-name>",
    "<local-command-stdout>",
    "<system-reminder>",
    "# AGENTS.md instructions",
    "<environment_context>",
    "Caveat:",
    "⚠",
)


def metadata(summary: dict[str, Any]) -> dict[str, Any]:
    value = summary.get("metadata")
    return value if isinstance(value, dict) else {}


def display_name(summary: dict[str, Any]) -> str:
    meta = metadata(summary)
    nested_summary = meta.get("summary")
    nested_text = nested_summary.get("text") if isinstance(nested_summary, dict) else ""
    value = meta.get("name") or nested_text
    if value:
        return str(value)
    return "等待首次 Prompt"


def flavor(summary: dict[str, Any]) -> str:
    return str(metadata(summary).get("flavor") or "HAPi")


def _pending_requests(summary: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = [
        item
        for item in (summary.get("pendingRequests") or [])
        if isinstance(item, dict)
    ]
    if explicit:
        return explicit
    agent_state = summary.get("agentState")
    requests = agent_state.get("requests") if isinstance(agent_state, dict) else None
    if not isinstance(requests, dict):
        return []
    normalized: list[dict[str, Any]] = []
    for item in requests.values():
        if not isinstance(item, dict):
            continue
        value = dict(item)
        tool = str(value.get("tool") or "")
        value.setdefault("kind", "input" if tool == "AskUserQuestion" else "permission")
        value.setdefault("since", value.get("createdAt"))
        normalized.append(value)
    return normalized


def state_from_summary(
    summary: dict[str, Any],
    native_state: SessionState | None = None,
) -> tuple[SessionState, str]:
    pending = _pending_requests(summary)
    input_request = next((item for item in pending if item.get("kind") == "input"), None)
    if input_request:
        tool = str(input_request.get("tool") or "")
        return SessionState.WAITING_INPUT, f"等待输入 {tool}".rstrip()
    if pending:
        tool = str(pending[0].get("tool") or "")
        return SessionState.WAITING_APPROVAL, f"等待审批 {tool}".rstrip()
    if summary.get("thinking"):
        return SessionState.WORKING, "Agent 处理中"
    background = int(summary.get("backgroundTaskCount") or 0)
    if background:
        return SessionState.BACKGROUND, f"后台任务中 x{background}"
    if native_state == SessionState.WORKING:
        return SessionState.WORKING, "Agent 处理中"
    if native_state == SessionState.IDLE:
        return SessionState.IDLE, "在线待命"
    return SessionState.IDLE, "在线待命"


def _collect_text(node: Any, output: list[str], *, budget: int = 8000) -> None:
    if sum(len(part) for part in output) >= budget:
        return
    if isinstance(node, str):
        if node.strip():
            output.append(node)
        return
    if isinstance(node, list):
        for item in node:
            _collect_text(item, output, budget=budget)
        return
    if not isinstance(node, dict):
        return
    text = node.get("text")
    if isinstance(text, str) and node.get("type") in (
        "text",
        "input_text",
        "user-text",
        None,
    ):
        if text.strip():
            output.append(text)
        return
    for key in ("content", "message", "data", "parts", "text"):
        if key in node:
            _collect_text(node[key], output, budget=budget)


def message_role(message: dict[str, Any]) -> str:
    content = message.get("content")
    return str(content.get("role") or "?") if isinstance(content, dict) else "?"


def message_text(message: dict[str, Any], *, limit: int = 4000) -> str:
    output: list[str] = []
    _collect_text(message.get("content"), output, budget=max(limit * 2, 8000))
    return " ".join(part.strip() for part in output if part.strip())[:limit]


def _is_real_user_text(text: str) -> bool:
    stripped = text.lstrip()
    return bool(stripped) and not any(stripped.startswith(prefix) for prefix in _SKIP_PREFIXES)


def normalize_first_prompt(value: Any, *, limit: int = 1200) -> str:
    text = str(value or "").strip()
    return text[:limit] if _is_real_user_text(text) else ""


def first_user_prompt(messages: list[dict[str, Any]], *, limit: int = 600) -> str:
    for message in sorted(messages, key=lambda item: item.get("seq", 0)):
        if message_role(message) != "user":
            continue
        text = message_text(message, limit=limit)
        if _is_real_user_text(text):
            return normalize_first_prompt(text, limit=limit)
    return ""


def _message_body(message: dict[str, Any]) -> dict[str, Any]:
    outer = message.get("content")
    if not isinstance(outer, dict):
        return {}
    body = outer.get("content")
    return body if isinstance(body, dict) else outer


def _agent_conclusion(message: dict[str, Any]) -> str:
    body = _message_body(message)
    data = body.get("data")
    if isinstance(data, dict):
        kind = data.get("type")
        if kind == "assistant":
            output: list[str] = []
            _collect_text(data.get("message"), output)
            return " ".join(part.strip() for part in output if part.strip())
        if kind in ("message", "agent_message"):
            output = []
            _collect_text(data, output)
            return " ".join(part.strip() for part in output if part.strip())
        return ""
    return message_text(message)


def conversation_rounds(
    messages: list[dict[str, Any]], *, max_rounds: int = 60
) -> list[ConversationRound]:
    rounds: list[ConversationRound] = []
    for message in sorted(messages, key=lambda item: item.get("seq", 0)):
        role = message_role(message)
        if role == "user":
            text = message_text(message)
            if _is_real_user_text(text):
                rounds.append(
                    ConversationRound(
                        user=text,
                        created_at=message.get("createdAt"),
                    )
                )
        elif role == "agent" and rounds:
            conclusion = _agent_conclusion(message)
            if conclusion:
                rounds[-1].assistant = conclusion
    return rounds[-max_rounds:]
