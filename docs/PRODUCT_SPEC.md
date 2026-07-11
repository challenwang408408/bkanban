# bkanban 产品方案

## 1. 产品摘要

`bkanban` 是一个面向 macOS、Ghostty 和 HAPi 多 Agent 开发者的 local-first 终端会话控制台。

它解决的不是“怎样创建更多 Session”，而是“当 Session 已经很多时，怎样看懂、回去和收尾”。

一句话定位：

> 把 Ghostty 当标签事实源，把 HAPi 当会话信息源：看清谁在工作，读到每轮结论，一键回到原标签底部。

## 2. 背景与问题

多 Agent 并行开发的主要管理单元并不是“项目目录”，而是“终端标签中的具体 Session”。同一项目常常同时有多个 Codex/Claude Session，它们拥有相同 cwd，但任务、进度和下一步完全不同。

典型痛点：

1. **标签不可辨认**：只显示目录名，无法知道具体任务。
2. **在线不等于工作中**：HAPi `active` 和 `thinking` 不足以覆盖本地受控 Agent 的真实 turn 状态。
3. **详情信息噪声大**：完整消息流里有 reasoning、工具参数、工具结果和 token 统计，而用户主要想回顾问题与结论。
4. **回到现场成本高**：长 Session 聚焦后仍要手动滚到最底部。
5. **收尾成本高**：完成的 Session 没有统一的快速关闭入口。

## 3. 目标用户

核心用户：

- 在 macOS 上使用 Ghostty。
- 通过 HAPi 启动 Codex、Claude、TClaude 或其他 Agent。
- 常态同时保持 5–20 个终端 tab。
- 需要在多个开发任务之间快速切换。

非目标用户：

- 不使用 Ghostty 的通用终端用户。
- 只有单一 Agent Session 的轻量用户。
- 寻找跨设备历史 Session 搜索或 Web 运维看板的用户。

## 4. 产品原则

### 4.1 所见即集合

用户问的是“我 Ghostty 里现在有什么”，所以 Ghostty tabs 必须是集合唯一事实源。HAPi 只能增强已存在的 tab，不能决定哪些 row 出现。

### 4.2 用户任务优先于技术身份

列表主信息是“这个 Session 在干什么”，不是 session UUID、PID 或 cwd。技术字段只用于关联和辅助诊断。

### 4.3 结论优先于过程

详情页默认只保留用户输入和 AI 的最后可见正文。这是一个“恢复上下文”界面，不是调试日志浏览器。

### 4.4 破坏性操作必须精确

跳转和关闭必须基于 stable IDs，不允许使用标题、序号或当前焦点猜测。批量关闭必须确认、冻结清单，且不纳入普通 tab。

### 4.5 降级优于消失

Hub 或某个 enrichment 失败时，row 应降级为普通标签或状态未知，而不是从集合中消失。

## 5. 核心场景

### 场景 A：辨认会话

1. 用户启动 `bkanban`。
2. 看板一行对应一个真实 Ghostty tab。
3. 用户通过任务标题和首次 Prompt 辨认 Session。
4. 用户通过状态颜色决定优先处理顺序。

### 场景 B：恢复上下文

1. 用户按 Enter 打开详情。
2. 详情按轮次展示“我输入的问题 / AI 最后结论”。
3. 长结论保留头尾，省略中间。

### 场景 C：回到开发现场

1. 用户在详情再按 Enter，或在主列表按 `g`。
2. Ghostty 精确选中原 window/tab/terminal。
3. terminal 执行 `scroll_to_bottom`。

### 场景 D：关闭已完成 Session

1. 用户点击行末 `[关闭]` 或按 `x`。
2. 弹窗展示任务名；工作中 Session 附加终止警告。
3. 确认后先请求 HAPi 停止精确 Session，给予短暂退出窗口，再关闭 stable tab ID。

### 场景 E：批量收尾

1. 用户点击“清空 Session”或按 `Ctrl+X`。
2. 弹窗展示目标数量和部分标题。
3. 确认时冻结 HAPi Session tab ID 清单。
4. 普通 Ghostty tab 和确认后新建 tab 不被关闭。

## 6. 信息架构

### 主列表

| 字段 | 用户问题 |
|---|---|
| 状态 | 我现在需要介入吗？ |
| Agent | 这是哪个 Agent？ |
| Session 名称 | 这个任务在做什么？ |
| 首次 Prompt | 这个任务是怎样开始的？ |
| Ghostty 标签 | 它在哪个 window/tab？ |
| 目录 | 它属于哪个项目？ |
| 操作 | 我能直接结束它吗？ |

### 详情页

- 标题与 Ghostty 位置。
- 关联 Session 数量。
- 用户输入。
- AI 结论。
- 连接并滚到底部。

## 7. 成功标准

### 产品准确性

- Ghostty 当前 tab 数与看板 row 数一致。
- HAPi 历史/远程 Session 不会创建新 row。
- 跳转和关闭不依赖 tab title、cwd 或 index。
- 工作中的 Codex/Claude Session 不因 HAPi `thinking=false` 被系统性误标为待命。

### 操作效率

- 两次 Enter 从列表到原 Session 底部。
- 一个键位 + 确认即可关闭当前 Session。
- 一个入口 + 确认即可收尾当前全部 HAPi tabs。

### 可靠性

- 单个 enrichment 失败不影响其他 row。
- 批量关闭不扩大用户确认的目标范围。
- 缓存和凭据不进入代码仓库。

## 8. 非目标

- 不通过 HAPi Session 列表反推 Ghostty 标签集合。
- 不使用 HAPi `attach` 或 `resume` 切换当前标签。
- 不展示完整 reasoning 和工具日志。
- 不管理或停止全局 HAPi runner。
- 不对其他终端或 Linux/Windows 做未经验证的支持承诺。

## 9. 隐私原则

- 默认不读取项目 `.env`。
- 没有显式配置标题模型凭据时，不向 OpenAI 发送 Prompt。
- 标题 provider 的数据外发必须在 README 中明示。
- 本地 Prompt 缓存必须使用私有权限，并提供清空命令。
- 诊断信息不包含 token、Authorization header、完整进程命令行或完整私密消息。

## 10. 路线图

### v0.1 · Public-ready

- Ghostty 事实源 + HAPi enrichment。
- Codex/Claude 原生状态修正。
- 详情结论、精确跳转、单个/批量关闭。
- OpenAI-compatible 标题 provider。
- 通用安装、隐私和开源文档。

### v0.2 · Configurable

- `~/.config/bkanban/config.toml`。
- 刷新频率、显示列、标题 provider 配置。
- `doctor --verbose` 脱敏诊断包。
- pipx / Homebrew 发布路径。

### v0.3 · Extensible

- Agent state adapter 插件化。
- 更清晰的 multi-split / multi-session 详情。
- 事件驱动刷新。

## 11. 开放问题

- HAPi 后续版本是否稳定 runner/hub CLI API 契约？
- Codex/Claude 原生日志格式变化如何做 fixture 驱动的兼容性测试？
- 是否需要将“连接状态”和“当前 turn 状态”分成两列？
- 如何在不引入通用终端抽象的前提下，保持 Ghostty 产品优势？
