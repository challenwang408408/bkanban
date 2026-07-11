"""用 OpenAI-compatible 小模型从首次 Prompt 生成展示标题。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

from .models import BoardSnapshot, HapiSession


DEFAULT_TITLE_MODEL = "gpt-4o-mini"
TITLE_MAX_CHARS = 20


def _model_name() -> str:
    return os.environ.get("BKANBAN_TITLE_MODEL", DEFAULT_TITLE_MODEL)


def _chat_text(model: str, prompt: str) -> str:
    base_url = (
        os.environ.get("BKANBAN_LLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    api_key = os.environ.get("BKANBAN_LLM_API_KEY") or os.environ.get(
        "OPENAI_API_KEY"
    )
    if not api_key and base_url.startswith("https://api.openai.com"):
        raise RuntimeError("未配置 BKANBAN_LLM_API_KEY 或 OPENAI_API_KEY")
    endpoint = (
        base_url
        if base_url.endswith("/chat/completions")
        else f"{base_url}/chat/completions"
    )
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = httpx.post(
            endpoint,
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 48,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"标题模型返回 HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"标题模型网络失败: {type(exc).__name__}") from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("标题模型返回了无效响应") from exc
    return str(content or "")


def _clean_model_title(value: str) -> str:
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    if not lines:
        return ""
    title = lines[0]
    title = re.sub(r"^(?:标题|任务标题)\s*[:：]\s*", "", title)
    title = title.strip("#*`“”\"'《》【】[] ")
    title = title.rstrip("。；;，,")
    return title[:TITLE_MAX_CHARS]


def generate_title(first_prompt: str) -> str:
    request = f"""你是一个任务标题生成器。根据用户的首次 Prompt，生成一个简短中文任务标题。

要求：
1. 直接概括用户真正想完成的任务，不复述背景。
2. 以中文动词开头，专有名词可保留英文。
3. 控制在 8 到 16 个汉字左右，最多 20 个字符。
4. 不要使用引号、句号、编号，不要解释。
5. 只输出标题本身。

首次 Prompt：
{first_prompt[:1200]}"""
    title = _clean_model_title(_chat_text(_model_name(), request))
    if not title:
        raise RuntimeError("标题模型返回了空标题")
    return title


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:20]


class TitleCache:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (
            Path.home()
            / ".local"
            / "state"
            / "notebook-hapi-board"
            / "titles.json"
        )
        self._loaded = False
        self._values: dict[str, dict[str, str]] = {}
        self._retry_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                self._values = {
                    str(key): item
                    for key, item in value.items()
                    if isinstance(item, dict)
                }
        except (OSError, json.JSONDecodeError):
            pass
        self._loaded = True

    def get(self, session_id: str, prompt: str) -> str:
        if not prompt:
            return ""
        with self._lock:
            self._load()
            item = self._values.get(session_id) or {}
            if (
                item.get("promptHash") == _prompt_hash(prompt)
                and item.get("title")
            ):
                return str(item.get("title") or "")
            return ""

    def put(self, session_id: str, prompt: str, title: str) -> None:
        with self._lock:
            self._load()
            self._values[session_id] = {
                "promptHash": _prompt_hash(prompt),
                "model": _model_name(),
                "title": title,
            }
            self._retry_at.pop(f"{session_id}:{_prompt_hash(prompt)}", None)
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self._values, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
            os.chmod(self.path, 0o600)

    def clear(self) -> None:
        with self._lock:
            self._values = {}
            self._retry_at = {}
            self._loaded = True
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def can_attempt(self, session_id: str, prompt: str) -> bool:
        key = f"{session_id}:{_prompt_hash(prompt)}"
        with self._lock:
            return time.monotonic() >= self._retry_at.get(key, 0)

    def mark_failure(self, session_id: str, prompt: str, *, retry_seconds: int = 60) -> None:
        key = f"{session_id}:{_prompt_hash(prompt)}"
        with self._lock:
            self._retry_at[key] = time.monotonic() + retry_seconds


TitleGenerator = Callable[[str], str]


def hydrate_titles(
    snapshot: BoardSnapshot,
    *,
    cache: TitleCache,
    generator: TitleGenerator = generate_title,
) -> list[str]:
    errors: list[str] = []
    pending: list[HapiSession] = []
    seen: set[str] = set()
    for row in snapshot.rows:
        for session in row.sessions:
            if session.session_id in seen:
                continue
            seen.add(session.session_id)
            if not session.first_prompt:
                session.name = "等待首次 Prompt"
                continue
            cached = cache.get(session.session_id, session.first_prompt)
            if cached:
                session.name = cached
                session.title_loaded = True
                continue
            if session.title_loaded:
                continue
            if cache.can_attempt(session.session_id, session.first_prompt):
                pending.append(session)
            else:
                session.name = "标题生成失败，稍后重试"

    if not pending:
        return errors

    with ThreadPoolExecutor(max_workers=min(3, len(pending))) as executor:
        futures = {executor.submit(generator, session.first_prompt): session for session in pending}
        for future in as_completed(futures):
            session = futures[future]
            try:
                title = _clean_model_title(future.result())
                if not title:
                    raise RuntimeError("标题为空")
                cache.put(session.session_id, session.first_prompt, title)
                session.name = title
                session.title_loaded = True
            except Exception as exc:
                cache.mark_failure(session.session_id, session.first_prompt)
                session.name = "标题生成失败，稍后重试"
                errors.append(f"{session.session_id[:8]}: {exc}")
    return errors
