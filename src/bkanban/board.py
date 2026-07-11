"""以 Ghostty 标签为事实源的 Textual 聚合看板。"""
from __future__ import annotations

import os
import time
from collections.abc import Callable

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, RichLog, Static

from . import ghostty
from .aggregate import collect_snapshot, hydrate_history, hydrate_prompts
from .hapi_client import HapiService, PromptCache
from .models import BoardRow, BoardSnapshot, HapiSession, SessionState, STATE_LABELS
from .titles import TitleCache, TitleGenerator, generate_title, hydrate_titles


REFRESH_SECONDS = 4
PROMPT_CHARS = 28


STATE_STYLE = {
    SessionState.WAITING_INPUT: ("!", "bold yellow"),
    SessionState.WAITING_APPROVAL: ("!", "bold yellow"),
    SessionState.WORKING: ("◆", "bold magenta"),
    SessionState.BACKGROUND: ("◇", "magenta"),
    SessionState.IDLE: ("●", "green"),
    SessionState.HUB_OFFLINE: ("◌", "yellow"),
    SessionState.TERMINAL: ("○", "dim"),
}


def _clip(value: str, width: int) -> str:
    clean = " ".join((value or "").split())
    return clean if len(clean) <= width else clean[: max(0, width - 1)] + "…"


def _head_tail(
    value: str,
    *,
    limit: int = 680,
    head: int = 500,
    tail: int = 120,
) -> str:
    clean = " ".join((value or "").split())
    if len(clean) <= limit:
        return clean
    return f"{clean[:head]}\n… 中间内容已省略 …\n{clean[-tail:]}"


def _directory(row: BoardRow) -> str:
    value = row.tab.cwd
    if not value:
        return "-"
    parts = [part for part in value.split("/") if part]
    return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]


def _status_rich(row: BoardRow) -> Text:
    icon, style = STATE_STYLE[row.state]
    label = row.primary.status_text if row.primary else STATE_LABELS[row.state]
    return Text(f"{icon} {_clip(label, 12)}", style=style)


class DetailScreen(ModalScreen[str | None]):
    CSS = """
    DetailScreen { align: center middle; }
    #detail-box { width: 92%; height: 88%; border: round $primary; background: $surface; }
    #detail-header { height: 3; padding: 0 2; background: $primary-background; content-align: left middle; }
    #detail-meta { height: 2; padding: 0 2; color: $text-muted; }
    #conversation { height: 1fr; padding: 1 2; scrollbar-size: 1 1; }
    #detail-actions { height: 3; padding: 0 2; align-horizontal: right; }
    #detail-actions Button { min-width: 18; margin-left: 1; }
    """
    BINDINGS = [
        Binding("enter", "goto", "连接标签", priority=True),
        Binding("escape", "close", "返回"),
        Binding("q", "close", "返回", show=False),
        Binding("g", "goto", "连接标签", show=False),
    ]

    def __init__(self, row: BoardRow) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-box"):
            yield Static(self.row.session_name, id="detail-header")
            yield Static(
                f"窗口 {self.row.tab.window_index} · 标签 {self.row.tab.tab_index}"
                f" · {_directory(self.row)} · {len(self.row.sessions)} 个 HAPi Session",
                id="detail-meta",
            )
            yield RichLog(id="conversation", wrap=True, markup=False)
            with Horizontal(id="detail-actions"):
                yield Button("返回", id="back")
                yield Button("↗ 连接并滚到底部", id="goto", variant="primary")

    def on_mount(self) -> None:
        log = self.query_one("#conversation", RichLog)
        if not self.row.sessions:
            log.write(Text("这个 Ghostty 标签没有关联到 HAPi Session。", style="dim"))
            log.write(Text("仍可直接连接并回到该标签底部。", style="dim"))
            return
        for session_index, session in enumerate(self.row.sessions, 1):
            if len(self.row.sessions) > 1:
                log.write(
                    Text(
                        f"Session {session_index} · {session.flavor} · {session.name}",
                        style="bold white on dark_blue",
                    )
                )
            if not session.history:
                log.write(Text(session.status_text, style="yellow"))
                log.write(Text("当前没有取到可展示的问答记录。", style="dim"))
                continue
            for round_index, round_ in enumerate(session.history, 1):
                log.write(Text(f"你 · 第 {round_index} 轮", style="bold cyan"))
                log.write(_head_tail(round_.user, limit=760, head=560, tail=140))
                log.write(Text("AI · 结论", style="bold magenta"))
                conclusion = round_.assistant or "该轮没有可提取的最终结论"
                log.write(_head_tail(conclusion))
                log.write(Text("─" * 38, style="dim"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("goto" if event.button.id == "goto" else None)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_goto(self) -> None:
        self.dismiss("goto")


class ConfirmCloseScreen(ModalScreen[bool]):
    CSS = """
    ConfirmCloseScreen { align: center middle; }
    #confirm-box { width: 68; height: auto; border: round $error; background: $surface; padding: 1 2; }
    #confirm-title { height: 2; color: $error; text-style: bold; }
    #confirm-message { height: auto; min-height: 3; margin-bottom: 1; }
    #confirm-actions { height: 3; align-horizontal: right; }
    #confirm-actions Button { min-width: 14; margin-left: 1; }
    """
    BINDINGS = [
        Binding("y", "confirm", "确认关闭"),
        Binding("n", "cancel", "取消"),
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消", show=False),
    ]

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self.confirm_title = title
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.confirm_title, id="confirm-title")
            yield Static(self.message, id="confirm-message")
            with Horizontal(id="confirm-actions"):
                yield Button("取消", id="cancel")
                yield Button("确认关闭", id="confirm", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


SnapshotLoader = Callable[[], BoardSnapshot]
FocusAction = Callable[[str], bool]
HistoryLoader = Callable[[HapiSession], None]
CloseTabAction = Callable[[str], None]
CloseTabsAction = Callable[[list[str]], int]
StopSessionAction = Callable[[str], bool]


class NotebookBoardApp(App[None]):
    TITLE = "笔记本会话聚合看板"
    CSS = """
    #topbar { height: 1; background: $primary-background; color: $text; padding: 0 1; }
    DataTable { height: 1fr; }
    #bottombar { height: 1; }
    #hintbar { width: 1fr; color: $text-muted; padding: 0 1; }
    #clear-sessions { width: 16; min-width: 16; height: 1; min-height: 1; border: none; padding: 0 1; }
    #refresh-indicator { width: 4; content-align: right middle; padding: 0 1; }
    """
    BINDINGS = [
        Binding("enter", "show_detail", "详情"),
        Binding("g", "focus_selected", "连接"),
        Binding("x", "close_selected", "关闭 Session"),
        Binding("ctrl+x", "clear_sessions", "清空 Session"),
        Binding("r", "refresh", "刷新"),
        Binding("q", "quit", "退出"),
    ]
    _autostart = True

    def __init__(
        self,
        *,
        service: HapiService | None = None,
        prompt_cache: PromptCache | None = None,
        title_cache: TitleCache | None = None,
        title_generator: TitleGenerator = generate_title,
        snapshot_loader: SnapshotLoader | None = None,
        focus_action: FocusAction = ghostty.focus_terminal_at_bottom,
        history_loader: HistoryLoader | None = None,
        close_tab_action: CloseTabAction = ghostty.close_tab,
        close_tabs_action: CloseTabsAction = ghostty.close_tabs,
        stop_session_action: StopSessionAction | None = None,
    ) -> None:
        super().__init__()
        self.service = service or HapiService()
        self.prompt_cache = prompt_cache or PromptCache()
        self.title_cache = title_cache or TitleCache()
        self.title_generator = title_generator
        self.snapshot_loader = snapshot_loader or (
            lambda: collect_snapshot(
                service=self.service,
                prompt_cache=self.prompt_cache,
                title_cache=self.title_cache,
            )
        )
        self.focus_action = focus_action
        self.history_loader = history_loader or (
            lambda session: hydrate_history(session, service=self.service)
        )
        self.close_tab_action = close_tab_action
        self.close_tabs_action = close_tabs_action
        self.stop_session_action = stop_session_action or self.service.stop_session
        self.snapshot = BoardSnapshot(rows=[])
        self.rows: dict[str, BoardRow] = {}
        self._error = ""

    def compose(self) -> ComposeResult:
        yield Static(" 笔记本会话聚合看板", id="topbar")
        yield DataTable(cursor_type="cell", zebra_stripes=False, id="sessions")
        with Horizontal(id="bottombar"):
            yield Static(" Enter/点击 详情 · x 关闭当前 · g 连接 · r 刷新", id="hintbar")
            yield Button("清空 Session", id="clear-sessions", variant="error")
            yield Static(Text("●", style="dim"), id="refresh-indicator")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sessions", DataTable)
        table.add_column("状态", width=15, key="state")
        table.add_column("Agent", width=11, key="agent")
        table.add_column("Session 名称", width=31, key="name")
        table.add_column("首次 Prompt", width=31, key="prompt")
        table.add_column("Ghostty 标签", width=15, key="tab")
        table.add_column("目录", width=24, key="cwd")
        table.add_column("操作", width=10, key="action")
        table.focus()
        if self._autostart:
            self.action_refresh()
            self.set_interval(REFRESH_SECONDS, self.action_refresh)

    def action_refresh(self) -> None:
        self.query_one("#refresh-indicator", Static).update(Text("●", style="yellow"))
        self._load_snapshot()

    @work(thread=True, exclusive=True, group="refresh")
    def _load_snapshot(self) -> None:
        try:
            snapshot = self.snapshot_loader()
        except Exception as exc:
            self.call_from_thread(self._apply_error, str(exc))
            return
        self.call_from_thread(self._apply_snapshot, snapshot)

    def _apply_error(self, message: str) -> None:
        self._error = message
        self.query_one("#refresh-indicator", Static).update(Text("●", style="bold red"))
        self._render_topbar()

    def _apply_snapshot(self, snapshot: BoardSnapshot) -> None:
        self.snapshot = snapshot
        self.rows = {row.key: row for row in snapshot.rows}
        self._error = ""
        self._render_table()
        self._render_topbar()
        self.query_one("#refresh-indicator", Static).update(Text("●", style="green"))
        needs_prompts = any(
            not session.prompt_loaded for row in snapshot.rows for session in row.sessions
        )
        if needs_prompts:
            self._hydrate_prompts(snapshot)
        elif any(
            session.first_prompt and not session.title_loaded
            for row in snapshot.rows
            for session in row.sessions
        ):
            self._hydrate_titles_worker(snapshot)

    @work(thread=True, exclusive=True, group="prompts")
    def _hydrate_prompts(self, snapshot: BoardSnapshot) -> None:
        errors = hydrate_prompts(
            snapshot,
            service=self.service,
            prompt_cache=self.prompt_cache,
        )
        self.call_from_thread(self._apply_prompt_results, snapshot, errors)

    def _apply_prompt_results(self, snapshot: BoardSnapshot, errors: list[str]) -> None:
        if snapshot is not self.snapshot:
            return
        self._render_table()
        if any(
            session.first_prompt and not session.title_loaded
            for row in snapshot.rows
            for session in row.sessions
        ):
            self._hydrate_titles_worker(snapshot)
        if errors:
            self.query_one("#hintbar", Static).update(
                " Enter/点击 详情 · g 连接并滚到底部 · HAPi 消息暂不可用"
            )

    @work(thread=True, exclusive=True, group="titles")
    def _hydrate_titles_worker(self, snapshot: BoardSnapshot) -> None:
        errors = hydrate_titles(
            snapshot,
            cache=self.title_cache,
            generator=self.title_generator,
        )
        self.call_from_thread(self._apply_title_results, snapshot, errors)

    def _apply_title_results(self, snapshot: BoardSnapshot, errors: list[str]) -> None:
        if snapshot is not self.snapshot:
            return
        self._render_table()
        if errors:
            self.query_one("#hintbar", Static).update(
                " Enter/点击 详情 · g 连接并滚到底部 · 标题模型暂不可用"
            )

    def _render_table(self) -> None:
        table = self.query_one("#sessions", DataTable)
        previous = ""
        previous_column = table.cursor_column
        if table.row_count:
            try:
                previous = str(
                    table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                )
            except Exception:
                pass
        table.clear()
        keys: list[str] = []
        for row in self.snapshot.rows:
            primary = row.primary
            agent = primary.flavor if primary else "-"
            tab_label = f"W{row.tab.window_index} · T{row.tab.tab_index}"
            table.add_row(
                _status_rich(row),
                Text(_clip(agent, 9), style="cyan" if primary else "dim"),
                Text(_clip(row.session_name, 29), style="bold" if primary else "dim"),
                Text(_clip(row.first_prompt, PROMPT_CHARS), style="" if primary else "dim"),
                Text(tab_label, style="green" if row.tab.selected else "dim"),
                Text(_clip(_directory(row), 22), style="dim"),
                Text(" [关闭] ", style="bold white on dark_red")
                if row.sessions
                else Text("-", style="dim"),
                key=row.key,
            )
            keys.append(row.key)
        if previous and previous in keys:
            table.move_cursor(
                row=keys.index(previous),
                column=min(previous_column, max(0, len(table.columns) - 1)),
            )

    def _render_topbar(self) -> None:
        rows = self.snapshot.rows
        hapi_tabs = sum(bool(row.sessions) for row in rows)
        waiting = sum(
            row.state in (SessionState.WAITING_INPUT, SessionState.WAITING_APPROVAL)
            for row in rows
        )
        working = sum(row.state == SessionState.WORKING for row in rows)
        bar = Text(f" 笔记本会话聚合看板 · Ghostty {len(rows)} tabs · HAPi {hapi_tabs}")
        if working:
            bar.append(f" · {working} 处理中", style="bold magenta")
        if waiting:
            bar.append(f" · {waiting} 待介入", style="bold yellow")
        if self._error:
            bar.append(f" · {_clip(self._error, 80)}", style="bold red")
        self.query_one("#topbar", Static).update(bar)

        warning_count = len(self.snapshot.warnings)
        hint = " Enter/点击 详情 · x 关闭 · Ctrl+X 清空 · g 连接 · r 刷新 · q 退出"
        if warning_count:
            hint += f" · {warning_count} 个 enrichment 降级"
        self.query_one("#hintbar", Static).update(hint)

    def _selected_row(self) -> BoardRow | None:
        table = self.query_one("#sessions", DataTable)
        if not table.row_count:
            return None
        try:
            key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        except Exception:
            return None
        return self.rows.get(key)

    def action_focus_selected(self) -> None:
        row = self._selected_row()
        if row:
            self._focus(row.target_terminal_id)

    def action_show_detail(self) -> None:
        row = self._selected_row()
        if not row:
            return
        table = self.query_one("#sessions", DataTable)
        try:
            column = str(
                table.coordinate_to_cell_key(table.cursor_coordinate).column_key.value
            )
        except Exception:
            column = ""
        if column == "action" and row.sessions:
            self._request_close(row)
        else:
            self._load_detail(row)

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        row = self.rows.get(str(event.cell_key.row_key.value))
        if not row:
            return
        if str(event.cell_key.column_key.value) == "action" and row.sessions:
            self._request_close(row)
        else:
            self._load_detail(row)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "clear-sessions":
            self.action_clear_sessions()

    def action_close_selected(self) -> None:
        row = self._selected_row()
        if row and row.sessions:
            self._request_close(row)

    def _request_close(self, row: BoardRow) -> None:
        state_warning = (
            "\n该 Session 仍在处理，关闭会终止当前任务。"
            if row.state == SessionState.WORKING
            else ""
        )
        message = f"即将关闭 Ghostty 标签：{row.session_name}{state_warning}"
        self.push_screen(
            ConfirmCloseScreen("关闭这个 Session？", message),
            lambda confirmed: self._close_one(row) if confirmed else None,
        )

    def action_clear_sessions(self) -> None:
        targets = [row for row in self.snapshot.rows if row.sessions]
        if not targets:
            self.notify("当前没有可关闭的 HAPi Session", severity="warning")
            return
        names = "、".join(_clip(row.session_name, 16) for row in targets[:4])
        suffix = f"等 {len(targets)} 个" if len(targets) > 4 else f"，共 {len(targets)} 个"
        message = (
            f"将关闭 {names}{suffix} Session 标签。\n"
            "普通 Ghostty 标签和看板自身会保留；正在执行的任务会被终止。"
        )
        frozen_targets = [
            (
                row.tab.tab_id,
                [session.session_id for session in row.sessions],
                [session.pid for session in row.sessions],
            )
            for row in targets
        ]
        self.push_screen(
            ConfirmCloseScreen("清空所有 Session？", message),
            lambda confirmed: self._close_many(frozen_targets) if confirmed else None,
        )

    def _stop_sessions(self, session_ids: list[str]) -> int:
        failures = 0
        for session_id in dict.fromkeys(session_ids):
            try:
                if not self.stop_session_action(session_id):
                    failures += 1
            except Exception:
                failures += 1
        return failures

    @staticmethod
    def _wait_for_pids(pids: list[int], *, timeout: float = 2.0) -> None:
        """给 HAPi 一个短暂的 flush/退出窗口；不主动 kill，之后由 Ghostty 关闭 PTY。"""
        remaining = {pid for pid in pids if pid > 0}
        deadline = time.monotonic() + timeout
        while remaining and time.monotonic() < deadline:
            exited: set[int] = set()
            for pid in remaining:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    exited.add(pid)
                except (PermissionError, OSError):
                    continue
            remaining -= exited
            if remaining:
                time.sleep(0.05)

    @work(thread=True, exclusive=True, group="close")
    def _close_one(self, row: BoardRow) -> None:
        stop_failures = self._stop_sessions(
            [session.session_id for session in row.sessions]
        )
        self._wait_for_pids([session.pid for session in row.sessions])
        try:
            self.close_tab_action(row.tab.tab_id)
        except Exception as exc:
            self.call_from_thread(self.notify, f"关闭失败: {exc}", severity="error")
            return
        self.call_from_thread(self.notify, f"已关闭：{row.session_name}")
        if stop_failures:
            self.call_from_thread(
                self.notify,
                "Ghostty 已关闭，但 HAPi 清理状态未确认",
                severity="warning",
            )
        self.call_from_thread(self.action_refresh)

    @work(thread=True, exclusive=True, group="close")
    def _close_many(
        self, frozen_targets: list[tuple[str, list[str], list[int]]]
    ) -> None:
        stop_failures = self._stop_sessions(
            [
                session_id
                for _, session_ids, _ in frozen_targets
                for session_id in session_ids
            ]
        )
        frozen_pids = [pid for _, _, pids in frozen_targets for pid in pids]
        self._wait_for_pids(frozen_pids)
        frozen_ids = [tab_id for tab_id, _, _ in frozen_targets]
        try:
            closed = self.close_tabs_action(frozen_ids)
        except Exception as exc:
            self.call_from_thread(self.notify, f"清空 Session 失败: {exc}", severity="error")
            return
        self.call_from_thread(self.notify, f"已关闭 {closed} 个 Session 标签")
        if stop_failures:
            self.call_from_thread(
                self.notify,
                f"{stop_failures} 个 HAPi Session 的清理状态未确认",
                severity="warning",
            )
        self.call_from_thread(self.action_refresh)

    @work(thread=True, exclusive=True, group="detail")
    def _load_detail(self, row: BoardRow) -> None:
        for session in row.sessions:
            try:
                self.history_loader(session)
            except Exception:
                continue
        self.call_from_thread(self._show_detail, row)

    def _show_detail(self, row: BoardRow) -> None:
        def resolved(action: str | None) -> None:
            if action == "goto":
                self._focus(row.target_terminal_id)

        self.push_screen(DetailScreen(row), resolved)

    @work(thread=True, exclusive=True, group="focus")
    def _focus(self, terminal_id: str) -> None:
        try:
            scrolled = self.focus_action(terminal_id)
        except Exception as exc:
            self.call_from_thread(
                self.notify,
                f"连接 Ghostty 失败: {exc}",
                severity="error",
            )
            return
        if not scrolled:
            self.call_from_thread(
                self.notify,
                "已连接标签，但 Ghostty 没有确认滚动到底部",
                severity="warning",
            )

    def on_unmount(self) -> None:
        self.service.close()


BoardApp = NotebookBoardApp
