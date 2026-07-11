"""Ghostty 标签枚举、TTY 关联和稳定 ID 跳转。"""
from __future__ import annotations

import re
import subprocess
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from urllib.parse import quote

from .models import GhosttyTab, GhosttyTerminal


class GhosttyError(RuntimeError):
    """Ghostty 枚举或控制失败。"""


_FIELD_SEP = "\x1f"
_ROW_SEP = "\x1e"
_MARK_PREFIX = "BKMAP-"
_TTY_RE = re.compile(r"^tty[A-Za-z0-9._-]+$")
_TTY_TO_TERMINAL_ID: dict[str, str] = {}
_MAPPING_LOCK = threading.Lock()


_ENUMERATE_SCRIPT = r'''
on replaceText(theText, oldText, newText)
  set AppleScript's text item delimiters to oldText
  set textItems to every text item of (theText as text)
  set AppleScript's text item delimiters to newText
  set theText to textItems as text
  set AppleScript's text item delimiters to ""
  return theText
end replaceText

on cleanField(theValue)
  set valueText to theValue as text
  set valueText to my replaceText(valueText, character id 31, " ")
  set valueText to my replaceText(valueText, character id 30, " ")
  set valueText to my replaceText(valueText, return, " ")
  set valueText to my replaceText(valueText, linefeed, " ")
  return valueText
end cleanField

set fieldSep to character id 31
set rowSep to character id 30
set outputRows to {}
set windowIndex to 0
tell application "Ghostty"
  repeat with w in windows
    set windowIndex to windowIndex + 1
    repeat with t in tabs of w
      set tabTitle to ""
      set focusedId to ""
      try
        set tabTitle to name of t
      end try
      try
        set focusedId to id of focused terminal of t
      end try
      repeat with s in terminals of t
        set termTitle to ""
        set termCwd to ""
        try
          set termTitle to name of s
        end try
        try
          set termCwd to working directory of s
        end try
        set rowText to (my cleanField(id of w)) & fieldSep & (windowIndex as text) & fieldSep & ¬
          (my cleanField(id of t)) & fieldSep & (index of t as text) & fieldSep & ¬
          (my cleanField(tabTitle)) & fieldSep & (my cleanField(id of s)) & fieldSep & ¬
          (my cleanField(termTitle)) & fieldSep & (my cleanField(termCwd)) & fieldSep & ¬
          (selected of t as text) & fieldSep & (my cleanField(focusedId))
        set end of outputRows to rowText
      end repeat
    end repeat
  end repeat
end tell
set AppleScript's text item delimiters to rowSep
set outputText to outputRows as text
set AppleScript's text item delimiters to ""
return outputText
'''


def _apple_string(value: str) -> str:
    clean = "".join(ch for ch in str(value) if unicodedata.category(ch) != "Cc")
    return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript(script: str) -> str:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhosttyError(f"AppleScript 执行失败: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip()[:240]
        lowered = detail.lower()
        if "-1743" in detail or "not authorized" in lowered or "not permitted" in lowered:
            raise GhosttyError(
                "Ghostty 自动化权限未授权，请在系统设置中允许当前终端控制 Ghostty"
            )
        if "application isn't running" in lowered or "application is not running" in lowered:
            raise GhosttyError("Ghostty 当前未运行")
        raise GhosttyError(f"AppleScript 失败: {detail or '未知错误'}")
    return result.stdout.rstrip("\r\n")


def _parse_terminals(raw: str) -> list[GhosttyTerminal]:
    if not raw:
        return []
    terminals: list[GhosttyTerminal] = []
    for row in raw.split(_ROW_SEP):
        fields = row.split(_FIELD_SEP)
        if len(fields) != 10:
            raise GhosttyError("Ghostty 返回了无法解析的标签数据")
        (
            window_id,
            window_index,
            tab_id,
            tab_index,
            tab_title,
            terminal_id,
            title,
            cwd,
            selected,
            focused_id,
        ) = fields
        try:
            parsed_window_index = int(window_index)
            parsed_tab_index = int(tab_index)
        except ValueError as exc:
            raise GhosttyError("Ghostty 返回了无效的标签序号") from exc
        terminals.append(
            GhosttyTerminal(
                window_id=window_id,
                window_index=parsed_window_index,
                tab_id=tab_id,
                tab_index=parsed_tab_index,
                tab_title=tab_title,
                terminal_id=terminal_id,
                title=title,
                cwd=cwd,
                selected=selected.strip().lower() == "true",
                focused=terminal_id == focused_id,
            )
        )
    return terminals


def enumerate_terminals() -> list[GhosttyTerminal]:
    """枚举所有 Ghostty window/tab/terminal，标签是看板唯一事实源。"""
    return _parse_terminals(_osascript(_ENUMERATE_SCRIPT))


def group_tabs(terminals: Iterable[GhosttyTerminal]) -> list[GhosttyTab]:
    grouped: dict[str, list[GhosttyTerminal]] = {}
    for terminal in terminals:
        grouped.setdefault(terminal.tab_id, []).append(terminal)
    tabs: list[GhosttyTab] = []
    for items in grouped.values():
        first = items[0]
        focused = next((item.terminal_id for item in items if item.focused), "")
        title = first.tab_title or next((item.title for item in items if item.focused), "")
        tabs.append(
            GhosttyTab(
                window_id=first.window_id,
                window_index=first.window_index,
                tab_id=first.tab_id,
                tab_index=first.tab_index,
                title=title,
                selected=first.selected,
                focused_terminal_id=focused or first.terminal_id,
                terminals=tuple(items),
            )
        )
    return sorted(tabs, key=lambda item: (item.window_index, item.tab_index))


def focus_terminal_at_bottom(terminal_id: str) -> bool:
    """聚焦既有 terminal，并用 Ghostty semantic action 滚到 scrollback 底部。

    返回 False 表示标签已成功聚焦，但 Ghostty 没有确认滚动 action。
    """
    target = _apple_string(terminal_id)
    script = f'''
tell application "Ghostty"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in terminals of t
        if id of s is {target} then
          activate window w
          select tab t
          focus s
          try
            set didScroll to perform action "scroll_to_bottom" on s
            if didScroll then return "ok"
          end try
          try
            set didKey to send key "end" modifiers "command" to s
            if didKey then return "ok"
          end try
          return "focused"
        end if
      end repeat
    end repeat
  end repeat
  return "notfound"
end tell
'''
    result = _osascript(script)
    if result == "notfound":
        raise GhosttyError("对应 Ghostty 标签已不存在，请刷新看板")
    return result == "ok"


def close_tab(tab_id: str) -> None:
    """按稳定 tab ID 精确关闭整个标签（包含其所有 split）。"""
    target = _apple_string(tab_id)
    script = f'''
tell application "Ghostty"
  repeat with w in windows
    repeat with t in tabs of w
      if id of t is {target} then
        close tab t
        return "ok"
      end if
    end repeat
  end repeat
  return "notfound"
end tell
'''
    if _osascript(script) != "ok":
        raise GhosttyError("对应 Ghostty 标签已不存在，请刷新看板")


def close_tabs(tab_ids: Iterable[str]) -> int:
    """只关闭调用时冻结的 tab ID 清单，不动态扩大范围。"""
    targets = list(dict.fromkeys(str(item) for item in tab_ids if str(item)))
    if not targets:
        return 0
    target_list = ", ".join(_apple_string(item) for item in targets)
    script = f'''
on closeOne(targetId)
  tell application "Ghostty"
    repeat with w in windows
      repeat with t in tabs of w
        if id of t is targetId then
          close tab t
          return true
        end if
      end repeat
    end repeat
  end tell
  return false
end closeOne

set targetIds to {{{target_list}}}
set closedCount to 0
repeat with targetId in targetIds
  if my closeOne(targetId as text) then set closedCount to closedCount + 1
end repeat
return closedCount as text
'''
    result = _osascript(script)
    try:
        return int(result)
    except ValueError as exc:
        raise GhosttyError("Ghostty 未返回有效的关闭数量") from exc


def _normalise_tty(tty: str) -> str:
    value = tty.strip()
    if value.startswith("/dev/"):
        value = value[5:]
    if not _TTY_RE.fullmatch(value):
        raise GhosttyError(f"无效的 TTY: {tty!r}")
    return value


def _sanitize_title(title: str) -> str:
    return "".join(ch for ch in str(title) if unicodedata.category(ch) != "Cc")[:160]


def _write_title(tty: str, title: str) -> None:
    safe_tty = _normalise_tty(tty)
    payload = f"\x1b]2;{_sanitize_title(title)}\x07".encode()
    try:
        with open(f"/dev/{safe_tty}", "wb", buffering=0) as stream:
            stream.write(payload)
    except OSError as exc:
        raise GhosttyError(f"无法向 {safe_tty} 写入临时标题: {exc}") from exc


def _write_cwd(tty: str, cwd: str) -> None:
    safe_tty = _normalise_tty(tty)
    clean = "".join(ch for ch in str(cwd) if unicodedata.category(ch) != "Cc")
    if not clean.startswith("/"):
        raise GhosttyError(f"无效的工作目录: {cwd!r}")
    payload = f"\x1b]7;file://localhost{quote(clean)}\x07".encode()
    try:
        with open(f"/dev/{safe_tty}", "wb", buffering=0) as stream:
            stream.write(payload)
    except OSError as exc:
        raise GhosttyError(f"无法向 {safe_tty} 写入临时目录标记: {exc}") from exc


def clear_mapping_cache() -> None:
    with _MAPPING_LOCK:
        _TTY_TO_TERMINAL_ID.clear()


def map_ttys(
    ttys: Iterable[str],
    *,
    fallback_titles: Mapping[str, str] | None = None,
    fallback_cwds: Mapping[str, str] | None = None,
    current_terminals: Iterable[GhosttyTerminal] | None = None,
) -> tuple[dict[str, GhosttyTerminal], dict[str, str]]:
    """用瞬时 OSC marker 把 HAPi child 的 TTY 关联到已枚举的 terminal。

    映射只做 enrichment。失败不会改变 Ghostty 标签 inventory。
    """
    safe_ttys = list(dict.fromkeys(_normalise_tty(tty) for tty in ttys if tty))
    fallbacks = {
        _normalise_tty(tty): _sanitize_title(title)
        for tty, title in (fallback_titles or {}).items()
        if tty
    }
    cwd_fallbacks = {
        _normalise_tty(tty): str(cwd)
        for tty, cwd in (fallback_cwds or {}).items()
        if tty and str(cwd).startswith("/")
    }
    with _MAPPING_LOCK:
        current = list(current_terminals) if current_terminals is not None else enumerate_terminals()
        by_id = {item.terminal_id: item for item in current}
        mapped: dict[str, GhosttyTerminal] = {}
        errors: dict[str, str] = {}
        pending: list[str] = []
        for tty in safe_ttys:
            terminal_id = _TTY_TO_TERMINAL_ID.get(tty)
            if terminal_id and terminal_id in by_id:
                mapped[tty] = by_id[terminal_id]
            else:
                _TTY_TO_TERMINAL_ID.pop(tty, None)
                pending.append(tty)
        if not pending:
            return mapped, errors

        markers = {tty: _MARK_PREFIX + uuid.uuid4().hex[:12] for tty in pending}
        marker_to_tty = {marker: tty for tty, marker in markers.items()}
        wrote: list[str] = []
        matched: dict[str, GhosttyTerminal] = {}
        before_titles = {item.terminal_id: item.title for item in current}
        try:
            for tty in pending:
                try:
                    _write_title(tty, markers[tty])
                    wrote.append(tty)
                except GhosttyError as exc:
                    errors[tty] = str(exc)
            for attempt in range(4):
                if attempt:
                    for tty in wrote:
                        if tty not in matched:
                            try:
                                _write_title(tty, markers[tty])
                            except GhosttyError:
                                pass
                    time.sleep(0.12)
                for item in enumerate_terminals():
                    tty = marker_to_tty.get(item.title)
                    if tty:
                        matched[tty] = item
                if len(matched) == len(wrote):
                    break
        finally:
            for tty in wrote:
                item = matched.get(tty)
                if item is None:
                    # 先保留 marker，下一步可能靠 cwd marker 找回 terminal 后精确恢复。
                    continue
                restore = before_titles.get(item.terminal_id, "")
                try:
                    _write_title(tty, restore)
                except GhosttyError:
                    pass

        unresolved = [
            tty for tty in pending if tty not in matched and tty in cwd_fallbacks
        ]
        cwd_markers = {
            tty: f"/tmp/BKCWD-{uuid.uuid4().hex[:12]}" for tty in unresolved
        }
        cwd_marker_to_tty = {marker: tty for tty, marker in cwd_markers.items()}
        cwd_wrote: list[str] = []
        try:
            for tty in unresolved:
                try:
                    _write_cwd(tty, cwd_markers[tty])
                    cwd_wrote.append(tty)
                except GhosttyError as exc:
                    errors[tty] = str(exc)
            for attempt in range(3):
                if attempt:
                    time.sleep(0.10)
                for item in enumerate_terminals():
                    tty = cwd_marker_to_tty.get(item.cwd)
                    if tty:
                        matched[tty] = item
                if all(tty in matched for tty in cwd_wrote):
                    break
        finally:
            for tty in cwd_wrote:
                try:
                    _write_cwd(tty, cwd_fallbacks[tty])
                except GhosttyError:
                    pass

        for tty in wrote:
            item = matched.get(tty)
            if item is not None:
                try:
                    _write_title(tty, before_titles.get(item.terminal_id, ""))
                except GhosttyError:
                    pass
            elif tty in fallbacks:
                try:
                    _write_title(tty, fallbacks[tty])
                except GhosttyError:
                    pass

        for tty, item in matched.items():
            restored = by_id.get(item.terminal_id, item)
            mapped[tty] = restored
            _TTY_TO_TERMINAL_ID[tty] = item.terminal_id
        for tty in pending:
            if tty not in mapped and tty not in errors:
                errors[tty] = "没有关联到可见 Ghostty terminal"
        return mapped, errors
