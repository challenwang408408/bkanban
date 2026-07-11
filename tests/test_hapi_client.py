from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from bkanban.hapi_client import HapiError, HapiService, PromptCache, RunnerClient


class FakeHub:
    def __init__(self) -> None:
        self.fail_sessions = False

    def list_sessions(self) -> list[dict]:
        if self.fail_sessions:
            raise HapiError("sessions transient failure")
        return [{"id": "sid-1", "active": True}]

    def all_messages(self, session_id: str) -> list[dict]:
        if session_id == "bad":
            raise HapiError("message transient failure")
        return [{"seq": 1}]

    def close(self) -> None:
        pass


class HapiIsolationTests(unittest.TestCase):
    def test_message_failure_does_not_poison_session_summary_endpoint(self) -> None:
        service = HapiService()
        service._hub = FakeHub()
        with self.assertRaises(HapiError):
            service.all_messages("bad")
        self.assertEqual(service.list_sessions()[0]["id"], "sid-1")
        self.assertEqual(service.all_messages("good"), [{"seq": 1}])

    def test_transient_summary_failure_uses_last_good_snapshot(self) -> None:
        service = HapiService()
        hub = FakeHub()
        service._hub = hub
        self.assertEqual(len(service.list_sessions()), 1)
        hub.fail_sessions = True
        self.assertEqual(len(service.list_sessions()), 1)

    def test_stop_session_targets_only_the_requested_runner_child(self) -> None:
        response = MagicMock()
        response.json.return_value = {"success": True}
        with (
            patch("bkanban.hapi_client._runner_url", return_value="http://runner"),
            patch("bkanban.hapi_client.httpx.post", return_value=response) as post,
        ):
            self.assertTrue(RunnerClient().stop_session("sid-1"))
        post.assert_called_once_with(
            "http://runner/stop-session",
            json={"sessionId": "sid-1"},
            timeout=1.5,
        )
        response.raise_for_status.assert_called_once_with()

    def test_prompt_cache_is_private_and_clearable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "prompts.json"
            cache = PromptCache(path)
            cache.put("sid", "private prompt")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            cache.clear()
            self.assertFalse(path.exists())
            self.assertEqual(cache.get("sid"), "")


if __name__ == "__main__":
    unittest.main()
