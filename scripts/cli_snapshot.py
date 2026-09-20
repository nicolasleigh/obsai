#!/usr/bin/env python3
"""CLI 输出基线快照工具。

用途：阶段 A 的改造承诺是「CLI 输出与改造前逐字节一致」。本脚本在固定的
Vault + 配置上跑一遍全部**离线可用**的 CLI 调用，把 stdout / stderr / 退出码
写成 JSON；改造前后各跑一次即可机械对比，而不是靠人眼读测试断言。

用法：
    .venv/bin/python scripts/cli_snapshot.py --write baseline.json
    .venv/bin/python scripts/cli_snapshot.py --compare baseline.json

对比失败时以非零码退出，并逐条打印差异。

设计约束：
- 必须隔离 HOME / XDG_CONFIG_HOME，否则会读到用户真实的 ~/.config/obsai/config.toml。
- 固定工作根目录（默认 /tmp/obsai-cli-snapshot），使输出里的绝对路径在两次运行间稳定。
- 归一化随机量：trash 目标目录里的 uuid、以及 Rich 的 ANSI 转义序列。
- 只覆盖不访问网络的命令；embeddings / ask / agent 的联网路径以「缺 key 时报错」的形式纳入。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

DEFAULT_ROOT = Path("/tmp/obsai-cli-snapshot")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# chunk_id 是含随机 note_id 的 sha256，每次重建索引都不同；note_id 本身是随机 uuid。
# 两者都不是行为差异，归一化后才能真正对比输出结构。
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
UUID_RE = re.compile(r"\b[0-9a-f]{32}\b")

VAULT_NOTES = {
    "A.md": "# Alpha\n\nAlpha body with a [[Reference]] link.\n",
    "Reference.md": "# Reference\n\nSee [[A]] and [[A#Alpha]].\n",
    "Notes/Beta.md": "# Beta\n\nBeta body mentioning Alpha.\n",
    "Inbox/Loose.md": "# Loose Note\n\nUnfiled content about Alpha.\n",
}

# (名称, argv, 交互输入)。输入为 None 表示该命令不需要 stdin。
CASES: list[tuple[str, list[str], str | None]] = [
    ("help", ["--help"], None),
    ("version", ["--version"], None),
    ("status", ["status"], None),
    ("index-update-initial", ["index", "update"], None),
    ("index-update-idempotent", ["index", "update"], None),
    ("index-rebuild", ["index", "rebuild"], None),
    ("search-hybrid", ["search", "Alpha"], None),
    ("search-keyword", ["search", "Alpha", "--mode", "keyword"], None),
    ("search-hybrid-json", ["search", "Alpha", "--mode", "hybrid", "--json"], None),
    ("search-graph", ["search", "Alpha", "--mode", "graph"], None),
    ("search-semantic-missing-index", ["search", "Alpha", "--mode", "semantic"], None),
    ("search-invalid-mode", ["search", "Alpha", "--mode", "bogus"], None),
    ("search-tag-no-match", ["search", "Alpha", "--limit", "1", "--tag", "absent"], None),
    ("search-folder", ["search", "Alpha", "--folder", "Notes"], None),
    ("search-frontmatter-scalar", ["search", "Alpha", "--frontmatter", "status=done"], None),
    ("search-frontmatter-nonscalar", ["search", "Alpha", "--frontmatter", 'x={"a":1}'], None),
    ("search-frontmatter-malformed", ["search", "Alpha", "--frontmatter", "noequals"], None),
    ("search-dataview", ["search", "Alpha", "--dataview", "rating=5"], None),
    ("search-modified-after-bad", ["search", "Alpha", "--modified-after", "notadate"], None),
    ("search-modified-after-ok", ["search", "Alpha", "--modified-after", "2000-01-01"], None),
    ("search-strict-semantic", ["search", "Alpha", "--strict-semantic"], None),
    ("links-backlinks", ["links", "backlinks", "A.md"], None),
    ("links-backlinks-missing", ["links", "backlinks", "Nope.md"], None),
    ("links-outgoing", ["links", "outgoing", "A.md"], None),
    ("links-related", ["links", "related", "A.md"], None),
    ("links-related-limited", ["links", "related", "A.md", "--depth", "1", "--max-nodes", "1"], None),
    ("links-suggest", ["links", "suggest", "A.md"], None),
    ("note-create-declined", ["note", "create", "Notes/New.md", "--content", "# New\n"], "n\n"),
    ("note-create-approved", ["note", "create", "Notes/New.md", "--content", "# New\n"], "y\n"),
    ("note-create-collision", ["note", "create", "Notes/New.md", "--content", "# New\n"], "y\n"),
    ("note-update-approved", ["note", "update", "A.md", "--old", "Alpha body", "--new", "Edited body"], "y\n"),
    ("note-update-span-missing", ["note", "update", "A.md", "--old", "zzz-not-present", "--new", "x"], "y\n"),
    ("note-frontmatter-approved", ["note", "frontmatter", "A.md", "--set", "status=done"], "y\n"),
    ("note-frontmatter-bad", ["note", "frontmatter", "A.md", "--set", "noequals"], "y\n"),
    ("note-move-approved", ["note", "move", "A.md", "Notes/Moved.md"], "y\n"),
    ("note-trash-approved", ["note", "trash", "Notes/Moved.md"], "y\n"),
    ("transaction-status", ["transaction", "status"], None),
    ("organize-inbox-cancel", ["organize", "inbox"], "q\n"),
    ("ask-no-key", ["ask", "What is Alpha?"], None),
    ("agent-run-no-key", ["agent", "run", "Summarize Alpha"], None),
    ("agent-resume-unknown", ["agent", "resume", "deadbeef"], None),
    ("note-create-escaping-path", ["note", "create", "../escape.md", "--content", "x"], "y\n"),
    ("note-create-non-markdown", ["note", "create", "Notes/file.txt", "--content", "x"], "y\n"),
]


def normalize(text: str) -> str:
    """抹平随机量与终端渲染差异，只保留语义内容。"""
    text = ANSI_RE.sub("", text)
    text = HASH_RE.sub("<HASH>", text)
    text = UUID_RE.sub("<UUID>", text)
    return text.replace(str(DEFAULT_ROOT), "<ROOT>")


def prepare(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)
    vault = root / "vault"
    vault.mkdir(parents=True)
    for name, content in VAULT_NOTES.items():
        target = vault / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    config = root / "config" / "obsai" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'[vault]\npath = "{vault}"\n[index]\ndatabase = "{root / "index.db"}"\n',
        encoding="utf-8",
    )

    # 与 tests/conftest.py 保持一致的环境隔离。
    os.environ["HOME"] = str(root)
    os.environ["XDG_CONFIG_HOME"] = str(root / "config")
    # 钉住 Rich 的渲染宽度，避免外部 COLUMNS 影响换行位置。
    os.environ["COLUMNS"] = "80"
    os.environ.pop("OPENAI_API_KEY", None)
    for name in [n for n in os.environ if n.startswith("OBSAI_")]:
        os.environ.pop(name, None)
    os.environ["OBSAI_CONFIG_PATH"] = str(config)


def collect(root: Path) -> list[dict]:
    from typer.testing import CliRunner

    from obsai.cli.app import app

    prepare(root)
    runner = CliRunner()
    records = []
    for name, argv, stdin in CASES:
        result = runner.invoke(app, argv, input=stdin)
        records.append(
            {
                "name": name,
                "argv": argv,
                "exit_code": result.exit_code,
                "stdout": normalize(result.output),
                "stderr": normalize(getattr(result, "stderr", "") or ""),
                "exception": type(result.exception).__name__ if result.exception else None,
            }
        )
    return records


def compare(baseline: list[dict], current: list[dict]) -> int:
    by_name = {item["name"]: item for item in baseline}
    failures = 0
    for item in current:
        expected = by_name.get(item["name"])
        if expected is None:
            print(f"[新增] {item['name']}（基线中不存在，跳过对比）")
            continue
        for field in ("exit_code", "stdout", "stderr", "exception"):
            if expected[field] != item[field]:
                failures += 1
                print(f"\n[差异] {item['name']} :: {field}")
                print(f"  改造前: {expected[field]!r}")
                print(f"  改造后: {item[field]!r}")
    missing = set(by_name) - {item["name"] for item in current}
    for name in sorted(missing):
        failures += 1
        print(f"\n[缺失] {name} 在本次快照中不存在")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="快照用的工作根目录")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", type=Path, metavar="PATH", help="采集快照并写入 PATH")
    group.add_argument("--compare", type=Path, metavar="PATH", help="采集快照并与 PATH 对比")
    args = parser.parse_args()

    records = collect(args.root)

    if args.write:
        args.write.write_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"已写入 {len(records)} 条快照 -> {args.write}")
        return 0

    baseline = json.loads(args.compare.read_text(encoding="utf-8"))
    failures = compare(baseline, records)
    if failures:
        print(f"\n对比失败：{failures} 处差异")
        return 1
    print(f"\n对比通过：{len(records)} 条调用输出完全一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
