from __future__ import annotations

import unittest

from bkanban.models import SessionState
from bkanban.sessions import conversation_rounds, first_user_prompt, state_from_summary


def message(seq: int, role: str, body: object) -> dict:
    return {
        "seq": seq,
        "createdAt": seq * 100,
        "content": {"role": role, "content": body},
    }


class StatusTests(unittest.TestCase):
    def test_status_priority_uses_explicit_hapi_signals(self) -> None:
        state, _ = state_from_summary(
            {
                "thinking": True,
                "backgroundTaskCount": 2,
                "pendingRequests": [
                    {"kind": "permission", "tool": "Bash"},
                    {"kind": "input", "tool": "AskUserQuestion"},
                ],
            }
        )
        self.assertEqual(state, SessionState.WAITING_INPUT)

    def test_active_without_work_signal_is_idle_not_working(self) -> None:
        state, text = state_from_summary({"active": True})
        self.assertEqual(state, SessionState.IDLE)
        self.assertEqual(text, "在线待命")

    def test_native_working_fills_hapi_false_negative(self) -> None:
        state, text = state_from_summary(
            {"active": True, "thinking": False}, SessionState.WORKING
        )
        self.assertEqual(state, SessionState.WORKING)
        self.assertEqual(text, "Agent 处理中")

    def test_hapi_explicit_waiting_signal_beats_native_state(self) -> None:
        state, _ = state_from_summary(
            {"pendingRequests": [{"kind": "input", "tool": "AskUserQuestion"}]},
            SessionState.WORKING,
        )
        self.assertEqual(state, SessionState.WAITING_INPUT)

    def test_hapi_thinking_beats_native_idle(self) -> None:
        state, _ = state_from_summary(
            {"thinking": True}, SessionState.IDLE
        )
        self.assertEqual(state, SessionState.WORKING)

    def test_agent_state_request_is_waiting_approval(self) -> None:
        state, text = state_from_summary(
            {
                "agentState": {
                    "requests": {"req-1": {"tool": "Bash", "createdAt": 100}}
                }
            }
        )
        self.assertEqual(state, SessionState.WAITING_APPROVAL)
        self.assertIn("Bash", text)


class ConversationTests(unittest.TestCase):
    def test_first_prompt_skips_command_wrapper(self) -> None:
        messages = [
            message(1, "user", {"type": "text", "text": "<command-name>/init"}),
            message(2, "user", {"type": "text", "text": "真正的首次需求"}),
        ]
        self.assertEqual(first_user_prompt(messages), "真正的首次需求")

    def test_round_keeps_only_last_agent_conclusion_and_skips_tools(self) -> None:
        messages = [
            message(1, "user", {"type": "text", "text": "请修复看板"}),
            message(2, "agent", {"data": {"type": "tool-call", "text": "中间过程"}}),
            message(
                3,
                "agent",
                {
                    "data": {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "初步结论"}]},
                    }
                },
            ),
            message(
                4,
                "agent",
                {"data": {"type": "agent_message", "text": "最终结论"}},
            ),
        ]
        rounds = conversation_rounds(messages)
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0].user, "请修复看板")
        self.assertEqual(rounds[0].assistant, "最终结论")
        self.assertNotIn("中间过程", rounds[0].assistant)


if __name__ == "__main__":
    unittest.main()
