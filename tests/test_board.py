from __future__ import annotations

import unittest

from textual.widgets import DataTable

from bkanban.board import (
    ConfirmCloseScreen,
    DetailScreen,
    NotebookBoardApp,
    PROMPT_CHARS,
    _clip,
    _head_tail,
)
from bkanban.models import (
    BoardRow,
    BoardSnapshot,
    ConversationRound,
    GhosttyTab,
    GhosttyTerminal,
    HapiSession,
    SessionState,
)


def snapshot() -> BoardSnapshot:
    terminal = GhosttyTerminal(
        window_id="window-1",
        window_index=1,
        tab_id="tab-1",
        tab_index=1,
        tab_title="会话聚合看板",
        terminal_id="terminal-1",
        title="会话聚合看板",
        cwd="/tmp/project",
        selected=True,
        focused=True,
    )
    tab = GhosttyTab(
        window_id="window-1",
        window_index=1,
        tab_id="tab-1",
        tab_index=1,
        title="会话聚合看板",
        selected=True,
        focused_terminal_id="terminal-1",
        terminals=(terminal,),
    )
    session = HapiSession(
        session_id="session-1",
        pid=99_999_999,
        tty="ttys001",
        terminal_id="terminal-1",
        name="重做笔记本会话聚合看板",
        flavor="codex",
        state=SessionState.WORKING,
        state_detail="Agent 处理中",
        first_prompt="请以 Ghostty 标签页为集合事实源，HAPi 只补充状态和问答",
        title_loaded=True,
        history=[ConversationRound(user="请重做这个看板", assistant="已完成核心架构与测试。")],
    )
    return BoardSnapshot(rows=[BoardRow(tab=tab, sessions=[session])])


class BoardUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_enters_open_detail_then_connect_same_stable_terminal(self) -> None:
        focus_calls: list[str] = []
        app = NotebookBoardApp(
            snapshot_loader=snapshot,
            focus_action=lambda terminal_id: focus_calls.append(terminal_id) or True,
            history_loader=lambda session: None,
        )
        app._autostart = False
        async with app.run_test(size=(140, 40)) as pilot:
            app._apply_snapshot(snapshot())
            table = app.query_one("#sessions", DataTable)
            self.assertEqual(table.row_count, 1)
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(focus_calls, ["terminal-1"])

    async def test_action_cell_and_x_open_confirmation_before_close(self) -> None:
        close_calls: list[str] = []
        lifecycle: list[str] = []
        app = NotebookBoardApp(
            snapshot_loader=snapshot,
            close_tab_action=lambda tab_id: (
                lifecycle.append("close-tab"), close_calls.append(tab_id)
            ),
            stop_session_action=lambda _: lifecycle.append("stop-hapi") or True,
            history_loader=lambda session: None,
        )
        app._autostart = False
        async with app.run_test(size=(140, 40)) as pilot:
            app._apply_snapshot(snapshot())
            table = app.query_one("#sessions", DataTable)
            table.move_cursor(row=0, column=6)
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmCloseScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(close_calls, [])

            table.focus()
            await pilot.press("x")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmCloseScreen)
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(close_calls, ["tab-1"])
            self.assertEqual(lifecycle, ["stop-hapi", "close-tab"])

    async def test_clear_sessions_freezes_visible_hapi_tab_ids(self) -> None:
        close_many_calls: list[list[str]] = []
        app = NotebookBoardApp(
            snapshot_loader=snapshot,
            close_tabs_action=lambda ids: close_many_calls.append(ids) or len(ids),
            stop_session_action=lambda _: True,
            history_loader=lambda session: None,
        )
        app._autostart = False
        async with app.run_test(size=(140, 40)) as pilot:
            app._apply_snapshot(snapshot())
            await pilot.press("ctrl+x")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmCloseScreen)
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(close_many_calls, [["tab-1"]])

    def test_prompt_defaults_to_20_to_30_visible_characters(self) -> None:
        self.assertGreaterEqual(PROMPT_CHARS, 20)
        self.assertLessEqual(PROMPT_CHARS, 30)
        self.assertEqual(len(_clip("很长" * 30, PROMPT_CHARS)), PROMPT_CHARS)

    def test_long_conclusion_keeps_context_without_filling_detail(self) -> None:
        value = "开头" + "中间" * 500 + "结尾"
        clipped = _head_tail(value)
        self.assertIn("开头", clipped)
        self.assertIn("结尾", clipped)
        self.assertIn("中间内容已省略", clipped)
        self.assertLess(len(clipped), len(value))


if __name__ == "__main__":
    unittest.main()
