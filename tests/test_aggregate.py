from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bkanban.aggregate import collect_snapshot, hydrate_prompts
from bkanban.hapi_client import HapiError, PromptCache
from bkanban.models import GhosttyTerminal, HapiChild, SessionState


def terminal(
    tab_id: str,
    tab_index: int,
    terminal_id: str,
    *,
    focused: bool = True,
) -> GhosttyTerminal:
    return GhosttyTerminal(
        window_id="window-1",
        window_index=1,
        tab_id=tab_id,
        tab_index=tab_index,
        tab_title=f"标签 {tab_index}",
        terminal_id=terminal_id,
        title=f"终端 {tab_index}",
        cwd=f"/tmp/project-{tab_index}",
        selected=tab_index == 1,
        focused=focused,
    )


class FakeService:
    def __init__(self, messages: list[dict] | None = None) -> None:
        self.messages = messages or []

    def all_messages(self, session_id: str) -> list[dict]:
        return self.messages


class SelectiveMessageService:
    def all_messages(self, session_id: str) -> list[dict]:
        if session_id == "sid-bad":
            raise HapiError("单个 Session 尚未同步")
        return [
            {
                "seq": 1,
                "content": {
                    "role": "user",
                    "content": {"type": "text", "text": "第二个 Session 的 Prompt"},
                },
            }
        ]


class FactSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.terminals = [terminal("tab-1", 1, "term-1"), terminal("tab-2", 2, "term-2")]
        self.temp = tempfile.TemporaryDirectory()
        self.cache = PromptCache(Path(self.temp.name) / "prompts.json")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_zero_hapi_sessions_still_keeps_every_ghostty_tab(self) -> None:
        with patch("bkanban.aggregate.ghostty.map_ttys", return_value=({}, {})):
            snapshot = collect_snapshot(
                prompt_cache=self.cache,
                list_terminals=lambda: self.terminals,
                list_children=lambda: [],
                list_sessions=lambda: [],
            )
        self.assertEqual([row.key for row in snapshot.rows], ["tab-1", "tab-2"])
        self.assertTrue(all(row.state == SessionState.TERMINAL for row in snapshot.rows))

    def test_hapi_only_enriches_existing_tabs_and_never_creates_rows(self) -> None:
        children = [
            HapiChild("sid-1", 10, tty="ttys001"),
            HapiChild("sid-extra", 11, tty="ttys999"),
        ]
        summary = {
            "id": "sid-1",
            "active": True,
            "thinking": True,
            "metadata": {"name": "修复首页", "flavor": "codex"},
        }
        with patch(
            "bkanban.aggregate.ghostty.map_ttys",
            return_value=(
                {"ttys001": self.terminals[0]},
                {"ttys999": "没有对应标签"},
            ),
        ):
            snapshot = collect_snapshot(
                prompt_cache=self.cache,
                list_terminals=lambda: self.terminals,
                list_children=lambda: children,
                list_sessions=lambda: [summary, {"id": "hub-only"}],
            )
        self.assertEqual(len(snapshot.rows), 2)
        self.assertEqual(snapshot.rows[0].session_name, "标签 1")
        self.assertEqual(snapshot.rows[0].state, SessionState.WORKING)
        self.assertFalse(snapshot.rows[1].sessions)

    def test_native_state_enriches_the_mapped_ghostty_row(self) -> None:
        child = HapiChild("sid-1", 10, tty="ttys001")
        summary = {
            "id": "sid-1",
            "active": True,
            "thinking": False,
            "metadata": {
                "flavor": "codex",
                "codexSessionId": "native-codex-1",
            },
        }
        with patch(
            "bkanban.aggregate.ghostty.map_ttys",
            return_value=({"ttys001": self.terminals[0]}, {}),
        ):
            snapshot = collect_snapshot(
                prompt_cache=self.cache,
                list_terminals=lambda: self.terminals,
                list_children=lambda: [child],
                list_sessions=lambda: [summary],
                native_state_resolver=lambda _: SessionState.WORKING,
            )
        self.assertEqual(snapshot.rows[0].state, SessionState.WORKING)
        self.assertEqual(snapshot.rows[0].primary.status_text, "Agent 处理中")

    def test_prompt_hydration_only_reads_visible_mapped_session(self) -> None:
        child = HapiChild("sid-1", 10, tty="ttys001")
        summary = {
            "id": "sid-1",
            "active": True,
            "metadata": {"name": "任务", "flavor": "claude"},
        }
        with patch(
            "bkanban.aggregate.ghostty.map_ttys",
            return_value=({"ttys001": self.terminals[0]}, {}),
        ):
            snapshot = collect_snapshot(
                prompt_cache=self.cache,
                list_terminals=lambda: self.terminals,
                list_children=lambda: [child],
                list_sessions=lambda: [summary],
            )
        messages = [
            {
                "seq": 1,
                "content": {
                    "role": "user",
                    "content": {"type": "text", "text": "这是首条 Prompt"},
                },
            }
        ]
        errors = hydrate_prompts(
            snapshot,
            service=FakeService(messages),
            prompt_cache=self.cache,
        )
        self.assertEqual(errors, [])
        self.assertEqual(snapshot.rows[0].first_prompt, "这是首条 Prompt")
        self.assertEqual(self.cache.get("sid-1"), "这是首条 Prompt")
        self.assertEqual(snapshot.rows[0].session_name, "标签 1")

    def test_one_message_failure_does_not_block_later_sessions(self) -> None:
        children = [
            HapiChild("sid-bad", 10, tty="ttys001"),
            HapiChild("sid-good", 11, tty="ttys002"),
        ]
        summaries = [
            {"id": "sid-bad", "active": True, "metadata": {"flavor": "codex"}},
            {"id": "sid-good", "active": True, "metadata": {"flavor": "claude"}},
        ]
        with patch(
            "bkanban.aggregate.ghostty.map_ttys",
            return_value=(
                {"ttys001": self.terminals[0], "ttys002": self.terminals[1]},
                {},
            ),
        ):
            snapshot = collect_snapshot(
                prompt_cache=self.cache,
                list_terminals=lambda: self.terminals,
                list_children=lambda: children,
                list_sessions=lambda: summaries,
            )
        errors = hydrate_prompts(
            snapshot,
            service=SelectiveMessageService(),
            prompt_cache=self.cache,
        )
        self.assertEqual(len(errors), 1)
        self.assertFalse(snapshot.rows[0].primary.prompt_loaded)
        self.assertEqual(snapshot.rows[1].first_prompt, "第二个 Session 的 Prompt")


if __name__ == "__main__":
    unittest.main()
