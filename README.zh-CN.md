# ObsAgent CLI

本地优先（local-first）的 Obsidian CLI，具备 Markdown 解析、上下文感知分块、可重建的
SQLite 索引、混合检索，以及有证据边界的回答。`obsai index update` 用于同步已配置的 Vault。

> 本文件是 [README.md](README.md) 的简体中文译本，英文原版为唯一权威来源。

## 安装

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。克隆本仓库，然后运行
`uv sync --frozen`。用 `uv run obsai --help` 验证安装。

## 快速开始

```bash
uv sync
uv run obsai --help
uv run obsai --version
uv run obsai status
uv run obsai index update
uv run obsai index rebuild
uv run obsai search "context.WithTimeout" --mode keyword
uv run obsai index embeddings
uv run obsai search "服务怎么平滑退出？" --mode semantic
uv run obsai ask "我以前如何理解 graceful shutdown？"
uv run obsai note --help
uv run pytest
```

首次运行 `index embeddings` 会预览其远程调用成本并请求批准。搜索与解析在本地即可工作，
无需 API key；`ask` 与 Agent 需要显式配置的远程 provider 以及 `OPENAI_API_KEY`。

## 架构

Vault 是唯一真相源。只读解析器产出 `ParsedNote`；上下文感知分块产出稳定的内容哈希；
SQLite 存储派生元数据、FTS5 行，以及按代（generation）隔离的 sqlite-vec 向量。检索使用
RRF 融合 FTS 与向量结果。`ContextBuilder` 在 LLM 生成之前筛选出有边界的证据，并校验引用
ID。Agent 负责协调这些服务，但只有 safe-write 与 transaction 服务可以编辑 Vault 文件。

## 隐私模型

Markdown 解析、索引、关键词检索、图遍历与本地基准测试绝不会把 Vault 内容发送给
provider。经批准的 embedding 会发送选中的分块文本；经批准的语义搜索会发送查询；`ask`
会发送有边界的证据片段与查询。在批准远程调用或写入之前，请先审阅成本预览与 diff。
API key 来自环境变量，不会存储在 Vault 中，也不会出现在结构化指标里。

## 安全模型

Vault 中的 Markdown 是不可信数据。代码、HTML、JavaScript 与 Dataview 查询只会被当作文本
解析。检索到的证据在发给 LLM 的指令中被显式标注为不可信，只读的 Agent 意图不会仅因某条
笔记指示它去写而变成写入操作。每一次 Vault 写入都要求预览与人工批准，校验原始内容哈希，
限制在已配置的 Vault 根目录内，并拒绝符号链接穿越。多文件写入使用快照、回滚与持久化恢复
日志。已检查的边界与残留限制见[安全审查](docs/security-review.md)。

可选的配置文件为 `~/.config/obsai/config.toml`（或在设置了 `$XDG_CONFIG_HOME` 时使用
`$XDG_CONFIG_HOME/obsai/config.toml`）：

```toml
[vault]
path = "/path/to/vault"

[index]
database = "/path/to/index.db"
```

这两个字段在配置模型中都是可选的。当配置文件省略它们时，`OBSAI_VAULT__PATH` 与
`OBSAI_INDEX__DATABASE` 可提供取值。CLI 只读取配置文件，不会创建它。`index update`
需要 Vault 路径，并在省略 `index.database` 时于 `~/.obsai/index.db` 创建 SQLite 数据库。

## 只读的 Vault 解析

```python
from pathlib import Path
from obsai.vault import parse_vault, scan_markdown_files

vault = Path("/path/to/vault")
paths = scan_markdown_files(vault)
notes = parse_vault(vault)  # list[ParsedNote], sorted by vault-relative path
```

解析器读取 UTF-8 Markdown 以及可选的 YAML frontmatter。它提取标题、段落、列表、代码块、
WikiLink 与嵌入、标签、外部链接、callout、块引用（block reference），以及 Dataview 的
`key:: value` 字段。它绝不渲染 HTML，也绝不执行代码、Dataview 查询或 JavaScript。Vault
根目录下的 `.obsaiignore` 使用 gitignore 风格的模式；隐藏文件与符号链接文件也会被跳过。
被忽略的目录会在读取前被剪枝，因此在本阶段，否定规则无法把文件重新包含进已排除的目录中。

## 上下文感知分块

```python
from obsai.chunking import ChunkingOptions, chunk_note

chunks = chunk_note(notes[0], ChunkingOptions(min_tokens=80, target_tokens=260, max_tokens=400))
```

分块遵循笔记的标题层级，并在段落边界处切分过长的小节。围栏代码块、callout，或含有块 ID
的段落即使超过 `max_tokens` 也会保持完整；这样的分块带有
`metadata["oversized_atomic"] = True`。`raw_content` 保留不含注入标签的 Markdown 正文。
`embedding_text` 会加入标题与章节面包屑，但不加入文件系统路径。当首个 H1 与笔记标题相同
时，它会从该分块的 `Section` 标签中省略；`heading_path` 始终保留完整层级。`token_count`
是与模型无关的确定性估算，用于衡量 embedding 文本的大小。此处生成的分块带有临时的、
由路径派生的 ID；仓储层在建立索引时会将其重新绑定到持久化的笔记 ID。

## SQLite 元数据索引

```python
from pathlib import Path
from obsai.storage import Database, IndexRepository

with Database(Path("/path/to/index.db")) as db:
    index = IndexRepository(db)
    note_id = index.index_note(notes[0], chunk_note(notes[0]))
    stored_note = index.notes.get_parsed(note_id)
    stored_chunks = index.chunks.list_for_note(note_id)
    index.notes.update_path(note_id, "New/location.md")
```

首次插入会分配一个基于 UUID 的笔记 ID。用该 ID 调用 `update_path` 会保留它以及现有的分块
ID；后续重新索引时可向 `index_note` 传入 `note_id=note_id`。schema 使用
`PRAGMA user_version = 3`，并在每个连接上启用外键。版本 1 和 2 的数据库会自动迁移；版本 1
的数据库还会回填 FTS5 行。`IndexRepository.clear()` 会移除派生行以支持重建。
`created_at`、`modified_at` 与 `indexed_at` 是索引时间戳（UTC），而非文件系统的创建或修改
时间。所有数据库写入都留在仓储层内部；Vault 始终是唯一真相源。

## 增量更新

`obsai index update` 会对每个可见、未被忽略的 Markdown 文件计算哈希。未变化的文件不会被
重新解析或重新分块。一个已消失的已索引路径，与一个新出现的、具有唯一精确内容哈希匹配的
路径，会被报告为重命名或移动；它们的笔记 ID、分块 ID 与 embedding 文本哈希都会被保留。
内容相同的歧义匹配会被保守地视为删除加创建。变化的文件会被重新解析与重新分块，被删除的
文件会连同其派生行一起从索引中移除。该更新会报告重命名与移动所影响的 WikiLink，但绝不
修改 Vault 文件或反向链接。它不使用 watcher 事件，也不使用 `.obsidian/workspace.json`
作为真相源。标题仅来自旧文件名的笔记，在纯重命名后会保留其已索引的标题，以使其
embedding 文本保持稳定；当内容后续被重新索引时，其标题会被重新计算。

## 关键词检索

```bash
uv run obsai search "graceful shutdown" --mode keyword --limit 10
uv run obsai search "context.WithTimeout" --mode keyword --tag go --folder Backend --json
```

检索使用本地 SQLite FTS5，覆盖笔记标题、标题面包屑、分块正文与标签。Vault 路径用于文件夹
过滤并作为元数据返回；它们不会被索引为正文文本。查询会被转义为字面短语，因此查询中的
FTS 语法绝不会被执行。重复使用 `--tag` 选项要求同时包含所有标签。汉字还会被额外按单字
建立索引，以支持中文子串短语。结果包含分块与笔记 ID、路径、标题、标题路径、片段
（snippet）、分数以及 `source="keyword"`。索引写入、更新、删除与回滚都会在同一事务中维护
FTS 行。本阶段不使用 LLM 或语义检索。测试套件包含一个小的合成 P95 冒烟基准；10k 笔记 /
100k 分块的目标仍需在该规模下做性能剖析。

## 语义检索

先运行 `obsai index update`，再运行 `obsai index embeddings`。embedding 命令会打印需要
生成的唯一文本数量、缓存复用情况、保守的 token 上界、请求数量以及估算成本。使用 OpenAI
时，在环境中设置 `OPENAI_API_KEY`，并在把文本发送到远程服务前确认；使用 Ollama 时，
向量和查询都留在本机，不需要远程同意对话框。

要完整使用本地 Ollama，请启动 Ollama 并准备一个 embedding 模型和一个聊天模型，然后在
`obsai.toml` 中同时设置：

```toml
[embedding]
provider = "ollama"
model = "<ollama list 中的 embedding 模型>"
dimensions = <该模型的实际输出维度>

[ask]
provider = "ollama"
model = "<ollama list 中的聊天模型>"
```

默认本地端点为 `http://127.0.0.1:11434/v1`，也可以通过 `.env` 中的
`OLLAMA_BASE_URL` 或配置中的 `base_url` 覆盖。Ollama 本地兼容接口要求一个会被忽略的
占位 Key `ollama`，不需要 `OPENAI_API_KEY`。更换 embedding provider、模型或维度后，必须
重新生成向量索引。详见
[Ollama OpenAI 兼容接口文档](https://docs.ollama.com/api/openai-compatibility)。

可选配置字段为：

```toml
[embedding]
provider = "openai"
model = "text-embedding-3-small"
model_version = "text-embedding-3-small"
# base_url = "http://127.0.0.1:11434/v1"  # Ollama/OpenAI 兼容端点
dimensions = 1536
batch_size = 64
max_concurrency = 2
max_input_tokens = 8192
max_request_tokens = 300000
timeout_seconds = 30
max_attempts = 4
max_embedding_tokens = 1000000
estimated_cost_limit_usd = 1.0
max_embedding_requests = 1000
# price_per_million_tokens_usd = 0.02
```

`text-embedding-3-small` 的默认价格估算为每百万输入 token $0.02，依据
[OpenAI 官方模型定价](https://developers.openai.com/api/docs/models/text-embedding-3-small)。
当 provider 定价变化时请覆盖它。token 估算使用 UTF-8 字节长度作为保守上界：它可能同时
高估 provider 的 token 数与成本，但绝不会在单输入或单请求限制上静默少算。Provider、
model、model version、dimensions 与文本哈希共同定义缓存键。每个代（generation）都有独立
的 sqlite-vec `vec0` 表。若上游模型别名改变了其 embedding 空间，则必须更新
`model_version`。纯 Vault 重命名会保留现有的向量映射；变化的分块在其 embedding 文本哈希
匹配时可以复用此前缓存的 embedding。

## 混合检索

`obsai search "..."` 默认使用混合检索。它从 FTS5 与当前向量代请求候选分块，然后用倒数排序
融合（Reciprocal Rank Fusion，`k=60`）合并排名并返回 top 结果。`--mode keyword` 与
`--mode semantic` 选择单一路径。混合检索的 JSON 结果包含 `sources`，以显示某分块出现在
关键词检索、语义检索还是两者之中。`NoOpReranker` 保持融合后的顺序；`Reranker` 接口可在
不改变检索后端的前提下接入后续的重排序实现。

共享的元数据过滤器包括 `--folder`、可重复的 `--tag`、`--modified-after`、
`--modified-before`、可重复的 `--frontmatter key=value`，以及可重复的
`--dataview key=value`。frontmatter 过滤支持顶层标量值。`modified` 比较的是数据库的内容
索引时间戳，而非文件系统 mtime。Dataview 字段按存储的字符串比较；不会执行任何 Dataview
查询。

当没有向量代、用户拒绝远程查询 embedding，或语义后端失败时，混合检索会**可见地**回退到
关键词结果。警告会输出到 stderr，也可通过 `HybridRetriever.search_with_status().warnings`
获取。使用 `--strict-semantic` 可让其直接失败而非降级。仅语义模式在其后端不可用时始终
失败。关键词模式绝不调用 embedding provider。

位于 `tests/fixtures/retrieval/benchmark.json` 的小型基准数据集包含查询与期望的笔记路径。
`obsai.retrieval.evaluation.evaluate` 计算宏平均 Recall@K、MRR 与 Precision@K。由 mock
支撑的集成基准检查混合分数在 K=2 时不低于任一路径；它并不衡量真实的 OpenAI embedding
质量。

## 带引用的 Ask

`obsai ask "..."` 执行一次混合检索与一次 LLM 请求。它从 SQLite 加载所选分块的原始内容；
FTS 片段绝不会被用作证据。ContextBuilder 保留排序后的结果、去除重复的分块 ID，并同时
施加以下三项独立限制。发送给 provider 的证据带有 `[S1]`、`[S2]` 等标记，以及路径、标题、
标题层级与块元数据。CLI 只打印被采纳答案中引用到的来源。

```toml
[ask]
provider = "openai"
model = "gpt-4.1-mini"
# base_url = "http://127.0.0.1:11434/v1"  # Ollama/OpenAI 兼容端点
timeout_seconds = 60
max_output_tokens = 1024
max_context_tokens = 12000
max_evidence_tokens = 2500
max_chunks = 6
```

使用 OpenAI 时请设置 `OPENAI_API_KEY`；使用 Ollama 时无需 OpenAI 凭证。缺失或失败的语义
后端会被报告，并改用关键词结果。没有证据时，CLI 会在不调用 LLM 的情况下弃答。若模型返回
未知的引用 ID 或没有引用，其回答会被丢弃并显示弃答。限制使用 UTF-8 字节长度作为保守的、
离线的 token 上界，可能比模型分词器允许的证据更少。过长的证据会被可见地截断。引用检查只
校验来源 ID，而不校验每一句自然语言陈述是否忠实转述了其来源；prompt 要求给出有依据、带
引用的回答。测试会替换 LLM 适配器，不发起远程调用。Vault 保持只读。

## 安全的单笔记写入

所有 CLI 笔记变更都直接经由 `SafeWriteService`，或经由 `TransactionService`。
`obsai note create`、`update`、`move`、`trash` 与 `frontmatter` 会先打印 Rich 着色的
unified diff 并询问 yes/no 批准；默认值为否。`update` 替换一段精确文本，`frontmatter`
修改 YAML 头部并保留 Markdown 正文。审阅一次编辑：

```bash
uv run obsai note create "Go/new.md" --content "# New note"
uv run obsai note update "Go/context.md" --old "timeout: 5s" --new "timeout: 10s"
uv run obsai note frontmatter "Go/context.md" --set status=done
uv run obsai note move "Go/context.md" "Archive/context.md"
uv run obsai note trash "Archive/context.md"
```

该服务在准备阶段对源文件计算哈希，并在提交前立即再次校验。源文件被改动或消失会抛出
`ConflictError`，目标已存在会抛出 `CollisionError`。相对 Markdown 路径被限制在已配置的
Vault 内；符号链接组件与路径穿越会被拒绝。内容更新会在同一目录使用已 flush 且 fsync 的
临时文件，然后替换。trash 会把笔记移动到隐藏的 `.obsai-trash/` 目录，而不是删除它们。
单笔记的 create、update、trash 或 frontmatter 变更后，请运行 `obsai index update` 以刷新
派生的 SQLite 索引。

## 多文件事务与恢复

`TransactionService.plan([...])` 在不写入的情况下模拟一批 create、精确替换、frontmatter、
move 与 trash 操作。`preflight` 会在快照或编辑开始前检查每个原始哈希、路径、目标、所需
权限、设备与可用空间。批准后，该服务在 `.obsai-transactions/` 下写入短生命周期的字节快照
与持久化日志，通过 `SafeWriteService` 应用每项变更，验证最终文件，并在某个文件操作失败时
回滚已应用的变更。

```python
from obsai.transactions import TransactionOperation as Op, TransactionService

service = TransactionService(vault_path, database_path=index_path)
plan = service.plan([
    Op.move("Go/context.md", "Archive/context.md"),
    Op.frontmatter("Archive/context.md", {"status": "archived"}),
    Op.replace("Go/guide.md", "old wording", "new wording"),
])
service.preview(plan, console)
result = service.execute(plan, approved=True)
```

`obsai note move` 使用这条事务路径。它只重写解析器确认的、带显式 vault 根路径的
WikiLink，例如 `[[Go/context#Heading|Alias]]`。仅含 basename 的链接（如 `[[context]]`）、
代码/文本混排行以及其他不确定的目标保持不变并被报告。Vault 提交成功但随后的索引更新失败
时**不会**回滚：日志会记录 `index_dirty`，而 SQLite 仓储在可用时会标记受影响的路径为
dirty。`obsai index update` 会做协调并清除该状态。

被中断的运行会留下日志。CLI 会在下次调用时警告；在恢复之前，新的写入与重新索引都会被
阻止。使用 `obsai transaction status` 查看受影响的路径，使用
`obsai transaction recover ID` 预览 diff 并从快照确认回滚。恢复会拒绝覆盖不再匹配已知
事务状态的文件。

## 有边界的 Agent 工作流

`obsai agent run "搜索 context"` 会直接执行一次搜索，不进入规划循环。问题类请求使用既有的
混合检索、有边界的 `ContextBuilder`、LLM 回答与引用校验。读取、写入与整理请求会进入一个
LangGraph 工作流，从 `search_notes`、`read_note`、`get_backlinks`、`get_outgoing_links`、
`create_note`、`update_note`、`move_note`、`trash_note` 与 `update_frontmatter` 中选择。
OpenAI planner 需要 `OPENAI_API_KEY`；测试使用 mock planner，不发起远程调用。

该工作流会在 15 个工具步、5 次检索、3 次连续错误、2 次完全相同的工具调用，或 3 个无进展
的步之后停止。它会报告停止原因。checkpoint 状态存储笔记 ID、分块 ID 与 artifact 引用，
而非笔记正文或事务快照。工作流 checkpoint 与工具 artifact 存放在已配置索引旁边，即
`agent-checkpoints.db` 与 `agent-artifacts.db`。它们与 Vault 事务日志是两回事。

每个写入工具都会先创建事务计划并显示 diff。随后它在应用任何内容之前中断。用
`obsai agent resume WORKFLOW_ID` 恢复，再次审阅 diff 并回答 yes/no 提示。被拒绝的计划会
让 Vault 保持不变。批准后会通过 `TransactionService` 执行，它会重新校验源哈希，并按上文
所述处理回滚与索引失败。CLI 会在每个待批准项旁打印工作流 ID；如果需要预先指定的 ID，可向
`agent run` 传入 `--thread-id`。只要两个 agent SQLite 文件仍然可用，checkpoint 恢复就能
在进程重启后存活。

## Inbox 整理器

在 `obsai index update` 之后运行 `obsai organize inbox`。默认扫描 `Inbox/`；用以下配置
指定另一个相对于 Vault 的目录：

```toml
[organize]
inbox = "Capture"
```

如果配置的目录尚未创建，扫描会正常返回空提案，且不会自动创建该目录。

整理器会解析每条笔记，搜索已索引的相关笔记，并提出一个已存在的目标目录、基于标题的文件
名、来自相关笔记的标签，以及指向至多两条相关笔记的显式 WikiLink。它绝不创建新的分类
目录。如果相关笔记没有明确指向某一个目录，该笔记会留在 Inbox。单个弱关键词匹配会得到低
置信度，默认不会被选中。已存在的目标与重复的拟定文件名会被标记为冲突，无法应用。

初始界面是一份紧凑的提案摘要，包含置信度、理由与反向链接数量。选择 `a` 应用所有默认选中
的提案，`s` 选择提案编号，`v` 分页查看 diff，`q` 取消。一次应用选择对应一次 Vault 事务。
移动会保守地重写显式路径反向链接；歧义链接保持不变。任何 preflight 冲突或文件操作失败
都会停止或回滚整个选中的批次。该命令在批准之前绝不改动 Vault 文件。本版本的分类基于本地
FTS，因此依赖一个大致最新的索引。

## WikiLink 图与链接建议

图派生自既有的 SQLite `links` 行；它不新增图数据库。`GraphService.get_outgoing_links`、
`get_backlinks` 与 `get_neighbors` 会保留标题与块目标。未解析的链接仍作为断裂的出边可见，
但不会创建邻居节点。遍历是双向的、防环的，默认深度为 2，至多 50 个节点与 200 条边。

```bash
obsai links outgoing Go/context.md
obsai links backlinks Go/context.md
obsai links related Go/context.md --depth 2
obsai search "graceful shutdown" --mode graph
obsai links suggest Go/context.md
```

图检索使用既有的混合检索器获取种子笔记，然后加入有边界的 WikiLink 邻居，并为每条选取一个
相关的 Chunk。元数据过滤器同样适用于扩展出的笔记。图检索中的语义失败遵循既有的可见混合
降级策略；`--strict-semantic` 会让其成为错误。链接建议需要可用的向量代，并在把笔记查询
发送给 embedding provider 之前需要显式批准。它们会排除已有的出链，并用图的邻近度对语义
匹配排序。

`links suggest` 默认只读。`--apply` 会要求选择建议编号，显示事务 diff，并在通过安全事务
服务追加选中的 WikiLink 之前要求最终的 yes/no 确认。图结果反映的是上一次索引更新时的
状态，因此在外部编辑 Vault 后请重新索引。

## 索引重建

`obsai index rebuild` 在在线数据库旁构建 `index.db.building`，校验 SQLite 完整性、外键、
笔记数量与 FTS 行数，然后原子地替换在线索引。在交换之前失败或被中断的构建会让旧索引保持
可用，并移除不完整的影子索引。在最终交换期间收到信号会被延迟，直到已验证的新索引就位。
如果硬性 kill 进程后 `.building` 仍然存在，CLI 会警告；重新运行 rebuild 会替换该陈旧的
派生文件。重建前请关闭其他索引连接。重建会创建全新的元数据索引并丢弃旧向量，因此之后需
再次运行 `obsai index embeddings` 并审阅其成本预览。被中断的 Vault 事务另行通过
`obsai transaction status` 与恢复处理。

SIGINT 与 SIGTERM 会在安全边界处停止新的索引、embedding、Agent 与整理器工作。多文件
Vault 事务会完成当前的原子文件操作，回滚已应用的操作，并在回滚无法完成时留下恢复日志。
在 Vault 成功提交之后，被中断的索引更新会把索引标记为 dirty，而不是回滚 Vault。

## 配置

可选的配置文件为 `~/.config/obsai/config.toml`，或 `$XDG_CONFIG_HOME/obsai/config.toml`。
索引所需的最小配置是 `[vault] path`；`[index] database` 覆盖默认的 `~/.obsai/index.db`。
embedding 与整理器的示例见上文。设置 `OBSAI_LOG_LEVEL=INFO` 可输出结构化指标；也支持
`DEBUG`、`WARNING` 与 `ERROR`。指标事件包含计时与计数，不含笔记文本、查询、路径或
API key。

## 开发

运行 `uv sync --frozen` 安装锁定依赖。源码包位于 `src/obsai/`；单元测试与集成测试位于
`tests/`。本地基准使用临时合成 Vault 与确定性向量输入：

```bash
uv run python scripts/benchmark.py
uv run python scripts/benchmark.py --notes 100 --chunks-per-note 10 --vector-chunks 1000 --queries 10
```

默认工作负载为 10,000 条笔记与 100,000 个分块/向量。基准会报告 CLI 启动、FTS/向量/混合的
P50 与 P95、初始索引，以及一次未变化的增量更新。其向量计时不包含远程查询 embedding 与
网络延迟。硬件、磁盘、查询组合与过滤器都会影响结果；100 ms P95 目标是发布目标，而非
保证。10k/100k 的本地运行与测量限制记录在[基准报告](docs/benchmark-2026-09-14.md)中。

## 测试

运行 `uv run pytest -q`。测试使用临时 Vault 与 SQLite 数据库；provider 被 mock，因此绝不
使用真实的 HOME，也不发起远程调用。失败注入覆盖重建、事务与 embedding 期间的信号；解析
失败、磁盘失败、429、超时、OCC 冲突、回滚与索引失败。发布门槛要求所有测试通过、无已知
数据丢失缺陷、每次写入都有批准、OCC 与回滚可用、重建可恢复、Agent 执行有边界、引用经过
校验、有成本预览，以及本 README。
