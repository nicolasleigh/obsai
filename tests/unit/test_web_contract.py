"""B-2 验收：前端声明的东西必须和服务端一致。

前端有两条"只能靠人记得同步"的声明：

1. **中文错误目录**（`web/src/lib/errors.ts`）—— 漏一个错误码不会报错，只会让用户看到
   一句「操作失败」加一段英文。没有任何信号。
2. **DTO 的字段名**（`web/src/lib/api-types.ts`）—— TS 类型只在编译期存在。服务端把
   `vault_ready` 改名后，前端**照样能编译通过**，只在运行时表现为 `undefined`；
   而"拼错字段名会在 tsc 阶段暴露"这句话只对前端自己写的代码成立，对服务端的改动不成立。

两条都做成常驻断言。放在 Python 侧是因为只有服务端知道自己能发什么码、返回什么字段；
而且 §12 的铁律要求每个里程碑都跑 pytest，这样它一定会被跑到。

断言是双向的：正向保证"服务端有的前端都有"，反向保证"前端有的服务端真的会发"。
只有正向会漏掉拼错的键，只有反向会漏掉未翻译的新码。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, get_args

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.api.deps import get_settings
from obsai.api.errors import error_code
from obsai.application.dto import JobStatus, JobView
from obsai.application.embedding_jobs import EMBEDDING_KIND, STEP_EMBEDDING
from obsai.application.index_jobs import (
    EMBEDDINGS_STALE,
    INDEX_REBUILD_KIND,
    INDEX_UPDATE_KIND,
    STEP_BUILDING,
    STEP_DONE,
    STEP_SCANNING,
    STEP_SYNCHRONIZING,
    index_rebuild_job,
    index_update_job,
)
from obsai.application.jobs import JobCallable, JobStore, job_journal_path
from obsai.config.models import Settings
from obsai.errors import ObsAIError, RecoveryRequiredError
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository

# 导入即注册为 ObsAIError 的子类，下面的子类树遍历依赖它。
import obsai.application.locks  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
ERRORS_TS = REPO / "web" / "src" / "lib" / "errors.ts"
TYPES_TS = REPO / "web" / "src" / "lib" / "api-types.ts"
JOBS_TS = REPO / "web" / "src" / "lib" / "jobs.ts"
ADAPTER_SOURCES = (
    REPO / "src" / "obsai" / "api" / "errors.py",
    REPO / "src" / "obsai" / "api" / "security.py",
)

# --------------------------------------------------------------------------- #
# 错误码
# --------------------------------------------------------------------------- #

#: 只可能由浏览器产生的码：服务端永远不会发它们，但目录里必须有。
FRONTEND_ONLY_CODES = frozenset({"offline", "unparsable_response", "cancelled"})

#: `CATALOGUE` 里一条记录的形状：两空格缩进 + 小写键 + `: {`。
#: 这个形状在 `errors.ts` 里只出现在目录中（`FALLBACK` 与两个类型别名都不匹配）。
CATALOGUE_ENTRY = re.compile(r"^  ([a-z][a-z0-9_]*): \{$", re.MULTILINE)

#: 适配层显式指定的码：`code="invalid_host"` 与 `envelope("http_error", ...)` 两种写法。
EXPLICIT_CODE = re.compile(r'(?:code=|envelope\()\s*"([a-z][a-z0-9_]*)"')


def catalogue_keys() -> set[str]:
    assert ERRORS_TS.is_file(), f"前端错误目录不见了：{ERRORS_TS}"
    return set(CATALOGUE_ENTRY.findall(ERRORS_TS.read_text(encoding="utf-8")))


def error_subclasses() -> set[type[BaseException]]:
    """``ObsAIError`` 的整棵子类树，含尚未在 `STATUS_BY_ERROR` 里登记的。"""
    found: set[type[BaseException]] = set()
    pending: list[type[BaseException]] = [ObsAIError]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(current.__subclasses__())
    return found


def server_codes() -> set[str]:
    """服务端**可能**发出的所有码。"""
    codes = {error_code(cls("probe")) for cls in error_subclasses()}
    for source in ADAPTER_SOURCES:
        codes |= set(EXPLICIT_CODE.findall(source.read_text(encoding="utf-8")))
    return codes


def test_the_catalogue_is_not_empty() -> None:
    """先证明解析确实读到了东西，否则下面两条断言会因为空集而恒真。"""
    keys = catalogue_keys()
    assert len(keys) > 15, keys
    assert {"config", "recovery_required", "lock_busy"} <= keys


def test_the_domain_tree_is_walked_not_hardcoded() -> None:
    """子类遍历要真的覆盖到各层，否则正向检查会漏。"""
    names = {cls.__name__ for cls in error_subclasses()}
    assert {"ObsAIError", "ConfigError", "RecoveryRequiredError", "LockBusyError"} <= names
    # 三层继承：RecoveryRequiredError → TransactionError → SafeWriteError → ObsAIError。
    # 走得对，才能把它和同样继承链上的父类区分开。
    assert error_code(RecoveryRequiredError("x")) == "recovery_required"


@pytest.mark.parametrize("code", sorted(server_codes()))
def test_every_server_code_has_a_chinese_message(code: str) -> None:
    assert code in catalogue_keys(), (
        f"服务端会发出 {code!r}，但 web/src/lib/errors.ts 的目录里没有它；"
        "用户会看到「操作失败」加一段英文"
    )


def test_the_catalogue_has_no_unknown_keys() -> None:
    """拼错的键不会被正向检查发现，所以反向也要查。"""
    allowed = server_codes() | FRONTEND_ONLY_CODES
    assert not (unknown := catalogue_keys() - allowed), (
        f"目录里有服务端不会发出的码：{sorted(unknown)}；"
        "要么是拼错了，要么该把它加进 FRONTEND_ONLY_CODES"
    )


def test_the_frontend_only_codes_are_still_used() -> None:
    """`FRONTEND_ONLY_CODES` 是豁免名单；目录里删了条目后它必须跟着收缩，否则会
    一直为不存在的键背书。"""
    assert FRONTEND_ONLY_CODES <= catalogue_keys()


# --------------------------------------------------------------------------- #
# DTO 字段名
# --------------------------------------------------------------------------- #

#: `export type Name = { ... }`，也接受 `= Other & { ... }` 这种交叉写法。
TYPE_BLOCK = re.compile(r"export type (\w+) = ([\w\s&]*)\{([^}]*)\}")

#: 类型体里一行字段声明。注释行以 `/` 开头，不会误匹配。
TYPE_FIELD = re.compile(r"^\s{2}([A-Za-z_][A-Za-z0-9_]*)\??:", re.MULTILINE)



def declared_fields(type_name: str, _seen: frozenset[str] = frozenset()) -> set[str]:
    """``type_name`` 声明的字段，**含交叉继承来的**。

    `StatusResponse = StatusView & {...}` 必须展开成两者之和：只看字面量的话，顶层
    断言会把 `StatusView` 的九个字段全判成"服务端多给的"，测试变成一句假警报。
    """
    blocks = {
        name: (base, body)
        for name, base, body in TYPE_BLOCK.findall(TYPES_TS.read_text(encoding="utf-8"))
    }
    assert type_name in blocks, f"web/src/lib/api-types.ts 里找不到 {type_name}"
    base, body = blocks[type_name]
    fields = set(TYPE_FIELD.findall(body))
    assert fields, f"{type_name} 解析出 0 个字段，解析规则可能失效了"
    for parent in re.findall(r"\w+", base):
        if parent not in _seen and parent in blocks:
            fields |= declared_fields(parent, _seen | {type_name})
    return fields


def live_status(tmp_path: Path) -> dict:
    """一个"什么都有"的 `/status` 响应：Vault、索引、脏笔记、未完成事务。

    形状与状态无关，但只有真造出这些状态，嵌套的 `JournalView` / `DirtyNoteView`
    才会出现在响应里——否则那两条断言会在空数组上恒真。
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n\nBody.\n", encoding="utf-8")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
        IndexRepository(database).mark_dirty("A.md", "note changed")

    journal = vault / ".obsai-transactions" / "t1"
    journal.mkdir(parents=True)
    (journal / "journal.json").write_text(
        json.dumps(
            {"id": "t1", "status": "applying", "originals": [{"path": "A.md", "snapshot": None}]}
        ),
        encoding="utf-8",
    )

    settings = Settings(vault={"path": vault}, index={"database": database_path})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.get("/api/v1/status")
    assert response.status_code == 200, response.text
    return response.json()


def test_the_status_response_has_exactly_the_declared_fields(tmp_path: Path) -> None:
    payload = live_status(tmp_path)
    assert set(payload) == declared_fields("StatusResponse"), (
        "GET /api/v1/status 的顶层字段与 web/src/lib/api-types.ts 的 StatusResponse 不一致"
    )


def test_the_nested_status_shapes_match(tmp_path: Path) -> None:
    payload = live_status(tmp_path)
    assert set(payload["index"]) == declared_fields("IndexStatusView")

    unfinished = payload["unfinished_transactions"]
    assert unfinished, "测试数据没造出未完成事务，这条断言会恒真"
    assert set(unfinished[0]) == declared_fields("JournalView")
    assert set(unfinished[0]["originals"][0]) == declared_fields("JournalOriginal")

    dirty = payload["index"]["dirty_notes"]
    assert dirty, "测试数据没造出脏笔记，这条断言会恒真"
    assert set(dirty[0]) == declared_fields("DirtyNoteView")


def test_the_intersection_type_is_expanded_not_ignored() -> None:
    """`StatusResponse = StatusView & {...}`：解析器要能穿过交叉写法。

    如果它只认 `= {`，`declared_fields("StatusResponse")` 会直接抛断言而不是静默返回
    一个空集——这条测试把那个行为钉住。
    """
    assert declared_fields("StatusResponse") == declared_fields("StatusView") | {
        "started_at",
        "uptime_seconds",
    }


def test_the_health_response_matches() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        payload = client.get("/api/v1/health").json()
    assert set(payload) == declared_fields("HealthResponse")


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


def build_search_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A two-note Vault with a keyword-only index."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Backend").mkdir()
    (vault / "Backend" / "Redis.md").write_text(
        "---\ntags: [redis]\n---\n# Redis\n\nRedis cache strategy.\n", encoding="utf-8"
    )
    (vault / "Guides").mkdir()
    (vault / "Guides" / "Deploy.md").write_text("# Deploy\n\nSee [[Backend/Redis]].\n", encoding="utf-8")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
    return vault, database_path


def client_with(vault: Path, database_path: Path) -> TestClient:
    settings = Settings(vault={"path": vault}, index={"database": database_path})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, base_url="http://127.0.0.1:8000")


def test_the_search_response_has_exactly_the_declared_fields(tmp_path: Path) -> None:
    vault, database_path = build_search_vault(tmp_path)
    with client_with(vault, database_path) as client:
        payload = client.post("/api/v1/search", json={"query": "redis", "mode": "keyword"}).json()
    assert set(payload) == declared_fields("SearchResponse"), (
        "POST /api/v1/search 的顶层字段与 web/src/lib/api-types.ts 的 SearchResponse 不一致"
    )


def test_the_search_result_shape_matches(tmp_path: Path) -> None:
    vault, database_path = build_search_vault(tmp_path)
    with client_with(vault, database_path) as client:
        payload = client.post("/api/v1/search", json={"query": "redis", "mode": "keyword"}).json()

    assert payload["results"], "测试数据没造出结果，这条断言会在空列表上恒真"
    assert set(payload["results"][0]) == declared_fields("SearchResult")


def test_the_semantic_probe_shape_matches_when_degraded(tmp_path: Path) -> None:
    """``semantic`` 非 null 时（hybrid 降级），它的字段必须与 ``SemanticProbe`` 一致。"""
    vault, database_path = build_search_vault(tmp_path)
    with client_with(vault, database_path) as client:
        payload = client.post("/api/v1/search", json={"query": "redis", "mode": "hybrid"}).json()

    assert payload["semantic"] is not None, "hybrid 模式应该返回 probe"
    assert set(payload["semantic"]) == declared_fields("SemanticProbe")


def test_the_remote_consent_shape_matches_when_available(tmp_path: Path) -> None:
    """语义后端可用时 ``consent`` 非 null，其字段必须与 ``RemoteConsent`` 一致。"""
    vault, database_path = build_search_vault(tmp_path)
    with Database(database_path) as database:
        from obsai.application.embedding import build_embedding_pipeline

        store, pipeline = build_embedding_pipeline(
            database, Settings(vault={"path": vault}, index={"database": database_path})
        )
        store.ensure_generation(pipeline.generation)

    with client_with(vault, database_path) as client:
        payload = client.post("/api/v1/search", json={"query": "redis", "mode": "hybrid"}).json()

    assert payload["semantic"]["consent"] is not None, "generation 已注册，应该给出 consent"
    assert set(payload["semantic"]["consent"]) == declared_fields("RemoteConsent")


def test_the_embedding_plan_shape_matches(tmp_path: Path) -> None:
    vault, database_path = build_search_vault(tmp_path)
    with client_with(vault, database_path) as client:
        payload = client.post("/api/v1/embedding/plans").json()
    assert set(payload) == declared_fields("EmbeddingPlanView"), (
        "POST /api/v1/embedding/plans 的字段与 web/src/lib/api-types.ts 的 EmbeddingPlanView 不一致"
    )


# --------------------------------------------------------------------------- #
# Ask
# --------------------------------------------------------------------------- #


def test_the_ask_response_has_exactly_the_declared_fields(tmp_path: Path, stub_llm) -> None:
    """`AskResponse = AskOutcome & {...}`，所以这一条同时覆盖了 `AskOutcome`。

    `SemanticProbe` 的形状由上面的搜索用例负责——两个端点返回的是同一个类，
    再断言一遍只会让两处一起腐坏。
    """
    vault, database_path = build_search_vault(tmp_path)
    stub_llm("Redis 是内存缓存。[S1]")
    with client_with(vault, database_path) as client:
        payload = client.post("/api/v1/ask", json={"query": "redis"}).json()
    assert set(payload) == declared_fields("AskResponse"), (
        "POST /api/v1/ask 的顶层字段与 web/src/lib/api-types.ts 的 AskResponse 不一致"
    )


def test_the_citation_shape_matches(tmp_path: Path, stub_llm) -> None:
    """嵌套的 `CitationView` 也要查，否则它改名只会在页面上表现为 `undefined`。"""
    vault, database_path = build_search_vault(tmp_path)
    stub_llm("Redis 是内存缓存。[S1]")
    with client_with(vault, database_path) as client:
        payload = client.post("/api/v1/ask", json={"query": "redis"}).json()

    assert payload["citations"], "测试数据没造出引用，这条断言会在空列表上恒真"
    assert set(payload["citations"][0]) == declared_fields("CitationView")


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #

#: 笔记页的入口是 note ID，而 ID 由索引分配，所以测试得先从索引里取一个。
#: 两篇各司其职：`Redis.md` 没有出链，`Deploy.md` 有一条指向它的链接。
NOTE_PATH = "Backend/Redis.md"
LINKING_NOTE_PATH = "Guides/Deploy.md"


def note_id_of(database_path: Path, path: str) -> str:
    with Database(database_path) as database:
        record = IndexRepository(database).notes.get_by_path(path)
    assert record is not None, f"{path} 没有被索引到，后面的断言都会失去意义"
    return record.id


def note_payload(tmp_path: Path, path: str = NOTE_PATH) -> dict:
    vault, database_path = build_search_vault(tmp_path)
    with client_with(vault, database_path) as client:
        response = client.get(f"/api/v1/notes/{note_id_of(database_path, path)}")
    assert response.status_code == 200, response.text
    return response.json()


def test_the_note_response_has_exactly_the_declared_fields(tmp_path: Path) -> None:
    """`GET /api/v1/notes/{id}` 的顶层字段与 `NoteView` 一致。

    这里没有 `NoteResponse`：`/status`、`/search`、`/ask` 三个端点的响应都比应用层
    DTO 多一两个字段（进程事实、probe），所以各有一个 `*Response` 包一层。笔记没有
    可加的字段，多一个只做改名的类型就是同一个形状的两个名字。
    """
    payload = note_payload(tmp_path)

    assert set(payload) == declared_fields("NoteView"), (
        "GET /api/v1/notes/{id} 的顶层字段与 web/src/lib/api-types.ts 的 NoteView 不一致"
    )


def test_the_note_block_shape_matches(tmp_path: Path) -> None:
    """块形状，含 `segments` 这个名字——它在渲染器里是唯一的正文来源。"""
    payload = note_payload(tmp_path)

    assert payload["blocks"], "测试数据没造出块，这条断言会在空列表上恒真"
    assert set(payload["blocks"][0]) == declared_fields("NoteBlockView")


def test_the_note_segment_shapes_match(tmp_path: Path) -> None:
    """两种段共用一张字段表，所以两个分支都要走到。

    只查 `link` 段是不够的：`text` 段是多数，而 `target_note_id` 拼错时只有 `link` 段
    会露出来——反过来，只有 `link` 段被查的话，`text` 段的字段改名没人发现。
    """
    payload = note_payload(tmp_path, LINKING_NOTE_PATH)
    segments = [segment for block in payload["blocks"] for segment in block["segments"]]

    kinds = {segment["kind"] for segment in segments}
    assert kinds == {"text", "link"}, f"测试数据没造出两种段，实际是 {kinds}"
    for segment in segments:
        assert set(segment) == declared_fields("NoteSegment")


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def test_the_job_shape_matches(tmp_path: Path) -> None:
    """`GET /api/v1/jobs` 的每个元素必须与 `JobView` 逐字段一致。

    先真的存一条任务记录，否则断言会在空列表上恒真——`live_status` 存在的理由也是这个。
    这里走 `JobStore` 而不是提交一个真任务：要测的是**序列化形状**，不是任务能不能跑。
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n\nBody.\n", encoding="utf-8")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
    with JobStore(job_journal_path(database_path)) as store:
        store.save(
            JobView(
                job_id="j1",
                kind="embedding",
                status="succeeded",
                created_at=NOW,
                started_at=NOW,
                finished_at=NOW,
                message="done",
                detail={"notes": 3},
            )
        )

    settings = Settings(vault={"path": vault}, index={"database": database_path})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        payload = client.get("/api/v1/jobs").json()

    assert payload, "测试数据没造出任务，这条断言会在空列表上恒真"
    assert set(payload[0]) == declared_fields("JobView"), (
        "GET /api/v1/jobs 的元素字段与 web/src/lib/api-types.ts 的 JobView 不一致"
    )


# --------------------------------------------------------------------------- #
# 任务的词表
# --------------------------------------------------------------------------- #

#: 三张词表在 `jobs.ts` 里的声明名。任务记录里没有一个给人看的词，所以这三张表是
#: 用户唯一会看到的文案——漏一条不会报错，只会显示一个英文标识符。
JOB_KIND_TABLE = "JOB_KIND_LABELS"
JOB_STATUS_TABLE = "JOB_STATUS_LABELS"
JOB_STEP_TABLE = "JOB_STEP_LABELS"

#: `export const NAME…= { 'key': '值', ... }`，键的引号可有可无（连字符必须加）。
RECORD_ENTRY = re.compile(r"^\s{2}'?([A-Za-z][A-Za-z0-9_-]*)'?: '([^']*)',", re.MULTILINE)

#: `const NAME…= [ 'a', 'b', ]` 这种字符串数组。
STRING_ARRAY = re.compile(r"^const (\w+)[^=]*= \[(.*?)\]", re.MULTILINE | re.DOTALL)
ARRAY_ITEM = re.compile(r"'([^']+)'")

#: `const NAME = 'value'`。
STRING_CONST = re.compile(r"^const (\w+) = '([^']*)'", re.MULTILINE)

#: `['key', '标签']` 这种二元组列表。
PAIR_ITEM = re.compile(r"\['([A-Za-z_][A-Za-z0-9_]*)', '([^']*)'\]")


def jobs_source() -> str:
    assert JOBS_TS.is_file(), f"前端任务词表不见了：{JOBS_TS}"
    return JOBS_TS.read_text(encoding="utf-8")


def ts_record(name: str) -> dict[str, str]:
    """`export const NAME…= { ... }` 的键值对。"""
    source = jobs_source()
    match = re.search(rf"export const {name}[^=]*= \{{(.*?)\n\}}", source, re.DOTALL)
    assert match, f"web/src/lib/jobs.ts 里找不到 {name}"
    entries = dict(RECORD_ENTRY.findall(match.group(1)))
    assert entries, f"{name} 解析出 0 条，解析规则可能失效了"
    return entries


def ts_string_array(name: str) -> set[str]:
    source = jobs_source()
    match = re.search(rf"^const {name}[^=]*= \[(.*?)\]", source, re.MULTILINE | re.DOTALL)
    assert match, f"web/src/lib/jobs.ts 里找不到 {name}"
    items = set(ARRAY_ITEM.findall(match.group(1)))
    assert items, f"{name} 解析出 0 条，解析规则可能失效了"
    return items


def ts_pair_keys(name: str) -> list[str]:
    source = jobs_source()
    match = re.search(rf"^const {name}[^=]*= \[(.*?)\n\]", source, re.MULTILINE | re.DOTALL)
    assert match, f"web/src/lib/jobs.ts 里找不到 {name}"
    keys = [key for key, _ in PAIR_ITEM.findall(match.group(1))]
    assert keys, f"{name} 解析出 0 条，解析规则可能失效了"
    return keys


def ts_string_const(name: str) -> str:
    match = re.search(rf"^const {name} = '([^']*)'", jobs_source(), re.MULTILINE)
    assert match, f"web/src/lib/jobs.ts 里找不到 {name}"
    return match.group(1)


class RecordingContext:
    """任务真正用到的只有 `progress`，所以替身只需要它。"""

    def __init__(self) -> None:
        self.steps: list[str] = []

    def progress(self, message: str, **_detail: Any) -> None:
        self.steps.append(message)


def run_job(factory: Callable[[Settings], JobCallable], vault: Path, index_path: Path):
    context = RecordingContext()
    detail = factory(Settings(vault={"path": vault}, index={"database": index_path}))(context) or {}
    return context, detail


@pytest.fixture()
def one_note_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n\nBody.\n", encoding="utf-8")
    return vault


def test_the_job_tables_are_not_empty() -> None:
    """先证明解析读到了东西，否则下面的双向断言会在空集上恒真。"""
    assert len(ts_record(JOB_KIND_TABLE)) >= 2
    assert len(ts_record(JOB_STATUS_TABLE)) >= 7
    assert len(ts_record(JOB_STEP_TABLE)) >= 4


def test_every_job_kind_has_a_chinese_name() -> None:
    table = ts_record(JOB_KIND_TABLE)
    for kind in (INDEX_UPDATE_KIND, INDEX_REBUILD_KIND, EMBEDDING_KIND):
        assert kind in table, (
            f"服务端会提交 {kind!r} 任务，但 web/src/lib/jobs.ts 的 {JOB_KIND_TABLE} 里没有它"
        )


def test_the_kind_table_has_no_unknown_entries() -> None:
    unknown = set(ts_record(JOB_KIND_TABLE)) - {INDEX_UPDATE_KIND, INDEX_REBUILD_KIND, EMBEDDING_KIND}
    assert not unknown, (
        f"{JOB_KIND_TABLE} 里有服务端不会提交的 kind：{sorted(unknown)}；"
        "要么是拼错了，要么该在后端补上对应的常量"
    )


def test_every_job_status_has_a_chinese_name() -> None:
    table = ts_record(JOB_STATUS_TABLE)
    assert set(get_args(JobStatus)) == set(table), (
        "web/src/lib/jobs.ts 的 JOB_STATUS_LABELS 与 dto.py 的 JobStatus 不是同一套；"
        f"服务端多出来的是 {sorted(set(get_args(JobStatus)) - set(table))}"
    )


def test_every_job_step_has_a_chinese_sentence() -> None:
    table = ts_record(JOB_STEP_TABLE)
    declared = {STEP_SCANNING, STEP_SYNCHRONIZING, STEP_BUILDING, STEP_DONE, STEP_EMBEDDING}
    assert declared == set(table), (
        "web/src/lib/jobs.ts 的 JOB_STEP_LABELS 与 application/index_jobs.py 的步骤常量"
        f"不是同一套；服务端多出来的是 {sorted(declared - set(table))}"
    )


def test_the_steps_a_real_run_reports_are_all_translated(
    one_note_vault: Path, tmp_path: Path
) -> None:
    """上一条比的是常量，这一条比的是**真的会发出去的那几个**。"""
    table = ts_record(JOB_STEP_TABLE)
    context, _ = run_job(index_update_job, one_note_vault, tmp_path / "index.db")
    assert context.steps, "任务一个步骤都没报，这条断言会恒真"
    assert set(context.steps) <= set(table), f"未翻译的步骤：{sorted(set(context.steps) - set(table))}"


def test_the_terminal_statuses_agree_with_the_server() -> None:
    """前端自己推导 `terminal`（它是 pydantic 的 property，不进 JSON），所以要钉住。"""
    derived = {
        status for status in get_args(JobStatus) if JobView(
            job_id="j", kind="probe", status=status, created_at=datetime.now(timezone.utc)
        ).terminal
    }
    assert derived == ts_string_array("TERMINAL_STATUSES")


def test_the_embeddings_marker_matches_the_server() -> None:
    assert ts_string_const("EMBEDDINGS_STALE") == EMBEDDINGS_STALE


def test_the_count_labels_cover_exactly_what_the_jobs_report(
    one_note_vault: Path, tmp_path: Path
) -> None:
    """计数键是**实测**出来的，不是抄常量：一条真跑过的任务报什么，页面就得认识什么。"""
    index_path = tmp_path / "index.db"
    _, updated = run_job(index_update_job, one_note_vault, index_path)
    _, rebuilt = run_job(index_rebuild_job, one_note_vault, index_path)

    labelled = set(ts_pair_keys("COUNT_FIELDS"))
    assert set(updated) == labelled, (
        "任务的 detail 与 web/src/lib/jobs.ts 的 COUNT_FIELDS 不一致；"
        f"服务端多出来的是 {sorted(set(updated) - labelled)}，"
        f"前端多出来的是 {sorted(labelled - set(updated))}"
    )
    assert set(rebuilt) == labelled | {EMBEDDINGS_STALE}


# --------------------------------------------------------------------------- #
# Changes & Transactions
# --------------------------------------------------------------------------- #


def test_the_change_plan_shapes_match(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n\nBody.\n", encoding="utf-8")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)

    with client_with(vault, database_path) as client:
        payload = client.post(
            "/api/v1/changes/plans",
            json={
                "operations": [
                    {
                        "kind": "replace",
                        "path": "A.md",
                        "old": "Body.",
                        "new": "New Body.",
                    }
                ]
            },
        ).json()

        assert set(payload) == declared_fields("ChangePlanView")
        assert payload["changes"], "测试数据没造出变更，断言会在空列表上恒真"
        assert set(payload["changes"][0]) == declared_fields("PlannedChange")
        assert payload["diff_summary"], "测试数据没造出 diff_summary，断言会在空列表上恒真"
        assert set(payload["diff_summary"][0]) == declared_fields("DiffSummaryItem")
        assert payload["diff"], "测试数据没造出 diff，断言会在空列表上恒真"
        assert set(payload["diff"][0]) == declared_fields("DiffLine")

        approve_payload = client.post(
            f"/api/v1/changes/plans/{payload['plan_id']}/approve",
            json={
                "revision": payload["revision"],
                "nonce": payload["nonce"],
                "approved": True,
            },
        ).json()
        assert set(approve_payload) == declared_fields("ChangeOutcome")


def test_the_organize_response_shapes_match(tmp_path: Path) -> None:
    vault, database_path = build_search_vault(tmp_path)
    inbox_note = vault / "Inbox" / "note.md"
    inbox_note.parent.mkdir(parents=True, exist_ok=True)
    inbox_note.write_text("# Redis Cache\n\nRedis cache strategy.\n", encoding="utf-8")
    with Database(database_path) as db:
        IncrementalIndexer(IndexRepository(db)).update(vault)

    with client_with(vault, database_path) as client:
        proposals_payload = client.post("/api/v1/organize/proposals").json()
        assert set(proposals_payload) == declared_fields("OrganizePreview")
        assert proposals_payload["proposals"], "测试数据没造出提案，断言会在空列表上恒真"
        assert set(proposals_payload["proposals"][0]) == declared_fields("OrganizeProposalView")

        assert declared_fields("OrganizePlanRequest") == {"numbers"}

        plan_payload = client.post("/api/v1/organize/plan", json={"numbers": [1]}).json()
        assert set(plan_payload) == declared_fields("ChangePlanView")


def test_the_transactions_response_shapes_match(tmp_path: Path) -> None:
    from uuid import uuid4

    from obsai.safe_write import ChangeSet
    from obsai.transactions.journal import TransactionJournal
    from obsai.transactions.models import TransactionOperation as Op
    from obsai.transactions.service import TransactionService

    vault, database_path = build_search_vault(tmp_path)
    (vault / "note.md").write_text("before", encoding="utf-8")
    service = TransactionService(vault)
    plan = service.plan([Op.replace("note.md", "before", "after")])
    tx_id = uuid4().hex
    journal = TransactionJournal.create(service.root, tx_id, plan)
    journal.update(status="applying")
    service.safe.apply(ChangeSet(plan.changes[0]), approved=True)

    with client_with(vault, database_path) as client:
        list_payload = client.get("/api/v1/transactions").json()
        assert len(list_payload) == 1
        assert set(list_payload[0]) == declared_fields("JournalView")
        assert list_payload[0]["originals"]
        assert set(list_payload[0]["originals"][0]) == declared_fields("JournalOriginal")

        recovery_payload = client.get(f"/api/v1/transactions/{tx_id}").json()
        assert set(recovery_payload) == declared_fields("RecoveryView")
        assert recovery_payload["originals"]
        assert set(recovery_payload["originals"][0]) == declared_fields("JournalOriginal")
        assert recovery_payload["diff"]
        assert set(recovery_payload["diff"][0]) == declared_fields("DiffLine")

        assert declared_fields("RecoverTransactionRequest") == {"approved"}

        outcome_payload = client.post(
            f"/api/v1/transactions/{tx_id}/recover",
            json={"approved": True},
        ).json()
        assert set(outcome_payload) == declared_fields("ChangeOutcome")


def test_the_agent_response_shapes_match(tmp_path: Path) -> None:
    assert declared_fields("AgentRunRequest") == {"query", "thread_id"}
    assert declared_fields("AgentResumeRequest") == {"approved"}
    assert declared_fields("AgentTimelineItem") == {"tool", "summary", "args"}
    assert declared_fields("AgentApprovalView") == {"kind", "preview", "plan_ref"}
    assert declared_fields("AgentRunView") == {
        "run_id",
        "status",
        "query",
        "step_count",
        "retrieval_step_count",
        "selected_note_ids",
        "retrieved_chunk_ids",
        "timeline",
        "final_answer",
        "stop_reason",
        "pending_approval",
    }

    vault, database_path = build_search_vault(tmp_path)
    with client_with(vault, database_path) as client:
        payload = client.post(
            "/api/v1/agent/runs",
            json={"query": "search redis", "thread_id": "test-agent-run-1"},
        ).json()
        assert set(payload) == declared_fields("AgentRunView")
        assert payload["timeline"]
        assert set(payload["timeline"][0]) == declared_fields("AgentTimelineItem")
        assert payload["status"] == "completed"

        get_payload = client.get(f"/api/v1/agent/runs/{payload['run_id']}").json()
        assert set(get_payload) == declared_fields("AgentRunView")
        assert get_payload["run_id"] == payload["run_id"]


