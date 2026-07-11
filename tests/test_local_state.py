from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bkanban import local_state
from bkanban.models import SessionState


class CodexNativeStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "state_5.sqlite"
        self.rollout = self.root / "rollout.jsonl"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)")
        connection.execute(
            "INSERT INTO threads (id, rollout_path) VALUES (?, ?)",
            ("codex-1", str(self.rollout)),
        )
        connection.commit()
        connection.close()
        local_state._path_cache.clear()
        local_state._state_cache.clear()
        self.database_patch = patch(
            "bkanban.local_state._database_paths", return_value=(self.database,)
        )
        self.database_patch.start()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.temp.cleanup()

    def write_signals(self, *signals: str) -> None:
        lines = [
            json.dumps({"type": "event_msg", "payload": {"type": signal}})
            for signal in signals
        ]
        self.rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def summary(self, session_id: str = "codex-1") -> dict:
        return {
            "metadata": {"flavor": "codex", "codexSessionId": session_id}
        }

    def test_latest_start_is_working(self) -> None:
        self.write_signals("task_complete", "task_started")
        self.assertEqual(
            local_state.local_agent_state(self.summary()), SessionState.WORKING
        )

    def test_latest_complete_or_abort_is_idle(self) -> None:
        for terminal_signal in ("task_complete", "turn_aborted"):
            with self.subTest(signal=terminal_signal):
                self.write_signals("task_started", terminal_signal)
                self.assertEqual(
                    local_state.local_agent_state(self.summary()), SessionState.IDLE
                )

    def test_file_change_invalidates_cached_state(self) -> None:
        self.write_signals("task_started")
        self.assertEqual(
            local_state.local_agent_state(self.summary()), SessionState.WORKING
        )
        with self.rollout.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"type": "event_msg", "payload": {"type": "task_complete"}}
                )
                + "\n"
            )
        self.assertEqual(local_state.local_agent_state(self.summary()), SessionState.IDLE)

    def test_missing_or_unsupported_session_returns_none(self) -> None:
        self.write_signals("task_started")
        self.assertIsNone(local_state.local_agent_state(self.summary("missing")))
        self.assertIsNone(
            local_state.local_agent_state(
                {"metadata": {"flavor": "claude", "codexSessionId": "codex-1"}}
            )
        )


class ClaudeNativeStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "claude.jsonl"
        local_state._state_cache.clear()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, *events: dict) -> None:
        self.path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )

    def test_external_prompt_without_end_turn_is_working(self) -> None:
        self.write(
            {"type": "user", "message": {"content": "请修复游戏"}},
            {"type": "assistant", "message": {"stop_reason": "tool_use"}},
            {"type": "user", "toolUseResult": True, "message": {"content": []}},
        )
        self.assertEqual(
            local_state._task_state_from_claude(self.path), SessionState.WORKING
        )

    def test_end_turn_is_idle_and_tool_result_is_not_new_prompt(self) -> None:
        self.write(
            {"type": "user", "message": {"content": "请分析"}},
            {"type": "assistant", "message": {"stop_reason": "tool_use"}},
            {"type": "user", "toolUseResult": True, "message": {"content": []}},
            {"type": "assistant", "message": {"stop_reason": "end_turn"}},
        )
        self.assertEqual(
            local_state._task_state_from_claude(self.path), SessionState.IDLE
        )

    def test_content_containing_tool_result_is_internal(self) -> None:
        event = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "content": "done"},
                    {"type": "text", "text": "internal wrapper"},
                ]
            },
        }
        self.assertFalse(local_state._real_claude_user_event(event))


if __name__ == "__main__":
    unittest.main()
