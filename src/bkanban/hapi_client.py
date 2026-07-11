"""HAPi runner 与 hub 适配器。"""
from __future__ import annotations

import json
import os
import copy
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from .models import HapiChild


HAPI_HOME = Path(os.environ.get("HAPI_HOME") or (Path.home() / ".hapi")).expanduser()


class HapiError(RuntimeError):
    """HAPi 数据源不可用。"""


def _safe_transport_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return str(exc)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HapiError(f"无法读取 {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise HapiError(f"{path.name} 格式无效")
    return value


def _runner_url() -> str:
    state = _read_json(HAPI_HOME / "runner.state.json")
    port = state.get("httpPort")
    if not isinstance(port, int):
        raise HapiError("runner.state.json 中没有有效的 httpPort")
    return f"http://127.0.0.1:{port}"


def _hub_url() -> str:
    configured = os.environ.get("HAPI_API_URL")
    if configured:
        return configured.rstrip("/")
    state = _read_json(HAPI_HOME / "runner.state.json")
    value = state.get("startedWithApiUrl")
    if not value:
        raise HapiError("runner.state.json 中没有 startedWithApiUrl")
    return str(value).rstrip("/")


def _cli_token() -> str:
    configured = os.environ.get("CLI_API_TOKEN")
    if configured:
        return configured
    settings = _read_json(HAPI_HOME / "settings.json")
    value = settings.get("cliApiToken")
    if not value:
        raise HapiError("settings.json 中没有 cliApiToken，请先完成 HAPi 登录")
    return str(value)


def local_machine_id() -> str:
    return str(_read_json(HAPI_HOME / "settings.json").get("machineId") or "")


class RunnerClient:
    def __init__(self, timeout: float = 1.5) -> None:
        self.timeout = timeout

    def list_children(self) -> list[HapiChild]:
        try:
            response = httpx.post(
                f"{_runner_url()}/list",
                json={},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, HapiError) as exc:
            raise HapiError(f"HAPi runner 不可用: {_safe_transport_error(exc)}") from exc
        children: list[HapiChild] = []
        for item in payload.get("children", []):
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("happySessionId") or "")
            try:
                pid = int(item.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            if session_id and pid > 0:
                children.append(
                    HapiChild(
                        session_id=session_id,
                        pid=pid,
                        started_by=str(item.get("startedBy") or ""),
                    )
                )
        return attach_ttys(children)

    def stop_session(self, session_id: str) -> bool:
        """只停止指定 session，绝不停止全局 runner。"""
        try:
            response = httpx.post(
                f"{_runner_url()}/stop-session",
                json={"sessionId": session_id},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, HapiError) as exc:
            raise HapiError(
                f"HAPi Session 停止失败: {_safe_transport_error(exc)}"
            ) from exc
        return bool(payload.get("success")) if isinstance(payload, dict) else False


def attach_ttys(children: list[HapiChild]) -> list[HapiChild]:
    if not children:
        return []
    try:
        result = subprocess.run(
            [
                "ps",
                "-o",
                "pid=,tty=",
                "-p",
                ",".join(str(item.pid) for item in children),
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HapiError(f"无法读取 HAPi child TTY: {exc}") from exc
    if result.returncode not in (0, 1):
        raise HapiError(f"ps 读取 HAPi child 失败: {result.stderr.strip()[:160]}")
    by_pid: dict[int, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        tty = parts[1].strip()
        if tty not in ("?", "??", "-"):
            by_pid[pid] = tty
    def process_cwd(pid: int) -> str:
        try:
            probe = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        paths = [line[1:] for line in probe.stdout.splitlines() if line.startswith("n")]
        return paths[-1] if paths else ""

    return [
        HapiChild(
            session_id=item.session_id,
            pid=item.pid,
            started_by=item.started_by,
            tty=by_pid.get(item.pid, ""),
            cwd=process_cwd(item.pid),
        )
        for item in children
    ]


class HubClient:
    def __init__(self, timeout: float = 3.0) -> None:
        self.base_url = _hub_url()
        self._http = httpx.Client(timeout=timeout)
        self._lock = threading.RLock()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        with self._lock:
            try:
                response = self._http.request(
                    method,
                    f"{self.base_url}/cli{path}",
                    headers={"Authorization": f"Bearer {_cli_token()}"},
                    **kwargs,
                )
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError, HapiError) as exc:
                raise HapiError(
                    f"HAPi hub 不可用: {_safe_transport_error(exc)}"
                ) from exc

    def list_sessions(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/sessions")
        return [item for item in payload.get("sessions", []) if isinstance(item, dict)]

    def list_resumable(self, machine_id: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/sessions/resumable",
            params={"machineId": machine_id},
        )
        return [item for item in payload.get("sessions", []) if isinstance(item, dict)]

    def get_session(self, session_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/sessions/{session_id}")
        session = payload.get("session") if isinstance(payload, dict) else None
        return session if isinstance(session, dict) else payload

    def messages_page(
        self,
        session_id: str,
        *,
        limit: int = 200,
        after_seq: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "afterSeq": after_seq}
        payload = self._request(
            "GET",
            f"/sessions/{session_id}/messages",
            params=params,
        )
        messages = [
            item for item in payload.get("messages", []) if isinstance(item, dict)
        ]
        return messages

    def all_messages(
        self, session_id: str, *, max_pages: int = 15
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        after_seq = 0
        for _ in range(max_pages):
            messages = self.messages_page(
                session_id,
                after_seq=after_seq,
            )
            output.extend(messages)
            if len(messages) < 200:
                break
            next_seq = max((int(item.get("seq") or 0) for item in messages), default=0)
            if next_seq <= after_seq:
                break
            after_seq = next_seq
        return output

    def close(self) -> None:
        self._http.close()


class HapiService:
    """为刷新轮询加短暂退避，hub 断线时不阻塞每一轮。"""

    def __init__(self) -> None:
        self.runner = RunnerClient()
        self._hub: HubClient | None = None
        self._retry_at: dict[str, float] = {}
        self._last_errors: dict[str, str] = {}
        self._sessions_cache: list[dict[str, Any]] = []
        self._resumable_cache: list[dict[str, Any]] = []
        self._detail_cache: dict[str, dict[str, Any]] = {}
        self._state_lock = threading.RLock()

    def list_children(self) -> list[HapiChild]:
        return self.runner.list_children()

    def stop_session(self, session_id: str) -> bool:
        return self.runner.stop_session(session_id)

    def _hub_client(self) -> HubClient:
        if self._hub is None:
            self._hub = HubClient()
        return self._hub

    def _blocked(self, key: str) -> bool:
        with self._state_lock:
            return time.monotonic() < self._retry_at.get(key, 0)

    def _record_failure(self, key: str, exc: HapiError) -> None:
        with self._state_lock:
            self._last_errors[key] = str(exc)
            self._retry_at[key] = time.monotonic() + 15

    def _blocked_error(self, key: str) -> HapiError:
        with self._state_lock:
            return HapiError(self._last_errors.get(key) or "HAPi hub 暂时离线")

    def list_sessions(self) -> list[dict[str, Any]]:
        key = "sessions"
        if self._blocked(key):
            with self._state_lock:
                if self._sessions_cache:
                    return copy.deepcopy(self._sessions_cache)
            raise self._blocked_error(key)
        try:
            value = self._hub_client().list_sessions()
            with self._state_lock:
                self._sessions_cache = copy.deepcopy(value)
                self._retry_at.pop(key, None)
            return value
        except HapiError as exc:
            self._record_failure(key, exc)
            with self._state_lock:
                if self._sessions_cache:
                    return copy.deepcopy(self._sessions_cache)
            raise

    def list_resumable(self) -> list[dict[str, Any]]:
        key = "resumable"
        if self._blocked(key):
            with self._state_lock:
                if self._resumable_cache:
                    return copy.deepcopy(self._resumable_cache)
            raise self._blocked_error(key)
        try:
            value = self._hub_client().list_resumable(local_machine_id())
            with self._state_lock:
                self._resumable_cache = copy.deepcopy(value)
                self._retry_at.pop(key, None)
            return value
        except HapiError as exc:
            self._record_failure(key, exc)
            with self._state_lock:
                if self._resumable_cache:
                    return copy.deepcopy(self._resumable_cache)
            raise

    def get_session(self, session_id: str) -> dict[str, Any]:
        key = f"detail:{session_id}"
        if self._blocked(key):
            with self._state_lock:
                cached = self._detail_cache.get(session_id)
                if cached:
                    return copy.deepcopy(cached)
            raise self._blocked_error(key)
        try:
            value = self._hub_client().get_session(session_id)
            with self._state_lock:
                self._detail_cache[session_id] = copy.deepcopy(value)
                self._retry_at.pop(key, None)
            return value
        except HapiError as exc:
            self._record_failure(key, exc)
            with self._state_lock:
                cached = self._detail_cache.get(session_id)
                if cached:
                    return copy.deepcopy(cached)
            raise

    def all_messages(self, session_id: str) -> list[dict[str, Any]]:
        key = f"messages:{session_id}"
        if self._blocked(key):
            raise self._blocked_error(key)
        try:
            value = self._hub_client().all_messages(session_id)
            with self._state_lock:
                self._retry_at.pop(key, None)
            return value
        except HapiError as exc:
            self._record_failure(key, exc)
            raise

    def close(self) -> None:
        if self._hub is not None:
            self._hub.close()


class PromptCache:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (
            Path.home()
            / ".local"
            / "state"
            / "notebook-hapi-board"
            / "first-prompts.json"
        )
        self._loaded = False
        self._values: dict[str, str] = {}
        self._miss_at: dict[str, int | None] = {}
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                self._values = {
                    str(key): str(text)
                    for key, text in value.items()
                    if isinstance(text, str)
                }
        except (OSError, json.JSONDecodeError):
            pass
        self._loaded = True

    def get(self, session_id: str) -> str:
        with self._lock:
            self._load()
            return self._values.get(session_id, "")

    def put(self, session_id: str, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._load()
            self._values[session_id] = text[:1200]
            self._miss_at.pop(session_id, None)
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
            self._miss_at = {}
            self._loaded = True
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def should_fetch(self, session_id: str, updated_at: int | None) -> bool:
        with self._lock:
            self._load()
            if self._values.get(session_id):
                return False
            return self._miss_at.get(session_id, object()) != updated_at

    def mark_miss(self, session_id: str, updated_at: int | None) -> None:
        with self._lock:
            self._load()
            self._miss_at[session_id] = updated_at
