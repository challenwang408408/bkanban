"""Ghostty inventory 与 HAPi enrichment 的单向合并。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import ghostty
from .hapi_client import (
    HapiError,
    HapiService,
    PromptCache,
    attach_ttys,
    local_machine_id,
)
from .local_state import local_agent_state
from .models import (
    BoardRow,
    BoardSnapshot,
    GhosttyTerminal,
    HapiChild,
    HapiSession,
    SessionState,
)
from .sessions import (
    conversation_rounds,
    display_name,
    first_user_prompt,
    flavor,
    normalize_first_prompt,
    state_from_summary,
)
from .titles import TitleCache


TerminalLister = Callable[[], list[GhosttyTerminal]]
ChildLister = Callable[[], list[HapiChild]]
SessionLister = Callable[[], list[dict[str, Any]]]
NativeStateResolver = Callable[[dict[str, Any]], SessionState | None]


def _session_from_child(
    child: HapiChild,
    summary: dict[str, Any] | None,
    *,
    terminal_id: str,
    first_prompt: str,
    prompt_loaded: bool,
    generated_title: str,
    native_state_resolver: NativeStateResolver = local_agent_state,
) -> HapiSession:
    resolved_prompt = first_prompt or (
        normalize_first_prompt(summary.get("_firstUserMessage")) if summary else ""
    )
    display_title = generated_title or (
        "正在生成任务标题…" if resolved_prompt else "等待首次 Prompt"
    )
    if summary is None:
        return HapiSession(
            session_id=child.session_id,
            pid=child.pid,
            tty=child.tty,
            terminal_id=terminal_id,
            name=display_title,
            state=SessionState.HUB_OFFLINE,
            state_detail="HAPi Session 在线，Hub 数据未连接",
            first_prompt=resolved_prompt,
            prompt_loaded=prompt_loaded,
            title_loaded=bool(generated_title),
        )
    try:
        native_state = native_state_resolver(summary)
    except Exception:
        native_state = None
    state, detail = state_from_summary(summary, native_state)
    return HapiSession(
        session_id=child.session_id,
        pid=child.pid,
        tty=child.tty,
        terminal_id=terminal_id,
        name=display_title,
        flavor=flavor(summary),
        state=state,
        state_detail=detail,
        first_prompt=resolved_prompt,
        prompt_loaded=prompt_loaded
        or bool(normalize_first_prompt(summary.get("_firstUserMessage"))),
        title_loaded=bool(generated_title),
        updated_at=summary.get("updatedAt"),
    )


def collect_snapshot(
    *,
    service: HapiService | None = None,
    prompt_cache: PromptCache | None = None,
    title_cache: TitleCache | None = None,
    list_terminals: TerminalLister | None = None,
    list_children: ChildLister | None = None,
    list_sessions: SessionLister | None = None,
    native_state_resolver: NativeStateResolver = local_agent_state,
) -> BoardSnapshot:
    """先枚举全部 Ghostty tabs，再把 HAPi 数据 join 进去。

    除 Ghostty 枚举失败外，任何 HAPi/TTY 失败都只能降级字段，不能删 row。
    """
    service = service or HapiService()
    prompt_cache = prompt_cache or PromptCache()
    title_cache = title_cache or TitleCache()
    terminals = list((list_terminals or ghostty.enumerate_terminals)())
    tabs = ghostty.group_tabs(terminals)
    rows_by_tab = {tab.tab_id: BoardRow(tab=tab) for tab in tabs}
    warnings: list[str] = []

    fallback_children: list[HapiChild] = []
    try:
        children = (list_children or service.list_children)()
    except Exception as exc:
        children = []
        warnings.append(f"HAPi runner enrichment 失败: {exc}")

    summaries: list[dict[str, Any]] = []
    summaries_by_id: dict[str, dict[str, Any]] = {}
    try:
        summaries = (list_sessions or service.list_sessions)()
        summaries_by_id = {
            str(item.get("id") or ""): dict(item)
            for item in summaries
            if isinstance(item, dict) and item.get("id")
        }
    except Exception as exc:
        warnings.append(f"HAPi session 摘要读取失败: {exc}")

    if list_sessions is None and summaries_by_id:
        try:
            resumable = service.list_resumable()
        except HapiError as exc:
            resumable = []
            warnings.append(f"HAPi resumable 补充失败: {exc}")
        for item in resumable:
            session_id = str(item.get("sessionId") or "")
            summary = summaries_by_id.get(session_id)
            if summary is None:
                continue
            raw_meta = summary.get("metadata")
            meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
            if item.get("name"):
                meta["name"] = item["name"]
            elif item.get("summary"):
                meta["summary"] = {"text": item["summary"]}
            if item.get("flavor") and not meta.get("flavor"):
                meta["flavor"] = item["flavor"]
            summary["metadata"] = meta
            summary["_firstUserMessage"] = item.get("firstUserMessage") or ""

        known_ids = {child.session_id for child in children}
        try:
            machine_id = local_machine_id()
        except HapiError:
            machine_id = ""
            warnings.append("HAPi machineId 不可用，已禁用 hostPid fallback")
        for summary in summaries_by_id.values():
            meta = summary.get("metadata")
            meta = meta if isinstance(meta, dict) else {}
            session_id = str(summary.get("id") or "")
            host_pid = meta.get("hostPid")
            if (
                session_id
                and session_id not in known_ids
                and summary.get("active")
                and isinstance(host_pid, int)
                and machine_id
                and meta.get("machineId") == machine_id
            ):
                fallback_children.append(HapiChild(session_id=session_id, pid=host_pid))

        detail_failures = 0
        for child in children:
            summary = summaries_by_id.get(child.session_id)
            if summary is None:
                continue
            try:
                detail = service.get_session(child.session_id)
            except HapiError:
                detail_failures += 1
                continue
            if isinstance(detail, dict):
                summary.update(detail)
        if detail_failures:
            warnings.append(f"HAPi detail 暂缺 {detail_failures} 个，已保留摘要状态")

    if fallback_children:
        try:
            children.extend(attach_ttys(fallback_children))
        except HapiError as exc:
            warnings.append(f"HAPi hostPid fallback 失败: {exc}")

    tty_children = [child for child in children if child.tty]
    fallback_titles = {
        child.tty: (
            display_name(summaries_by_id[child.session_id])
            if child.session_id in summaries_by_id
            else f"HAPi {child.session_id[:8]}"
        )
        for child in tty_children
    }
    fallback_cwds = {child.tty: child.cwd for child in tty_children if child.cwd}
    try:
        mapped, mapping_errors = ghostty.map_ttys(
            [child.tty for child in tty_children],
            fallback_titles=fallback_titles,
            fallback_cwds=fallback_cwds,
            current_terminals=terminals,
        )
    except Exception as exc:
        mapped = {}
        mapping_errors = {child.tty: str(exc) for child in tty_children}

    tab_by_terminal = {
        terminal.terminal_id: terminal.tab_id for terminal in terminals
    }
    for child in tty_children:
        terminal = mapped.get(child.tty)
        if terminal is None:
            message = mapping_errors.get(child.tty, "未映射")
            warnings.append(f"HAPi {child.session_id[:8]} 未关联标签: {message}")
            continue
        tab_id = tab_by_terminal.get(terminal.terminal_id)
        row = rows_by_tab.get(tab_id or "")
        if row is None:
            continue
        summary = summaries_by_id.get(child.session_id)
        cached_prompt = prompt_cache.get(child.session_id)
        updated_at = summary.get("updatedAt") if summary else None
        resume_prompt = (
            normalize_first_prompt(summary.get("_firstUserMessage")) if summary else ""
        )
        title_prompt = cached_prompt or resume_prompt
        cached_title = title_cache.get(child.session_id, title_prompt)
        row.sessions.append(
            _session_from_child(
                child,
                summary,
                terminal_id=terminal.terminal_id,
                first_prompt=cached_prompt,
                prompt_loaded=bool(cached_prompt or resume_prompt)
                or not prompt_cache.should_fetch(child.session_id, updated_at),
                generated_title=cached_title,
                native_state_resolver=native_state_resolver,
            )
        )

    for child in children:
        if not child.tty:
            warnings.append(f"HAPi {child.session_id[:8]} 没有可见 TTY，未加入任何标签")

    rows = [rows_by_tab[tab.tab_id] for tab in tabs]
    return BoardSnapshot(rows=rows, warnings=warnings)


def hydrate_prompts(
    snapshot: BoardSnapshot,
    *,
    service: HapiService,
    prompt_cache: PromptCache,
) -> list[str]:
    """只读取 Ghostty 可见 rows 已关联的 HAPi Session。"""
    errors: list[str] = []
    seen: set[str] = set()
    for row in snapshot.rows:
        for session in row.sessions:
            if session.session_id in seen or session.prompt_loaded:
                continue
            seen.add(session.session_id)
            try:
                messages = service.all_messages(session.session_id)
                text = first_user_prompt(messages)
                if text:
                    prompt_cache.put(session.session_id, text)
                    session.first_prompt = text
                    session.name = "正在生成任务标题…"
                    session.title_loaded = False
                else:
                    prompt_cache.mark_miss(session.session_id, session.updated_at)
                session.prompt_loaded = True
            except HapiError as exc:
                errors.append(f"{session.session_id[:8]}: {exc}")
                continue
    return errors


def hydrate_history(session: HapiSession, *, service: HapiService) -> None:
    messages = service.all_messages(session.session_id)
    session.history = conversation_rounds(messages)
    if not session.first_prompt:
        session.first_prompt = first_user_prompt(messages)
