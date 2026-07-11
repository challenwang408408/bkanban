from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SessionState(StrEnum):
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    WORKING = "working"
    BACKGROUND = "background"
    IDLE = "idle"
    HUB_OFFLINE = "hub_offline"
    TERMINAL = "terminal"


STATE_LABELS = {
    SessionState.WAITING_INPUT: "等待输入",
    SessionState.WAITING_APPROVAL: "等待审批",
    SessionState.WORKING: "Agent 处理中",
    SessionState.BACKGROUND: "后台任务中",
    SessionState.IDLE: "在线待命",
    SessionState.HUB_OFFLINE: "HAPi 未连接",
    SessionState.TERMINAL: "普通标签",
}


STATE_PRIORITY = {
    SessionState.WAITING_INPUT: 0,
    SessionState.WAITING_APPROVAL: 1,
    SessionState.WORKING: 2,
    SessionState.BACKGROUND: 3,
    SessionState.IDLE: 4,
    SessionState.HUB_OFFLINE: 5,
    SessionState.TERMINAL: 6,
}


@dataclass(frozen=True, slots=True)
class GhosttyTerminal:
    window_id: str
    window_index: int
    tab_id: str
    tab_index: int
    tab_title: str
    terminal_id: str
    title: str
    cwd: str
    selected: bool = False
    focused: bool = False


@dataclass(frozen=True, slots=True)
class GhosttyTab:
    window_id: str
    window_index: int
    tab_id: str
    tab_index: int
    title: str
    selected: bool
    focused_terminal_id: str
    terminals: tuple[GhosttyTerminal, ...]

    @property
    def target_terminal_id(self) -> str:
        return self.focused_terminal_id or self.terminals[0].terminal_id

    @property
    def cwd(self) -> str:
        focused = next(
            (item for item in self.terminals if item.terminal_id == self.target_terminal_id),
            self.terminals[0],
        )
        return focused.cwd


@dataclass(frozen=True, slots=True)
class HapiChild:
    session_id: str
    pid: int
    started_by: str = ""
    tty: str = ""
    cwd: str = ""


@dataclass(slots=True)
class ConversationRound:
    user: str
    assistant: str = ""
    created_at: int | None = None


@dataclass(slots=True)
class HapiSession:
    session_id: str
    pid: int
    tty: str
    terminal_id: str = ""
    name: str = ""
    flavor: str = "HAPi"
    state: SessionState = SessionState.HUB_OFFLINE
    state_detail: str = ""
    first_prompt: str = ""
    prompt_loaded: bool = False
    title_loaded: bool = False
    updated_at: int | None = None
    history: list[ConversationRound] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.first_prompt:
            self.prompt_loaded = True

    @property
    def status_text(self) -> str:
        return self.state_detail or STATE_LABELS[self.state]


@dataclass(slots=True)
class BoardRow:
    tab: GhosttyTab
    sessions: list[HapiSession] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.tab.tab_id

    @property
    def primary(self) -> HapiSession | None:
        if not self.sessions:
            return None
        focused = [
            item for item in self.sessions if item.terminal_id == self.tab.focused_terminal_id
        ]
        pool = focused or self.sessions
        return min(
            pool,
            key=lambda item: (
                STATE_PRIORITY[item.state],
                -(item.updated_at or 0),
                item.session_id,
            ),
        )

    @property
    def state(self) -> SessionState:
        return self.primary.state if self.primary else SessionState.TERMINAL

    @property
    def session_name(self) -> str:
        primary = self.primary
        if primary:
            suffix = f" +{len(self.sessions) - 1}" if len(self.sessions) > 1 else ""
            return f"{primary.name}{suffix}"
        return self.tab.title or self.tab.cwd.rstrip("/").rsplit("/", 1)[-1] or "Ghostty"

    @property
    def first_prompt(self) -> str:
        if not self.primary:
            return "未关联 HAPi Session"
        if self.primary.first_prompt:
            return self.primary.first_prompt
        return "尚未同步首次 Prompt" if self.primary.prompt_loaded else "正在读取首次 Prompt…"

    @property
    def target_terminal_id(self) -> str:
        primary = self.primary
        return primary.terminal_id if primary and primary.terminal_id else self.tab.target_terminal_id


@dataclass(slots=True)
class BoardSnapshot:
    rows: list[BoardRow]
    warnings: list[str] = field(default_factory=list)
