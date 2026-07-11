from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from bkanban import ghostty
from bkanban.models import GhosttyTerminal


def terminal(
    *,
    tab_id: str = "tab-1",
    tab_index: int = 1,
    terminal_id: str = "terminal-1",
    tab_title: str = "任务一",
    focused: bool = True,
) -> GhosttyTerminal:
    return GhosttyTerminal(
        window_id="window-1",
        window_index=1,
        tab_id=tab_id,
        tab_index=tab_index,
        tab_title=tab_title,
        terminal_id=terminal_id,
        title=tab_title,
        cwd="/tmp/project",
        selected=tab_index == 1,
        focused=focused,
    )


class InventoryTests(unittest.TestCase):
    def test_parse_includes_tab_title_and_focused_terminal(self) -> None:
        raw = ghostty._FIELD_SEP.join(
            [
                "window-1",
                "1",
                "tab-1",
                "2",
                "会话标签",
                "terminal-1",
                "终端标题",
                "/tmp/project",
                "true",
                "terminal-1",
            ]
        )
        with patch.object(ghostty, "_osascript", return_value=raw):
            found = ghostty.enumerate_terminals()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].tab_title, "会话标签")
        self.assertTrue(found[0].focused)

    def test_group_tabs_is_one_row_per_tab_and_keeps_splits(self) -> None:
        terminals = [
            terminal(terminal_id="left", focused=False),
            terminal(terminal_id="right", focused=True),
            terminal(tab_id="tab-2", tab_index=2, terminal_id="other"),
        ]
        tabs = ghostty.group_tabs(terminals)
        self.assertEqual(len(tabs), 2)
        self.assertEqual(tabs[0].focused_terminal_id, "right")
        self.assertEqual(len(tabs[0].terminals), 2)


class FocusTests(unittest.TestCase):
    def test_focus_selects_exact_id_then_scrolls_to_bottom(self) -> None:
        with patch.object(ghostty, "_osascript", return_value="ok") as osa:
            scrolled = ghostty.focus_terminal_at_bottom('terminal-"1')
        self.assertTrue(scrolled)
        script = osa.call_args.args[0]
        self.assertIn('terminal-\\"1', script)
        self.assertIn("select tab t", script)
        self.assertIn('perform action "scroll_to_bottom" on s', script)

    def test_focus_only_is_reported_when_scroll_action_is_unconfirmed(self) -> None:
        with patch.object(ghostty, "_osascript", return_value="focused"):
            self.assertFalse(ghostty.focus_terminal_at_bottom("terminal-1"))


class CloseTests(unittest.TestCase):
    def test_close_tab_uses_exact_stable_id(self) -> None:
        with patch.object(ghostty, "_osascript", return_value="ok") as osa:
            ghostty.close_tab('tab-"1')
        script = osa.call_args.args[0]
        self.assertIn('tab-\\"1', script)
        self.assertIn("if id of t is", script)
        self.assertIn("close tab t", script)

    def test_missing_tab_is_not_reported_as_closed(self) -> None:
        with patch.object(ghostty, "_osascript", return_value="notfound"):
            with self.assertRaisesRegex(ghostty.GhosttyError, "已不存在"):
                ghostty.close_tab("missing")

    def test_bulk_close_freezes_and_deduplicates_exact_ids(self) -> None:
        with patch.object(ghostty, "_osascript", return_value="2") as osa:
            closed = ghostty.close_tabs(["tab-1", "tab-2", "tab-1"])
        self.assertEqual(closed, 2)
        script = osa.call_args.args[0]
        self.assertEqual(script.count('"tab-1"'), 1)
        self.assertEqual(script.count('"tab-2"'), 1)
        self.assertNotIn("index of t", script)


class MappingTests(unittest.TestCase):
    def setUp(self) -> None:
        ghostty.clear_mapping_cache()

    def test_invalid_tty_never_reaches_device_write(self) -> None:
        with patch.object(ghostty, "_write_title") as write:
            with self.assertRaisesRegex(ghostty.GhosttyError, "无效的 TTY"):
                ghostty.map_ttys(["../../etc/passwd"], current_terminals=[])
        write.assert_not_called()

    def test_dynamic_title_falls_back_to_cwd_marker_and_restores_both(self) -> None:
        current = terminal()
        title_uuid = type("FakeUUID", (), {"hex": "aaaaaaaaaaaa0000"})()
        cwd_uuid = type("FakeUUID", (), {"hex": "bbbbbbbbbbbb0000"})()
        title_overwritten = replace(current, title="spinner")
        cwd_marked = replace(current, cwd="/tmp/BKCWD-bbbbbbbbbbbb")
        with (
            patch.object(ghostty.uuid, "uuid4", side_effect=[title_uuid, cwd_uuid]),
            patch.object(ghostty, "_write_title") as write_title,
            patch.object(ghostty, "_write_cwd") as write_cwd,
            patch.object(
                ghostty,
                "enumerate_terminals",
                side_effect=[
                    [title_overwritten],
                    [title_overwritten],
                    [title_overwritten],
                    [title_overwritten],
                    [cwd_marked],
                ],
            ),
            patch.object(ghostty.time, "sleep"),
        ):
            mapped, errors = ghostty.map_ttys(
                ["ttys001"],
                fallback_titles={"ttys001": "HAPi session"},
                fallback_cwds={"ttys001": "/tmp/project"},
                current_terminals=[current],
            )
        self.assertEqual(errors, {})
        self.assertEqual(mapped["ttys001"].terminal_id, "terminal-1")
        self.assertEqual(write_cwd.call_args_list[-1].args, ("ttys001", "/tmp/project"))
        self.assertEqual(write_title.call_args_list[-1].args, ("ttys001", "任务一"))


if __name__ == "__main__":
    unittest.main()
