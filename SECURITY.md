# Security Policy

## 支持范围

当前仅对最新发布版本提供安全修复。

## 报告安全问题

请不要在公开 issue 中提交未修复的凭据泄露、命令注入、任意路径写入或错误关闭 tab 问题。请使用 GitHub 仓库的 **Security → Report a vulnerability** 私密报告。

报告中请包含：

- 受影响版本。
- 最小复现步骤。
- 预期影响。
- 已做脱敏的证据。

请勿提交真实 HAPi token、API key、私密 Prompt、Hub 内部 URL、真实 Session ID 或完整进程命令行。

## 数据访问

`bkanban` 的核心功能需要读取：

- Ghostty AppleScript inventory。
- `~/.hapi` 中的 Hub/runner 连接信息和 token。
- HAPi Session summaries/messages。
- Codex 本地 SQLite/rollout JSONL。
- Claude/TClaude 本地 transcript JSONL。

HAPi token 只用于 Authorization header，不写入 bkanban cache。请只将 `HAPI_API_URL` 指向你信任的 Hub；远程 Hub 应使用 HTTPS。

## 本地缓存

`bkanban` 会在 `~/.local/state/notebook-hapi-board/` 下缓存：

- 首次 Prompt 原文，最多 1200 字符/Session。
- Session ID、Prompt hash、标题模型名和生成标题。

目录权限为 `0700`，文件权限为 `0600`。清空：

```bash
bkanban clear-cache
```

## 可选标题模型

只有显式配置 OpenAI-compatible provider 后，首次 Prompt 才会发往该 provider。默认不读项目 `.env`。请根据你的数据政策选择本地模型或可信服务。

## 破坏性操作

- `[关闭]` / `x` 会停止 HAPi Session 并关闭整个 Ghostty tab。
- “清空 Session” / `Ctrl+X` 会关闭用户确认时快照中的全部 HAPi Session tabs。
- 两种操作都有确认屏；默认焦点是“取消”。
- 普通 Ghostty tabs 不会进入批量目标集。
- 跳转和关闭使用 stable IDs，不使用 title/cwd/index 猜测。

## 信任边界

- Ghostty AppleScript 自动化权限。
- HAPi Hub 和 runner。
- Agent 本地日志格式。
- 用户配置的标题 provider。

详细威胁边界见 [docs/TECHNICAL_DESIGN.md](docs/TECHNICAL_DESIGN.md)。
