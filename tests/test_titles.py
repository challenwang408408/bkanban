from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from bkanban.titles import TitleCache, _chat_text, generate_title


class TitleGeneratorTests(unittest.TestCase):
    def test_uses_small_model_and_returns_only_clean_short_title(self) -> None:
        with patch(
            "bkanban.titles._chat_text",
            return_value="任务标题：优化 Session 中文标题。\n这里是解释",
        ) as chat:
            title = generate_title("请根据首次 Prompt 生成 Session 中文标题")
        self.assertEqual(title, "优化 Session 中文标题")
        self.assertEqual(chat.call_args.args[0], "gpt-4o-mini")
        self.assertIn("只输出标题本身", chat.call_args.args[1])

    def test_cache_is_keyed_by_session_and_prompt_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = TitleCache(Path(directory) / "titles.json")
            cache.put("sid", "第一次需求", "生成第一个标题")
            self.assertEqual(cache.get("sid", "第一次需求"), "生成第一个标题")
            self.assertEqual(cache.get("sid", "修改后的需求"), "")

    def test_title_cache_is_private_and_clearable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "titles.json"
            cache = TitleCache(path)
            cache.put("sid", "private prompt", "生成标题")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            cache.clear()
            self.assertFalse(path.exists())

    def test_openai_compatible_provider_is_explicitly_configured(self) -> None:
        response = unittest.mock.MagicMock()
        response.json.return_value = {
            "choices": [{"message": {"content": "生成标题"}}]
        }
        environment = {
            "BKANBAN_LLM_BASE_URL": "http://127.0.0.1:11434/v1",
            "BKANBAN_LLM_API_KEY": "test-key",
        }
        with (
            unittest.mock.patch.dict(os.environ, environment, clear=False),
            unittest.mock.patch("bkanban.titles.httpx.post", return_value=response) as post,
        ):
            self.assertEqual(_chat_text("small-model", "prompt"), "生成标题")
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer test-key"})


if __name__ == "__main__":
    unittest.main()
