#!/usr/bin/env python3
"""用无头 Chromium 真实渲染 ObsAgent UI，逐路由核对侧栏高亮、页面标题与页面内容。

为什么需要它：`curl` 只能拿到 `index.html` 这个空壳，路由、侧栏、面包屑、卡片全部由
客户端渲染。`vitest` 侧的测试只覆盖纯逻辑（匹配规则、状态摘要），碰不到"点进去到底
渲染了什么"。这个脚本补上那一段。

用法：

    ./scripts/dev.sh &                          # 后端 + 开发服务器
    npm --prefix web run build
    npm --prefix web run preview &              # 打预览产物
    .venv/bin/python scripts/web-smoke.py

**场景二：后端未配置 Vault。** B-4 的验收标准是"无 Vault 时给出明确引导"，而这条
只有在后端真的没有 Vault 时才看得到。用隔离的 HOME 起第二个后端（`load_settings()`
读 `$HOME/.config/obsai/config.toml`，隔离后自然读不到），再让第二个预览服务把 `/api`
代理过去：

    TMP=$(mktemp -d)
    HOME=$TMP .venv/bin/python -m uvicorn obsai.api.app:app --host 127.0.0.1 --port 8001 &
    OBSAI_UI_TARGET=http://127.0.0.1:8001 npm --prefix web run preview -- --port 4174 &
    .venv/bin/python scripts/web-smoke.py --base-url http://127.0.0.1:4174 --state none

**场景三：后端有待恢复的事务。** 计划 §5 B-4 要求"恢复状态用 `alert` 醒目提示"，而
`recovery_required` 需要磁盘上真的有一份未完成的 journal。隔离 HOME 里造一份即可：

    ROOT=/tmp/obsai-recovery-home
    mkdir -p $ROOT/.config/obsai $ROOT/vault/.obsai-transactions/deadbeef01234567
    # config.toml 里把 vault.path 指向 $ROOT/vault
    # journal.json 写 {"id": "deadbeef01234567", "status": "applying",
    #                  "originals": [{"path": "notes/alpha.md", "snapshot": "…"}]}
    HOME=$ROOT .venv/bin/python -m uvicorn obsai.api.app:app --host 127.0.0.1 --port 8002 &
    OBSAI_UI_TARGET=http://127.0.0.1:8002 npm --prefix web run preview -- --port 4175 &
    .venv/bin/python scripts/web-smoke.py --base-url http://127.0.0.1:4175 --state recovery

这个场景还顺带守住一条安全约束：journal 里带 `snapshot`（文件被改动前的**笔记原文**），
概览页只该列路径。脚本会断言快照内容**没有**出现在页面里。

真实 `HOME` 与真实 Vault 都不会被碰到：这些后端只读配置、只读索引，而它们的配置与
索引路径都落在临时目录里。

**场景四：搜索页真的跑一次查询。** B-5 的验收标准是「`--mode keyword` 无需 API key
可用；降级 warning 可见」，而这两句话只有在结果列表和告警真的渲染出来时才算数。
`--dump-dom` 不会打字，所以页面支持从地址栏恢复查询（`#/search?q=…&mode=…`），脚本
打的就是那个 URL：

    ROOT=/tmp/obsai-search-home
    mkdir -p $ROOT/.config/obsai $ROOT/vault/notes
    # config.toml 里把 vault.path 指向 $ROOT/vault
    # $ROOT/vault/notes/redis.md 写一篇含 "redis" 的笔记
    # 建一次索引：obsai index update
    HOME=$ROOT .venv/bin/python -m uvicorn obsai.api.app:app --host 127.0.0.1 --port 8003 &
    OBSAI_UI_TARGET=http://127.0.0.1:8003 npm --prefix web run preview -- --port 4176 &
    .venv/bin/python scripts/web-smoke.py --base-url http://127.0.0.1:4176 --state search \
        --search-query redis --search-note notes/redis.md

这个 fixture **没有向量**（建向量要调 embedding 后端，测试不该依赖网络），所以它同时
是"降级提示该出现"的前提——这正是 `--state search` 与 `--state vault` 状态灯不同的原因。

**场景五：问答页的两条路径。** B-6 的验收标准是「弃答、引用校验失败均有明确提示；未
配置 Key 时给出引导而非报错堆栈」。两种情形都不需要网络，但需要同一个 fixture 里既有
能命中索引的词、又有命不中的词：

    ROOT=/tmp/obsai-search-home          # 与场景四同一个 fixture
    OPENAI_API_KEY= HOME=$ROOT .venv/bin/python -m uvicorn obsai.api.app:app \
        --host 127.0.0.1 --port 8004 &
    OBSAI_UI_TARGET=http://127.0.0.1:8004 npm --prefix web run preview -- --port 4177 &
    .venv/bin/python scripts/web-smoke.py --base-url http://127.0.0.1:4177 --state search \
        --search-query redis --search-note notes/redis.md \
        --ask-miss zzzznotfound --ask-hit redis

`OPENAI_API_KEY=` 是**显式置空**而不是"不设置"：shell 或 IDE 里可能已经有一把真 key，
那样这条路径会真的去调模型，测的就不再是"缺 Key 的引导"。

**场景六：笔记页。** B-7 的验收标准是「含 `<script>`、`<iframe>` 的笔记不执行任何内容；
WikiLink 只跳转服务端校验过的 Vault 内目标」。fixture 与场景四、五同一个 HOME，另加一篇
带脚本的笔记（内容照抄下方 `NOTE_TITLE` 上方的说明），它链接到已存在的 `notes/redis.md`，
并链接一篇不存在的 `notes/gone.md`：

    ROOT=/tmp/obsai-search-home
    # 造 $ROOT/vault/notes/scripted.md（见 NOTE_TITLE 上方）
    HOME=$ROOT .venv/bin/obsai index update                  # 让新笔记进索引
    HOME=$ROOT .venv/bin/python -m uvicorn obsai.api.app:app --host 127.0.0.1 --port 8004 &
    OBSAI_UI_TARGET=http://127.0.0.1:8004 npm --prefix web run preview -- --port 4177 &
    .venv/bin/python scripts/web-smoke.py --base-url http://127.0.0.1:4177 --state search \
        --search-query redis --search-note notes/redis.md \
        --ask-miss zzzznotfound --ask-hit redis \
        --note-query 冒烟

入口是"先搜索、再从结果卡上取 note ID"——那是用户的真实路径，顺带证明结果卡确实可点
（B-5 留下的未落地项）。note ID 由索引在写入时分配，脚本不可能预先知道，这一跳正好把它
拿到；所以 `--note-query` 必须是能命中那篇笔记的词。

**场景七：远程同意对话框。** B-8 的验收标准是「拒绝时不发远程请求，明确降级为关键词
结果」，而计划点名的产出就是这个对话框——它必须在真实浏览器里被**看到**，不能只验"入口
按钮渲染出来了"。

这个场景要的是"语义后端可用、只是没人批准过"的状态。`probe_semantic` 判断语义腿能不能
跑用的是 `store.has_generation()`，那是**一次行查询**，不碰 provider，所以登记一行就够了
——不需要 key，也不需要网络。它是**另一个 HOME**，不能拿场景四那份直接用：场景四靠
"没有 generation"才看得到降级提示，种进去就把那条断言弄坏了：

    ROOT=/tmp/obsai-search-home            # 从场景四的 fixture 复制一份
    CONSENT=/tmp/obsai-consent-home
    cp -r $ROOT $CONSENT
    # 把 $CONSENT/.config/obsai/config.toml 里两条绝对路径改指到 $CONSENT
    HOME=$CONSENT .venv/bin/python -c "
    from obsai.application.embedding import build_embedding_pipeline
    from obsai.config.loader import load_settings
    from obsai.storage import Database
    settings = load_settings()
    with Database(settings.index.database) as database:
        store, pipeline = build_embedding_pipeline(database, settings)
        store.ensure_generation(pipeline.generation)
    "
    HOME=$CONSENT .venv/bin/python -m uvicorn obsai.api.app:app --host 127.0.0.1 --port 8005 &
    OBSAI_UI_TARGET=http://127.0.0.1:8005 npm --prefix web run preview -- --port 4178 &
    .venv/bin/python scripts/web-smoke.py --base-url http://127.0.0.1:4178 --state search \
        --consent-query redis --consent-note notes/redis.md

`--state search` 仍是对的：这个 fixture 的索引、状态灯与场景四同形，多出来的只有一行
generation。

对话框的开合也进 URL（`#/search?q=…&mode=…&consent=1`），理由与 `q` / `mode` 一样——
`--dump-dom` 不会点击。**"拒绝"这条路径浏览器验不到**：它需要一个点击，而这里没有点击
可用。它的证据在别处：`tests/integration/test_api_consent.py` 用 spy 数
`build_semantic_retriever`（查询离开本机的唯一一道门）证明拒绝后一个远程请求都没发，
`web/src/lib/consent.test.ts` 证明拒绝状态该显示什么。刻意**没有**为了凑一个浏览器场景
而往 URL 里塞一个"已拒绝"的决定——URL 记录不了用户还没做出的决定。

环境变量：`OBSAI_UI_BASE`（默认 `http://127.0.0.1:4173`）、`CHROME`（覆盖浏览器路径）。

三条环境注意事项（都踩过）：

1. **必须打预览服务，不能打开发服务器。** Vite dev 挂着 HMR 的 WebSocket，
   `--virtual-time-budget` 永远等不到网络空闲，浏览器会卡到超时。
2. **用 Playwright 的 `chrome-headless-shell` 而不是系统 Chrome。** 系统 Chrome 在这个
   受限环境里会因 "sandbox initialization failed: Operation not permitted" 拖垮 GPU
   进程并 FATAL 退出；而 `chrome-headless-shell` 是 Playwright 已经装好的最小无头构建，
   只要 `--no-sandbox` 就能跑。脚本会自动探测两者。
3. **本机没有 `timeout` 命令**（macOS 默认不带 coreutils），所以超时保护只能靠
   `subprocess.run(timeout=...)`，不要在 shell 里套 `timeout`。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, TypedDict
from urllib.parse import quote

#: Playwright 的缓存目录；`chrome-headless-shell` 就装在这里。
PLAYWRIGHT_CACHE = Path.home() / "Library/Caches/ms-playwright"

#: 由 `main()` 从命令行覆盖。
BASE = os.environ.get("OBSAI_UI_BASE", "http://127.0.0.1:4173")


def find_browser() -> tuple[str, bool]:
    """返回 (可执行文件路径, 是否需要自己加 --headless)。

    优先用 Playwright 的 headless shell：它本身就是无头构建，且不需要 Chrome 那套
    在受限环境里起不来的进程沙箱。
    """
    override = os.environ.get("CHROME")
    if override:
        return override, "headless-shell" not in override

    for shell in sorted(
        PLAYWRIGHT_CACHE.glob("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell")
    ):
        if shell.is_file():
            return str(shell), False

    fallback = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(fallback).is_file():
        return fallback, True

    raise SystemExit(
        "找不到可用的无头浏览器。用 CHROME=<路径> 指定，或安装 Playwright 的 Chromium。"
    )


BROWSER, NEEDS_HEADLESS_FLAG = find_browser()

#: 计划文档 §5 B-3 要求的八个侧栏入口，顺序即渲染顺序。
SIDEBAR = ["概览", "搜索", "问答", "索引", "整理", "写入", "Agent", "恢复"]

#: 侧栏徽标的两组期望：已实现的页面不显示步骤，未实现的显示。
#:
#: 与 `web/src/lib/navigation.ts` 的 `status` 字段一一对应。**页面交付时要一起改**，
#: 而改它正好是一次"侧栏状态和实现是否同步"的复核。
#: `/notes`（B-7）不在侧栏，所以不列入。
READY_STEPS: list[tuple[str, str]] = [
    ("概览", "B-4"),
    ("搜索", "B-5"),
    ("问答", "B-6"),
    ("索引", "C-3"),
    ("整理", "D-4"),
    ("写入", "D-1"),
    ("恢复", "D-5"),
    ("Agent", "E-2"),
]
PLANNED_STEPS: list[tuple[str, str]] = []

#: 路由 → (期望 h1, 期望被点亮的侧栏项或 None)
CASES: list[tuple[str, str, str | None]] = [
    ("/", "概览", "概览"),
    ("/search", "搜索", "搜索"),
    ("/ask", "问答", "问答"),
    ("/index", "索引", "索引"),
    ("/organize", "整理", "整理"),
    ("/changes", "写入", "写入"),
    ("/agent", "Agent", "Agent"),
    ("/recovery", "恢复", "恢复"),
    # 笔记详情没有侧栏入口，因此不该点亮任何一项。
    ("/notes/abc-123", "笔记", None),
    # 向量生成计划由索引页进入，不在侧栏，不点亮侧栏项。
    ("/embedding", "向量生成", None),
    # 手改地址栏的兜底页。
    ("/nope", "没有这个页面", None),
]

#: 概览页六张卡片的标题。`Vault` / `索引` / `写入锁` 与侧栏或摘要文案重名，断言价值
#: 有限；真正能证明"卡片渲染出来了"的是下面这几个长标题。
OVERVIEW_CARDS = ["待重新索引的笔记", "未完成事务", "最近任务", "任务运行器尚未接入 API"]

#: 后端状态 → 概览页该出现什么、不该出现什么、该有几条 alert、侧栏状态灯该说什么。
#:
#: `absent` 比 `present` 更重要。无 Vault 时最容易犯的错是照样渲染"无待恢复事务"；
#: 恢复场景里最容易犯的错是把 journal 的 `snapshot`（笔记原文）顺手打进页面。
class StateExpectation(TypedDict):
    present: list[str]
    absent: list[str]
    alerts: int
    lamp: str


STATE_EXPECTATIONS: dict[str, StateExpectation] = {
    "vault": {
        "present": ["Vault 就绪", "索引文件", "语义检索"],
        "absent": ["还没有配置 Vault", "无法检查事务状态", "写入已被冻结"],
        "alerts": 0,
        "lamp": "后端已连接",
    },
    "none": {
        "present": [
            "还没有配置 Vault",
            "No vault configured",
            "无法检查事务状态",
            "无法检查待重新索引的笔记",
        ],
        "absent": ["Vault 就绪", "没有待重新索引的笔记"],
        "alerts": 1,
        "lamp": "未配置 Vault",
    },
    "recovery": {
        "present": [
            "有 1 个未完成的写入事务，写入已被冻结",
            "obsai transaction recover",
            "notes/alpha.md",
            "notes/beta.md",
            "feedfacecafebabe",
        ],
        # 快照是笔记正文。概览页只列路径；差异展示属于 D-5 的恢复页。
        "absent": ["SNAPSHOT-MUST-NOT-BE-RENDERED", "ANOTHER-SECRET-BODY"],
        "alerts": 1,
        "lamp": "有 1 个未完成的写入事务",
    },
    # 搜索场景用的 fixture：Vault 与索引都正常，但**没有向量**（建向量要调 embedding
    # 后端，测试不该依赖网络）。所以索引摘要是一句警告，状态灯也就不是「后端已连接」。
    # 这恰好是 B-5 降级提示会出现的前提，所以它是这个场景需要的状态，而不是妥协。
    "search": {
        "present": ["Vault 就绪", "索引文件", "已索引", "语义检索不可用"],
        "absent": ["还没有配置 Vault", "无法检查事务状态", "写入已被冻结"],
        "alerts": 0,
        # 只断言前缀：篇数由 fixture 决定，写死会让改 fixture 时莫名其妙地红。
        "lamp": "已索引",
    },
}

PER_ROUTE_TIMEOUT = 60


class DomProbe(HTMLParser):
    """收集主导航里的链接、h1、title、`data-slot` 与正文文本。

    `<main>` 内部另有一组收集（`main_tags` / `main_links`）。分开是因为"页面上没有
    `<script>`"这句话对整页不成立——React 自己的 bundle 就在 `<head>` 里。B-7 要断言
    的是**笔记内容**没有渲染出任何可执行元素，所以必须限定在内容区。

    内容区是**带 `id="main"` 的那个** `<main>`：shadcn 的 `SidebarInset` 自己也会渲染
    一个 `<main>` 把页头一起包进去，只按标签名判断会把页头的面包屑与图标都算成内容。
    """

    def __init__(self) -> None:
        super().__init__()
        self._nav_depth = 0
        self._main_stack: list[bool] = []
        self._open: list[dict[str, str]] = []
        self.links: list[dict[str, str | None]] = []
        self.headings: list[str] = []
        self.titles: list[str] = []
        self.slots: list[str] = []
        self.inputs: list[dict[str, str]] = []
        self.text_parts: list[str] = []
        #: 内容区里出现过的每个开始标签名，按出现顺序。
        self.main_tags: list[str] = []
        #: 内容区里的链接（`<a>`）：笔记页的 WikiLink 与搜索页的结果卡都在这里。
        self.main_links: list[dict[str, str | None]] = []
        #: 带 `data-highlighted="true"` 的块数（笔记页的 `?block=` 定位）。
        self.highlighted_blocks = 0

    @property
    def _in_content(self) -> bool:
        return any(self._main_stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: (value or "") for key, value in attrs}
        if tag == "nav" and attr.get("aria-label") == "主导航":
            self._nav_depth += 1
        if tag == "main":
            self._main_stack.append(attr.get("id") == "main")
        elif self._in_content:
            self.main_tags.append(tag)
            if attr.get("data-highlighted") == "true":
                self.highlighted_blocks += 1
        # `data-slot` 是 shadcn 组件自带的标记（`alert` / `card` / `skeleton`…），
        # 用它数组件比数 CSS 类或结构层级稳得多。
        if "data-slot" in attr:
            self.slots.append(attr["data-slot"])
        # 受控 `input` 的 `value` 由 React 写进 DOM 属性，`--dump-dom` 能拿到。
        # 用它验证"URL 里的 q 真的落到了输入框里"。
        if tag == "input":
            self.inputs.append({"value": attr.get("value", ""), "placeholder": attr.get("placeholder", "")})
        if tag in ("a", "h1", "title", "script", "style"):
            self._open.append(
                {
                    "tag": tag,
                    "text": "",
                    "href": attr.get("href"),
                    "current": attr.get("aria-current"),
                }
            )

    def handle_endtag(self, tag: str) -> None:
        if self._open and self._open[-1]["tag"] == tag:
            node = self._open.pop()
            text = " ".join(node["text"].split())
            if tag == "a":
                if self._nav_depth > 0:
                    self.links.append({"text": text, "current": node["current"]})
                elif self._in_content:
                    self.main_links.append({"text": text, "href": node["href"]})
            elif tag == "h1":
                self.headings.append(text)
            elif tag == "title":
                self.titles.append(text)
        if tag == "nav":
            self._nav_depth = max(0, self._nav_depth - 1)
        if tag == "main" and self._main_stack:
            self._main_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._open:
            self._open[-1]["text"] += data
        if not any(node["tag"] in ("script", "style") for node in self._open):
            self.text_parts.append(data)

    @property
    def body_text(self) -> str:
        return " ".join(" ".join(self.text_parts).split())

    def count_slots(self, name: str) -> int:
        return sum(1 for slot in self.slots if slot == name)

    def main_link_to(self, text: str) -> str | None:
        """内容区里文字为 ``text`` 的链接地址；没有则 `None`。"""
        for link in self.main_links:
            if str(link["text"]) == text:
                return None if link["href"] is None else str(link["href"])
        return None


def render(route: str) -> DomProbe:
    with tempfile.TemporaryDirectory(prefix="obsai-smoke-") as profile:
        args = [
            BROWSER,
            # Chrome 自己的沙箱在受限环境里起不来（"sandbox initialization failed:
            # Operation not permitted"），进而拖垮 GPU 进程并以 FATAL 退出。这里访问的
            # 只是本机回环地址上的自有页面，关掉浏览器沙箱不影响被验证对象。
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-proxy-server",
            # 让页面在"虚拟时间"里跑满 6 秒再 dump，足够等 React 渲染与
            # /api/v1/status 的异步查询落地。
            "--virtual-time-budget=6000",
            f"--user-data-dir={profile}",
            "--dump-dom",
            f"{BASE}/#{route}",
        ]
        if NEEDS_HEADLESS_FLAG:
            args.insert(1, "--headless=new")
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=PER_ROUTE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise SystemExit(
                f"路由 {route} 渲染超时（{PER_ROUTE_TIMEOUT}s）。"
                "确认访问的是 `npm run preview` 而不是开发服务器——dev 的 HMR WebSocket "
                "会让虚拟时间永远等不到空闲。"
            ) from None
        except FileNotFoundError:
            raise SystemExit(f"找不到浏览器：{BROWSER}\n用 CHROME=<路径> 指定。") from None
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-2000:])
        raise SystemExit(f"浏览器退出码 {proc.returncode}（路由 {route}）")
    probe = DomProbe()
    probe.feed(proc.stdout)
    return probe


def check_overview(probe: DomProbe, *, state: str, failures: list[str]) -> None:
    """概览页的内容断言。这一组是 B-4 的验收证据。"""
    text = probe.body_text
    expectations = STATE_EXPECTATIONS[state]

    for fragment in OVERVIEW_CARDS:
        if fragment not in text:
            failures.append(f"概览页缺少 {fragment!r}")

    # B-3 的临时版把 `/status` 原始响应摊成一张调试卡。B-4 是最终版，它不该还在。
    if "原始状态" in text:
        failures.append("概览页仍在渲染 B-3 的「原始状态」调试卡")

    # `Alert` 组件带 `data-slot="alert"`，数它比数 CSS 类或结构层级稳得多。
    alerts = probe.count_slots("alert")
    expected_alerts = expectations["alerts"]
    if alerts != expected_alerts:
        failures.append(f"概览页有 {alerts} 条 alert，期望 {expected_alerts} 条")

    for fragment in expectations["present"]:
        if fragment not in text:
            failures.append(f"概览页缺少 {fragment!r}（state={state}）")
    for fragment in expectations["absent"]:
        if fragment in text:
            failures.append(f"概览页不该出现 {fragment!r}（state={state}）")


#: 搜索场景的两种模式 → (期望出现的降级提示, 期望的 alert 条数)。
#:
#: keyword 完全不碰 embedding 后端，所以既没有降级提示也没有 alert——这正是 B-5 的
#: 验收标准「--mode keyword 无需 API key 可用」。hybrid 会降级，所以必须有提示。
SEARCH_MODES: list[tuple[str, str, int]] = [
    ("keyword", "", 0),
    ("hybrid", "Semantic index missing", 1),
]


#: 需要异步查询才有内容的页面，允许重试的次数。
#:
#: 只给搜索页和问答页用。原因不是页面不稳，而是 `--dump-dom` 靠固定的虚拟时间预算
#: 截取 DOM：预览服务的代理在第一次收到 POST 时要建连接，偶发地那一次请求没能在预算
#: 内落地，页面就停在错误面板上。实测单独渲染 6/6 通过、夹在路由循环之后约 1/3 失败。
#:
#: 路由循环那十条**不重试**——它们断言的是静态渲染，重试只会掩盖真正的渲染回归。
QUERY_RENDER_ATTEMPTS = 2


def excerpt(text: str, limit: int = 240) -> str:
    """失败信息里附一段页面正文，否则"没渲染出结果"这句话没有诊断价值。

    取**尾部**：DOM 里 `<aside>`（侧栏）排在 `<main>` 之前，所以页面内容在正文末尾，
    而取头部只会每次都拿到同一段侧栏文字——那对排查没有任何帮助。
    """
    return "…" + text[-limit:] if len(text) > limit else text


def render_until(route: str, ready: Callable[[DomProbe], bool]) -> tuple[DomProbe, bool]:
    """渲染到 `ready` 成立为止，最多 `QUERY_RENDER_ATTEMPTS` 次。

    返回 (probe, 是否重试过)。重试是容许的，但"容许了多少次"不该是秘密——调用方会
    把重试打印出来，于是它既不会让门禁误红，也不会掩盖一个真的回归（真回归两次都失败）。
    """
    probe = render(route)
    if ready(probe):
        return probe, False
    for _ in range(QUERY_RENDER_ATTEMPTS - 1):
        probe = render(route)
        if ready(probe):
            return probe, True
    return probe, False


def check_search(*, query: str, note: str, failures: list[str]) -> None:
    """搜索页的内容断言。这一组是 B-5 的验收证据。

    查询串只能走 URL（`#/search?q=…&mode=…`）：`--dump-dom` 不会打字，所以页面必须
    支持从地址栏恢复查询——这既是可分享性的需要，也是这个脚本能跑到结果列表的唯一
    路径。`--search-query` / `--search-note` 由调用方给出，因为"该找到哪篇笔记"是
    fixture 的属性，不是脚本能猜的。
    """
    for mode, warning, alerts in SEARCH_MODES:
        label = f"搜索页 mode={mode}"
        probe, retried = render_until(
            f"/search?q={query}&mode={mode}",
            lambda candidate: note in candidate.body_text,
        )
        if retried:
            print(f"  （{label}：首次渲染没等到查询落地，重试一次后通过）")
        text = probe.body_text

        if note not in text:
            failures.append(f"{label}: 没有渲染出结果 {note!r}；页面显示：{excerpt(text)}")

        # URL 里的 q 必须真的落进输入框，否则上面的"有结果"可能是别的原因造成的。
        if query not in [item["value"] for item in probe.inputs]:
            failures.append(f"{label}: 搜索框的值不是 {query!r}，URL 里的 q 没有生效")

        found = probe.count_slots("alert")
        if found != alerts:
            failures.append(f"{label}: 有 {found} 条 alert，期望 {alerts} 条")
        if warning and warning not in text:
            failures.append(f"{label}: 缺少降级提示 {warning!r}")
        if not warning and "Semantic index missing" in text:
            failures.append(f"{label}: 不该出现降级提示")


class AskExpectation(TypedDict):
    present: list[str]
    absent: list[str]
    alerts: int


#: 问答页两条路径的期望。两条都不需要网络，见模块 docstring 的场景五。
#:
#: `hit` 那条的期望里同时包含中文标题、中文下一步动作、服务端英文原文：三样都在，
#: 才说明"引导而非报错堆栈"这条验收真的达成了——只有中文标题的话，用户看不到
#: 服务端到底说了什么；只有英文原文的话，等于没给引导。
ASK_EXPECTATIONS: dict[str, AskExpectation] = {
    "miss": {
        "present": ["未找到可用于回答的笔记证据。", "obsai index update"],
        "absent": ["缺少 API Key", "操作失败"],
        # 弃答本身一条 alert，加上"检索已降级"一条。
        "alerts": 2,
    },
    "hit": {
        "present": [
            "缺少 API Key，无法生成回答",
            "OPENAI_API_KEY",
            "config.toml 无需改动",
            "OPENAI_API_KEY is required for remote answers",
        ],
        # 错误面板是 Card 不是 Alert，所以这里不该有 alert。
        "absent": ["操作失败", "Traceback", "Cannot read properties"],
        "alerts": 0,
    },
}


def check_ask(*, hit: str, miss: str, failures: list[str]) -> None:
    """问答页的内容断言。这一组是 B-6 的验收证据。

    与搜索页同一个前提：问题只能走 URL（`#/ask?q=…`）。`--ask-hit` / `--ask-miss`
    由调用方给出，因为"哪个词能命中索引"是 fixture 的属性。
    """
    for kind, question in (("miss", miss), ("hit", hit)):
        expectation = ASK_EXPECTATIONS[kind]
        label = f"问答页 {kind}（{question!r}）"
        probe, retried = render_until(
            f"/ask?q={question}",
            lambda candidate: all(
                fragment in candidate.body_text for fragment in expectation["present"]
            ),
        )
        if retried:
            print(f"  （{label}：首次渲染没等到查询落地，重试一次后通过）")
        text = probe.body_text

        if question not in [item["value"] for item in probe.inputs]:
            failures.append(f"{label}: 输入框的值不是 {question!r}，URL 里的 q 没有生效")

        found = probe.count_slots("alert")
        if found != expectation["alerts"]:
            failures.append(f"{label}: 有 {found} 条 alert，期望 {expectation['alerts']} 条")

        for fragment in expectation["present"]:
            if fragment not in text:
                failures.append(f"{label}: 缺少 {fragment!r}；页面显示：{excerpt(text)}")
        for fragment in expectation["absent"]:
            if fragment in text:
                failures.append(f"{label}: 不该出现 {fragment!r}")


#: 场景六的 fixture 笔记。脚本不认识 Vault，只认识这份期望，所以 fixture 要照抄：
#:
#:     ---
#:     tags: [smoke]
#:     ---
#:     # 冒烟笔记
#:
#:     正文 <script>alert('smoke')</script> 之后还有字。
#:
#:     <script>alert('block')</script>
#:
#:     <iframe src="https://example.com/tracker"></iframe>
#:
#:     见 [[notes/redis|缓存笔记]] 与 [[notes/gone]]。
#:
#:     一段带锚点的话。 ^smoke-anchor
#:
#: `notes/redis.md` 要真实存在（否则 `缓存笔记` 也会变成断链），`notes/gone.md` 不能存在。
NOTE_TITLE = "冒烟笔记"
NOTE_PRESENT = ["正文", "之后还有字", "缓存笔记", "一段带锚点的话"]
#: 三样都不该出现，各证明一件事：iframe 整块消失、HTML 块的文字不落地、块锚点标记被剥掉。
NOTE_ABSENT = ["example.com", "alert('block')", "^smoke-anchor"]
NOTE_LINK_TEXT = "缓存笔记"
NOTE_MISSING_TEXT = "notes/gone"
NOTE_ANCHOR = "smoke-anchor"

#: 内容区里不该出现的标签。`script` / `iframe` 是 B-7 点名的两个，其余是同类——
#: 任何能执行代码或自动发起请求的元素。`img` 在列是因为"外部资源自动加载"同样被禁。
NOTE_FORBIDDEN_TAGS = ("script", "iframe", "img", "object", "embed", "link", "form")


def check_note(*, query: str, failures: list[str]) -> None:
    """笔记页的内容断言。这一组是 B-7 的验收证据。

    入口刻意走"先搜索、再从结果卡上取 note ID"：那是用户的真实路径，也顺带证明结果卡
    真的可点了（B-5 留下的未落地项）。note ID 由索引在写入时分配，脚本不可能预先知道，
    而这一跳正好把它拿到。

    断言分三层：内容渲染出来了；内容区没有出现任何可执行或会自动加载的元素；可跳转的
    WikiLink 是链接、不可跳转的只是文字。第三层是 B-7 验收的后半句——它的判据是服务端
    的 `target_note_id`，所以这里查的是"有没有 `<a>`"而不是"点了会怎样"。
    """
    label = "笔记页"
    # 查询串要转义：fixture 的标题是中文，而这里拼的是 URL。`quote` 对 ASCII 是恒等
    # 变换，所以英文查询串走同一条路径不会改变行为。
    probe, retried = render_until(
        f"/search?q={quote(query)}&mode=keyword",
        lambda candidate: candidate.main_link_to(NOTE_TITLE) is not None,
    )
    if retried:
        print(f"  （{label}：首次渲染没等到查询落地，重试一次后通过）")

    href = probe.main_link_to(NOTE_TITLE)
    if href is None or not href.startswith("#/notes/"):
        failures.append(
            f"{label}: 搜索结果里没有指向笔记的链接（拿到 {href!r}）；"
            f"页面显示：{excerpt(probe.body_text)}"
        )
        return
    note_id = href[len("#/notes/") :]
    print(f"  从搜索结果进入 #/notes/{note_id}")

    note = render(f"/notes/{note_id}")
    text = note.body_text
    for fragment in NOTE_PRESENT:
        if fragment not in text:
            failures.append(f"{label}: 缺少 {fragment!r}；页面显示：{excerpt(text)}")
    for fragment in NOTE_ABSENT:
        if fragment in text:
            failures.append(f"{label}: 不该出现 {fragment!r}")

    # 先证明正文真的渲染成了块。否则"没有出现 <script>"这句话可能只是因为页面是空的。
    if note.count_slots("note-block") == 0:
        failures.append(f"{label}: 一个正文块都没渲染出来；页面显示：{excerpt(text)}")

    # 限定在内容区：React 自己的 bundle 就在 <head> 的 <script> 里，整页查会恒真。
    rendered = sorted({tag for tag in note.main_tags if tag in NOTE_FORBIDDEN_TAGS})
    if rendered:
        failures.append(f"{label}: 笔记内容渲染出了可执行或会自动加载的元素 {rendered}")

    if note.main_link_to(NOTE_LINK_TEXT) is None:
        failures.append(f"{label}: {NOTE_LINK_TEXT!r} 是可解析的 WikiLink，应当渲染成链接")
    if note.main_link_to(NOTE_MISSING_TEXT) is not None:
        failures.append(
            f"{label}: {NOTE_MISSING_TEXT!r} 指向不存在的笔记，不该渲染成链接"
        )
    if "部分 WikiLink 无法跳转" not in text:
        failures.append(f"{label}: 有断链却没有给出提示")

    # `?block=` 的定位：恰好一块被高亮。零块说明锚点没生效，多块说明选择器写错了。
    anchored = render(f"/notes/{note_id}?block={NOTE_ANCHOR}")
    if anchored.highlighted_blocks != 1:
        failures.append(
            f"{label}: ?block={NOTE_ANCHOR} 高亮了 {anchored.highlighted_blocks} 块，期望 1 块"
        )


#: 场景七的对话框里必须出现的内容。
#:
#: 前三项对应"让用户看清将要发出去的是什么"——查询原文在最前面，不是装饰：只显示 token
#: 数与价格，用户批准的就是一个自己看不见的字符串。最后两项是两个按钮：只有"批准"没有
#: "取消"的对话框不是同意，是通知。
CONSENT_PRESENT = [
    "允许这次语义检索？",
    "查询文本会离开本机",
    "将发送的查询",
    "预估 tokens",
    "预估成本",
    "批准并重新搜索",
    "取消",
]

#: 还没批准时页面上的提示条。它与对话框是一对：提示条负责"让你知道有这个选择"，
#: 对话框负责"让你看清再决定"。
CONSENT_BANNER = "可以启用语义检索"

#: 成本不得被渲染成零。
#:
#: 不能反过来断言"某个金额存在"——成本随 fixture 的 token 数与价目表变，写死一个数
#: 会让改 fixture 时莫名其妙地红。断言**不得出现零**才有意义：`lib/consent.ts::formatCost`
#: 刻意不用 `Intl.NumberFormat`，因为后者会把任何小于半分钱的东西显示成 `$0.00`，把
#: "很便宜"说成"免费"——而这个对话框存在的全部意义就是让用户看见价格。
#:
#: `$0.000001` 里的 `$0.00` 不算，所以后面不许再跟数字。
ZERO_COST = re.compile(r"\$0\.00(?![0-9])")


def check_consent(*, query: str, note: str, failures: list[str]) -> None:
    """同意对话框的内容断言。这一组是 B-8 的验收证据。

    URL 带 `consent=1` 打开对话框——`--dump-dom` 不会点击，所以"打开它"只能是一个可
    寻址的状态（与 `q` / `mode` 同一手法）。

    **"拒绝"这条路径这里验不到**：它需要一个点击，而这里没有点击可用。它的证据在
    `tests/integration/test_api_consent.py`（用 spy 数 `build_semantic_retriever`，那是
    查询离开本机的唯一一道门）与 `lib/consent.test.ts`（拒绝状态该显示什么）。刻意没有
    往 URL 里塞一个"已拒绝"的决定来凑场景——URL 记录不了用户还没做出的决定。

    断言的顺序有讲究：先证明结果真的渲染出来了，再谈对话框里的内容。否则"对话框里有
    预估成本"这句话在页面整体白屏时也可能因为别的原因成立。
    """
    label = "同意对话框"
    probe, retried = render_until(
        f"/search?q={query}&mode=hybrid&consent=1",
        lambda candidate: candidate.count_slots("dialog-content") == 1,
    )
    if retried:
        print(f"  （{label}：首次渲染没等到查询落地，重试一次后通过）")
    text = probe.body_text

    # 对话框不在 `id="main"` 里——Radix 把它 portal 到 `document.body`。所以下面几条用
    # 整页文本，与笔记页那条"内容区没有 <script>"的限定方式不同。
    dialogs = probe.count_slots("dialog-content")
    if dialogs != 1:
        failures.append(f"{label}: 渲染出 {dialogs} 个对话框，期望 1 个")

    for fragment in CONSENT_PRESENT:
        if fragment not in text:
            failures.append(f"{label}: 缺少 {fragment!r}；页面显示：{excerpt(text)}")

    if ZERO_COST.search(text):
        failures.append(
            f"{label}: 成本被渲染成了零（{ZERO_COST.pattern}），"
            "低于一微美元时应当说「不足 $0.000001」"
        )

    # 对话框开着的时候，后面的结果仍然是关键词检索的结果——那正是"还没批准"的样子。
    if note not in text:
        failures.append(f"{label}: 对话框后面没有结果 {note!r}；页面显示：{excerpt(text)}")

    # 提示条与"检索已降级"说的是同一件事，所以只该留前者：后者是红色告警，而用户能做的
    # 事只有提示条上那件（批准，或换模式）。两条同时在，页面就像两个部件在互相打架。
    if CONSENT_BANNER not in text:
        failures.append(f"{label}: 缺少同意提示条 {CONSENT_BANNER!r}")
    if "检索已降级" in text:
        failures.append(
            f"{label}: 不该出现「检索已降级」——「没批准」由提示条说明，告警留给真故障"
        )

    # 没有批准就什么都不该发出去。这个 fixture 没有 API key，所以"前端擅自批准过"的表现
    # 是结果区变成缺 Key 的错误，而不是悄悄成功。
    for fragment in ("缺少 API Key", "操作失败"):
        if fragment in text:
            failures.append(f"{label}: 不该出现 {fragment!r}——说明前端自己批准了这次查询")

    alerts = probe.count_slots("alert")
    if alerts != 1:
        failures.append(f"{label}: 有 {alerts} 条 alert，期望 1 条（只有同意提示条）")


def main() -> int:
    global BASE
    parser = argparse.ArgumentParser(description="无头浏览器逐路由核对 ObsAgent UI")
    parser.add_argument("--base-url", default=BASE, help=f"预览服务地址（默认 {BASE}）")
    parser.add_argument(
        "--state",
        choices=sorted(STATE_EXPECTATIONS),
        default="vault",
        help="被测后端处于哪种状态，决定概览页的期望内容（默认 vault）",
    )
    parser.add_argument(
        "--search-query",
        help="给了就额外核对搜索页：用这个词查询（需与 --search-note 同时给出）",
    )
    parser.add_argument(
        "--search-note",
        help="搜索该词时必须在结果里出现的笔记路径（需与 --search-query 同时给出）",
    )
    parser.add_argument(
        "--ask-hit",
        help="能检索到证据的问题；fixture 后端须以 OPENAI_API_KEY= 启动，"
        "这条路径因此必然是缺 Key 的引导（需与 --ask-miss 同时给出）",
    )
    parser.add_argument(
        "--ask-miss",
        help="检索不到任何证据的问题，页面应当弃答（需与 --ask-hit 同时给出）",
    )
    parser.add_argument(
        "--note-query",
        help="给了就额外核对笔记页：用这个词搜索，点进结果卡应当安全渲染 fixture 笔记",
    )
    parser.add_argument(
        "--consent-query",
        help="给了就额外核对同意对话框：用这个词查询，页面须停在对话框上"
        "（需与 --consent-note 同时给出）",
    )
    parser.add_argument(
        "--consent-note",
        help="对话框后面的关键词结果里必须出现的笔记路径（需与 --consent-query 同时给出）",
    )
    options = parser.parse_args()
    if (options.search_query is None) != (options.search_note is None):
        parser.error("--search-query 与 --search-note 必须同时给出")
    if (options.ask_hit is None) != (options.ask_miss is None):
        parser.error("--ask-hit 与 --ask-miss 必须同时给出")
    if (options.consent_query is None) != (options.consent_note is None):
        parser.error("--consent-query 与 --consent-note 必须同时给出")
    BASE = options.base_url.rstrip("/")
    state = options.state

    failures: list[str] = []

    # 先确认服务在跑，否则每个路由都要等一次 Chrome 冷启动才知道白忙。
    try:
        first = render("/")
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    if first.titles == []:
        failures.append("页面没有 <title>，可能根本没渲染出 React 树")
    if first.links == []:
        print("首页没有渲染出主导航。确认服务在跑：")
        print(f"  npm --prefix web run preview   # 期望监听 {BASE}")
        return 1

    print(f"场景：{state}   目标：{BASE}")
    print()
    print(f"{'路由':16} {'h1':14} {'高亮':8} {'侧栏项':6} title")
    print("-" * 72)
    for route, expected_heading, expected_active in CASES:
        probe = first if route == "/" else render(route)
        active = [link for link in probe.links if link["current"] == "page"]
        labels = [str(link["text"]) for link in probe.links]
        heading = probe.headings[0] if probe.headings else "(无 h1)"
        title = probe.titles[0] if probe.titles else "(无 title)"
        active_label = str(active[0]["text"]) if active else "—"
        print(f"{route:16} {heading:14} {active_label:8} {len(labels):<6} {title}")

        if heading != expected_heading:
            failures.append(f"{route}: h1 是 {heading!r}，期望 {expected_heading!r}")
        if len(active) > 1:
            failures.append(f"{route}: 有 {len(active)} 个 aria-current=page，应当只有一个")
        if expected_active is None:
            if active:
                failures.append(f"{route}: 不该点亮任何侧栏项，实际点亮了 {active_label!r}")
        elif active_label != expected_active:
            failures.append(f"{route}: 高亮的是 {active_label!r}，期望 {expected_active!r}")
        if labels != SIDEBAR:
            failures.append(f"{route}: 侧栏条目是 {labels}，期望 {SIDEBAR}")
        if not title.endswith("· ObsAgent"):
            failures.append(f"{route}: 标题 {title!r} 没有以 '· ObsAgent' 结尾")

    check_overview(first, state=state, failures=failures)

    # 侧栏状态灯证明 Vite 代理与 Host 校验都通了，同时它自己也是一条摘要：
    # 三种场景下它该说的话不同。
    lamp = STATE_EXPECTATIONS[state]["lamp"]
    if lamp not in first.body_text:
        failures.append(
            f"侧栏状态灯没有显示 {lamp!r}；检查后端是否在跑，"
            "以及预览服务的 /api 代理是否生效"
        )

    # 侧栏徽标：未实现的页面显示它属于哪一步，已实现的页面不显示。
    #
    # 两个方向都要查——只查"有徽标"的话，把某个页面误标成 ready 却忘了实现（页面
    # 会变成占位页）不会被发现；只查"没徽标"则反之。这份清单要随页面交付一起更新，
    # 更新它本身就是一次"侧栏状态和实现是否同步"的复核。
    for label, step in READY_STEPS:
        if step in first.body_text:
            failures.append(f"{label} 已实现，侧栏不该再显示 {step} 徽标")
    for label, step in PLANNED_STEPS:
        if step not in first.body_text:
            failures.append(f"{label} 未实现，侧栏应显示 {step} 徽标")

    # 搜索页的验收要真的跑一次查询，所以它需要知道"该找到什么"——由调用方给。
    if options.search_query is not None:
        print()
        print(f"搜索：{options.search_query!r} 应找到 {options.search_note!r}")
        check_search(
            query=options.search_query,
            note=options.search_note,
            failures=failures,
        )

    # 问答页同理：两条路径都要真的渲染出来才算数。
    if options.ask_hit is not None:
        print()
        print(
            f"问答：{options.ask_miss!r} 应弃答；"
            f"{options.ask_hit!r} 应给出缺 API Key 的引导"
        )
        check_ask(hit=options.ask_hit, miss=options.ask_miss, failures=failures)

    # 笔记页要从搜索结果点进去，所以它也必须真的跑一次查询。
    if options.note_query is not None:
        print()
        print(f"笔记：搜索 {options.note_query!r} → 结果卡 → 笔记页")
        check_note(query=options.note_query, failures=failures)

    # 同意对话框需要一个"语义腿可用、只是没人批准过"的后端，所以它是另一个 fixture
    # （见模块 docstring 场景七）。它也要真的跑一次查询——对话框显示的是这次查询的
    # token 数与成本，凭空渲染不出来。
    if options.consent_query is not None:
        print()
        print(f"同意：搜索 {options.consent_query!r} → 对话框（URL 带 consent=1）")
        check_consent(
            query=options.consent_query, note=options.consent_note, failures=failures
        )

    print()
    print("侧栏条目：", " / ".join(str(link["text"]) for link in first.links))
    if failures:
        print("\n失败：")
        for item in failures:
            print("  -", item)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
