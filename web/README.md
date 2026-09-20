# ObsAgent UI（前端）

`obsai-cli` 的本地只读 Web 界面。开发进度与步骤见
[`../docs/frontend-implementation-plan.zh-CN.md`](../docs/frontend-implementation-plan.zh-CN.md)。

## 技术栈

Vite 8 + React 19 + TypeScript 6 + Tailwind CSS 4 + shadcn/ui（`new-york-v4` 命名空间）
+ TanStack Query / Table + react-router。后端为 FastAPI，见 `../src/obsai/api/`。

## 常用命令

```bash
npm run dev        # 开发服务器（127.0.0.1:5173，/api 代理到 127.0.0.1:8000）
npm run build      # 类型检查 + 构建到 dist/
npm run preview    # 预览构建产物（127.0.0.1:4173，同样代理 /api）
npm run lint       # oxlint
npm run test       # vitest（纯逻辑，node 环境）
npm run clean      # 清理 dist/
```

在仓库根目录用 `./scripts/dev.sh` 可同时启动后端与前端。

## 目录约定

```
src/
├── lib/            纯逻辑，无 React，可在 node 环境单测
│   ├── navigation.ts   导航表：侧栏 / 路由 / 面包屑 / 标题的单一真相源
│   ├── status.ts       /status → 中文摘要
│   ├── search.ts       搜索页：模式解析、分数格式化、筛选器描述
│   ├── ask.ts          问答页：弃答分类、回答正文分段、降级提示过滤
│   ├── note.ts         笔记页：链接 href、标题层级下移、块分组、锚点定位
│   ├── consent.ts      远程同意：处于什么状态、价格怎么说、哪条提示该滤掉
│   ├── errors.ts       错误码 → 中文文案
│   └── api.ts          fetch 封装
├── hooks/          共享 hook
├── components/     业务组件（ui/ 是 registry 生成的，不要手改）
├── routes/         页面
├── router.tsx      路由表（由 navigation.ts 生成）
└── App.tsx         <RouterProvider>
```

**加一个页面**：只在 `lib/navigation.ts` 里加一个条目。路由、侧栏项、面包屑与页面标题
都从那张表派生，不需要改 `router.tsx`。页面实现好之后，把该条目的 `status` 改成
`'ready'` 并注册进 `router.tsx` 的 `READY_PAGES`——两处不同步会在启动时直接抛错。

**"读不到"和"没有"要分开写。** `/status` 是全函数：Vault 不可读时
`unfinished_transactions` 是空元组，索引打不开时 `dirty_notes` 是字段默认值。空集合
不等于"一切正常"。判断逻辑全部放在 `lib/status.ts`（纯函数、可单测），页面只负责渲染
——渲染层里写一句 `length === 0 ? '无' : ...` 就是在制造假安心。

## 端到端冒烟

`vitest` 只覆盖纯逻辑；真实渲染（路由切换、侧栏高亮、概览页内容）由仓库根目录的
`scripts/web-smoke.py` 用无头 Chromium 逐路由核对：

```bash
npm run build && npm run preview &
../.venv/bin/python ../scripts/web-smoke.py            # 场景一：已配置 Vault
```

它**必须打预览服务**：开发服务器的 HMR WebSocket 会让无头浏览器等不到网络空闲。

概览页在不同后端状态下渲染不同的内容，所以脚本按状态分场景。后两种需要一个隔离
`HOME` 的后端（读不到真实 `~/.config/obsai/config.toml`），再用 `OBSAI_UI_TARGET`
把第二个预览服务的 `/api` 指过去——`vite.config.ts` 的 `preview.proxy.target` 读这个
变量，因此同一份构建产物可以被多个后端复用：

```bash
# 场景二：后端未配置 Vault（B-4 的验收标准）
HOME=/tmp/obsai-novault-home .venv/bin/python -m uvicorn obsai.api.app:app --port 8001 &
OBSAI_UI_TARGET=http://127.0.0.1:8001 npm run preview -- --port 4174 &
../.venv/bin/python ../scripts/web-smoke.py --base-url http://127.0.0.1:4174 --state none

# 场景三：后端有待恢复的事务（造一份未完成的 journal 即可）
HOME=/tmp/obsai-recovery-home .venv/bin/python -m uvicorn obsai.api.app:app --port 8002 &
OBSAI_UI_TARGET=http://127.0.0.1:8002 npm run preview -- --port 4175 &
../.venv/bin/python ../scripts/web-smoke.py --base-url http://127.0.0.1:4175 --state recovery

# 场景四：搜索页真的跑一次查询（B-5 的验收标准）
# fixture：一个含 "redis" 笔记的 Vault + 已建索引，**没有向量**（所以会降级）
HOME=/tmp/obsai-search-home .venv/bin/python -m uvicorn obsai.api.app:app --port 8003 &
OBSAI_UI_TARGET=http://127.0.0.1:8003 npm run preview -- --port 4176 &
../.venv/bin/python ../scripts/web-smoke.py --base-url http://127.0.0.1:4176 --state search \
    --search-query redis --search-note notes/redis.md
```

场景三还顺带守住一条安全约束：journal 里带 `snapshot`（笔记原文），概览页只该列路径，
脚本会断言快照内容**没有**出现在页面里。完整的目录构造见脚本 docstring。

场景四能跑到结果列表，是因为**搜索页把查询串放在地址栏里**
（`#/search?q=redis&mode=keyword`）——`--dump-dom` 不会打字，纯本地 state 的页面在无头
浏览器里永远只能验到"输入框渲染出来了"。这个设计本身也是有用的：搜索可分享、可回链。
问答页同理（`#/ask?q=redis`）。

```bash
# 场景五：问答页的两条路径（B-6 的验收标准）
# 复用场景四的 fixture，但**显式清空 key**：空值让 provider 抛
# MissingCredentialError，"能检索到证据"那条路径因此必然是缺 Key 的引导。
# 不这样做的话，机器上恰好有 key 的人会真的发起一次远程调用。
HOME=/tmp/obsai-search-home OPENAI_API_KEY= \
    .venv/bin/python -m uvicorn obsai.api.app:app --port 8004 &
OBSAI_UI_TARGET=http://127.0.0.1:8004 npm run preview -- --port 4177 &
../.venv/bin/python ../scripts/web-smoke.py --base-url http://127.0.0.1:4177 --state search \
    --ask-hit redis --ask-miss zzzznotfound
```

两条路径都不需要网络：`--ask-miss` 在检索阶段就弃答，`--ask-hit` 在缺 Key 处停下。

```bash
# 场景六：笔记页（B-7 的验收标准）
# 复用场景四/五的 fixture，其中 notes/scripted.md 刻意含行内 <script>、整块 <script>、
# <iframe>、一个断链与一个 ^smoke-anchor 块锚点。
HOME=/tmp/obsai-search-home OPENAI_API_KEY= \
    .venv/bin/python -m uvicorn obsai.api.app:app --port 8004 &
OBSAI_UI_TARGET=http://127.0.0.1:8004 npm run preview -- --port 4177 &
../.venv/bin/python ../scripts/web-smoke.py --base-url http://127.0.0.1:4177 --state search \
    --note-query 冒烟
```

笔记场景**先**断言正文块渲染出来了（`data-slot="note-block"` 数量 > 0），再断言内容区里
没有 `script` / `iframe` / `img` / `object` / `embed` / `link` / `form`。顺序不能反——
否则"没有 `<script>`"可能只是因为页面是空的，这条断言会变成恒真。它靠搜索页取到
note ID，再渲染 `#/notes/<id>?block=smoke-anchor`，断言**恰好**一块被高亮。

**内容区按 `id="main"` 判定，不按标签名**：shadcn 的 `SidebarInset` 自己也渲染一个
`<main>`，把面包屑与图标一起包了进去，按标签名找会把页头算进内容区。

笔记页不执行任何脚本，**不是**因为前端做了过滤，而是因为 `/notes/{note_id}` 的响应里
根本没有原文——`NoteView` 不含 `raw_content`，`Block.content` 是解析器只保留可见文本的
产物：行内 `<script>` 只剩 `alert(1)` 这段字，独占一行的 HTML 块完全不产生块。详见
`../docs/frontend-implementation-plan.zh-CN.md` §23.2。

搜索页与问答页的检查允许**一次重试并会打印出来**：`--dump-dom` 靠固定虚拟时间预算截取
DOM，预览代理首次收到 POST 偶发赶不上（实测约 1/3）。路由循环那十条不重试——静态渲染
的重试只会掩盖真正的渲染回归。失败信息会附上页面正文，反向验证时正是靠它一眼看出页面
当时渲染的是弃答提示。

```bash
# 场景七：远程同意对话框（B-8 的验收标准）
# 要的是"语义腿可用、只是没人批准过"的后端。probe_semantic 判断语义腿能不能跑用的是
# store.has_generation()——**一次行查询**，不碰 provider，所以登记一行就够了：不需要
# key，也不需要网络。它是**另一个 HOME**，不能拿场景四那份直接用：场景四靠"没有
# generation"才看得到降级提示，种进去就把那条断言弄坏了。
CONSENT=/tmp/obsai-consent-home
cp -r /tmp/obsai-search-home $CONSENT
# 把 $CONSENT/.config/obsai/config.toml 里那两条绝对路径改指到 $CONSENT
HOME=$CONSENT .venv/bin/python -c "
from obsai.application.embedding import build_embedding_pipeline
from obsai.config.loader import load_settings
from obsai.storage import Database
settings = load_settings()
with Database(settings.index.database) as database:
    store, pipeline = build_embedding_pipeline(database, settings)
    store.ensure_generation(pipeline.generation)
"
HOME=$CONSENT OPENAI_API_KEY= .venv/bin/python -m uvicorn obsai.api.app:app --port 8005 &
OBSAI_UI_TARGET=http://127.0.0.1:8005 npm run preview -- --port 4178 &
../.venv/bin/python ../scripts/web-smoke.py --base-url http://127.0.0.1:4178 --state search \
    --consent-query redis --consent-note notes/redis.md
```

`--state search` 仍然是对的：这个 fixture 的索引与状态灯与场景四同形，多出来的只有一行
generation。`OPENAI_API_KEY=` 依旧是显式置空——万一有人真的点了批准，它必须失败得很大声，
而不是悄悄发一次远程调用。

对话框的开合也进 URL（`#/search?q=…&mode=…&consent=1`），理由与 `q` / `mode` 一样：
`--dump-dom` 不会点击。**"拒绝"这条路径浏览器验不到**——它需要一个点击，而这里没有点击
可用；证据在 `../tests/integration/test_api_consent.py`（spy 数 `build_semantic_retriever`，
那是查询离开本机的唯一一道门）与 `src/lib/consent.test.ts`。刻意**没有**为了凑一个场景而往
URL 里塞一个"已拒绝"的决定——URL 记录不了用户还没做出的决定。

注意对话框不在 `id="main"` 里：Radix 把它 portal 到 `document.body`。所以这一场景的断言
用整页文本，与笔记页那条"内容区没有 `<script>`"的限定方式不同。详见
`../docs/frontend-implementation-plan.zh-CN.md` §24。

## 环境限制

本环境**无法安装新的 npm 依赖**——WorkBuddy 的 Node FS 代理拦截 `fs.mkdir`，报
`CODEBUDDY_BROKER_DENY`（`npm install --package-lock-only` 可以，因为它不碰
`node_modules`）。因此这里没有 `jsdom` / Testing Library，组件级测试暂时由上面那个
冒烟脚本替代。详见 `../docs/frontend-implementation-plan.zh-CN.md` §19.5。

同一条规则也影响 Python 侧：pytest 的临时根目录（`$TMPDIR/pytest-of-*`）如果在**上一个
会话**里创建过，本会话对它调 `mkdir(exist_ok=True)` 会被拒，整个测试套件会报
`PermissionError: EEXIST ... mkdir`（表现为 549 errors）。解法是把那些目录挪走，或
`pytest --basetemp=/tmp/obsai-pytest`。详见 §20.6。

## 组件同步

`src/components/ui/**` 由 registry 生成，**不要手改**（会被覆盖）。
官方 `npx shadcn@latest` 在当前环境不可用，改用：

```bash
node scripts/sync-shadcn.mjs            # 同步默认组件列表
node scripts/sync-shadcn.mjs button card
```

脚本会打印需要安装的 npm 依赖。装依赖请用两步法，避免中途失败导致 package.json
未更新、下次安装把已装好的包剪掉：

```bash
npm install --package-lock-only <包名...>
npm install
```

> 注意：上面这条在**当前环境**走不通（见「环境限制」）。新增运行时依赖前请先确认。

主题变量在 `src/index.css`，取自 registry 的 `colors/neutral.json`（oklch）。
要调色请改这里，不要改组件文件。
