"""`bkanban` 命令入口。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .aggregate import collect_snapshot, hydrate_prompts
from .board import PROMPT_CHARS, _clip, _directory
from .hapi_client import HapiService, PromptCache
from .models import STATE_LABELS


def _format_rows(rows: list) -> str:
    headers = ("状态", "Agent", "Session 名称", "首次 Prompt", "Ghostty 标签", "目录")
    widths = (16, 11, 30, 30, 15, 24)
    lines = ["  ".join(value.ljust(width) for value, width in zip(headers, widths))]
    lines.append("  ".join("─" * width for width in widths))
    if not rows:
        lines.append("当前没有可见的 Ghostty 标签")
        return "\n".join(lines)
    for row in rows:
        primary = row.primary
        values = (
            primary.status_text if primary else STATE_LABELS[row.state],
            primary.flavor if primary else "-",
            row.session_name,
            _clip(row.first_prompt, PROMPT_CHARS),
            f"W{row.tab.window_index} · T{row.tab.tab_index}",
            _directory(row),
        )
        lines.append(
            "  ".join(
                _clip(str(value), width).ljust(width)
                for value, width in zip(values, widths)
            )
        )
    return "\n".join(lines)


def cmd_list() -> int:
    service = HapiService()
    cache = PromptCache()
    try:
        snapshot = collect_snapshot(
            service=service,
            prompt_cache=cache,
        )
        hydrate_prompts(snapshot, service=service, prompt_cache=cache)
    except Exception as exc:
        print(f"bkanban list 失败: {exc}", file=sys.stderr)
        return 1
    finally:
        service.close()
    print(_format_rows(snapshot.rows))
    if snapshot.warnings:
        print(f"\n降级提示: {len(snapshot.warnings)} 个", file=sys.stderr)
    return 0


def cmd_doctor() -> int:
    from . import ghostty

    service = HapiService()
    failures = 0
    try:
        terminals = ghostty.enumerate_terminals()
        tabs = ghostty.group_tabs(terminals)
        print(f"[通过] Ghostty inventory: {len(tabs)} tabs / {len(terminals)} terminals")
    except Exception as exc:
        failures += 1
        print(f"[失败] Ghostty inventory: {exc}")
    try:
        children = service.list_children()
        print(f"[通过] HAPi runner: {len(children)} 个在线 Session")
    except Exception as exc:
        failures += 1
        print(f"[失败] HAPi runner: {exc}")
    try:
        sessions = service.list_sessions()
        print(f"[通过] HAPi hub: {len(sessions)} 个 Session 摘要")
    except Exception as exc:
        print(f"[降级] HAPi hub: {exc}")
    finally:
        service.close()
    print("[说明] doctor 不写 OSC marker，不改标题，不执行跳转。")
    return 1 if failures else 0


def cmd_clear_cache() -> int:
    PromptCache().clear()
    legacy_title_cache = (
        Path.home() / ".local" / "state" / "notebook-hapi-board" / "titles.json"
    )
    try:
        legacy_title_cache.unlink()
    except FileNotFoundError:
        pass
    print("已清空首次 Prompt 缓存和旧版标题缓存。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bkanban",
        description="Ghostty 标签页事实源 + HAPi 状态和问答增强",
    )
    parser.add_argument("--version", action="version", version=f"bkanban {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("list", help="一次性打印当前 Ghostty 标签集合")
    subparsers.add_parser("doctor", help="只读检查 Ghostty 与 HAPi 数据源")
    subparsers.add_parser("clear-cache", help="删除本地 Prompt 和标题缓存")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "list":
        return cmd_list()
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "clear-cache":
        return cmd_clear_cache()
    from .board import NotebookBoardApp

    NotebookBoardApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
