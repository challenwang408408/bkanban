# Contributing to bkanban

感谢你帮助改进 bkanban。这个项目的核心不变式是：**Ghostty tab 是列表的唯一事实源，HAPi 只做 enrichment。**

## 开发环境

```bash
git clone https://github.com/challenwang408408/bkanban.git
cd bkanban
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

## 提交前检查

- 新行为必须有对应测试。
- 不得让 HAPi-only Session 在 Ghostty inventory 外创建 row。
- 跳转和关闭必须使用 stable IDs，不能使用 title/cwd/index 猜测。
- 任何需要真实关闭 Ghostty tab 的 E2E 测试必须 opt-in，且只操作一次性测试 tab。
- 不得提交 `.env`、token、Prompt cache、真实 Session ID、完整进程命令行或个人绝对路径。
- 更新说明与当前实现一致，不将 roadmap 描述为已完成功能。

## Issue 建议

兼容性 issue 请包含：

- macOS 版本
- Ghostty 版本
- HAPi 版本
- Agent flavor 与版本
- `bkanban doctor` 的脱敏输出
- 预期行为和实际行为

请删除 token、Hub 密密 URL、私密 Prompt、Session ID 和进程命令行。

## Pull Request

1. 保持 PR 聚焦在一个明确问题。
2. 描述用户影响、技术取舍和验证方式。
3. 如果修改 HAPi/Ghostty/Agent 契约，附上脱敏 fixture 或可复现证据。
4. 确保全部测试通过。
