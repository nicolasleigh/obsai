"""A-12 and B-1 acceptance: the layer boundaries are enforced, not just documented.

``application/`` exists so the CLI and the HTTP adapter share one behaviour. That
only holds while it stays framework-free and does not depend upwards on either
adapter, and while neither adapter reaches past it into the domain. A grep is a
one-off; these tests are the permanent version.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "obsai"
APPLICATION = SRC / "application"
API = SRC / "api"
API_ROUTES = API / "routes"
CLI_APP = SRC / "cli" / "app.py"

FORBIDDEN_IN_APPLICATION = {"rich", "typer", "click", "fastapi", "starlette", "uvicorn"}
FORBIDDEN_IN_API = {"rich", "typer", "click"}
UPWARD = {"obsai.cli", "obsai.api"}

#: Domain packages a *route* must not import. Routes go through ``application``;
#: reaching into ``obsai.storage`` from an endpoint is how the CLI ended up with
#: its assembly logic scattered across a thousand lines.
DOMAIN_PACKAGES = {
    "obsai.agent",
    "obsai.answering",
    "obsai.chunking",
    "obsai.embedding",
    "obsai.graph",
    "obsai.indexing",
    "obsai.logging",
    "obsai.organizer",
    "obsai.retrieval",
    "obsai.safe_write",
    "obsai.shutdown",
    "obsai.storage",
    "obsai.transactions",
    "obsai.vault",
}

#: The only module-level calls the API layer may make. Everything else — reading
#: configuration, opening a database, binding a socket — belongs in the lifespan,
#: where it happens once and can be logged.
PURE_MODULE_CALLS = {"create_app", "logging.getLogger", "frozenset", "re.compile", "APIRouter"}

#: Domain internals the CLI used to call directly. Every one of them now has a
#: public counterpart, so a reappearance means a boundary was reopened.
FORBIDDEN_PRIVATE = {
    "_path",
    "_read",
    "_vacant",
    "_check_current",
    "_atomic_write",
    "_check_budget",
    "_recovery_state",
    "_verify",
    "_rollback",
    "_current",
}


def modules() -> list[Path]:
    return sorted(path for path in APPLICATION.glob("*.py") if path.name != "__init__.py")


def api_modules() -> list[Path]:
    return sorted(path for path in API.rglob("*.py") if path.name != "__init__.py")


def route_modules() -> list[Path]:
    return sorted(path for path in API_ROUTES.glob("*.py") if path.name != "__init__.py")


def imported_roots(path: Path) -> set[str]:
    """Every dotted module name imported at any level of ``path``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def module_level_calls(path: Path) -> list[str]:
    """Calls executed while ``path`` is being imported."""
    calls: list[str] = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            calls.append(ast.unparse(node.value))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            calls.append(ast.unparse(node.value))
    return calls


def test_the_application_package_is_not_empty() -> None:
    assert {path.stem for path in modules()} >= {
        "dto", "paths", "embedding", "search", "answering",
        "changes", "organizer", "agent_runtime", "jobs", "locks",
        "index", "status", "notes", "index_jobs",
    }


@pytest.mark.parametrize("path", modules(), ids=lambda path: path.stem)
def test_the_application_layer_imports_no_framework(path: Path) -> None:
    roots = {name.split(".")[0] for name in imported_roots(path)}
    assert not (roots & FORBIDDEN_IN_APPLICATION), (
        f"{path.name} imports {sorted(roots & FORBIDDEN_IN_APPLICATION)}; "
        "rendering and argument parsing belong to the adapters"
    )


@pytest.mark.parametrize("path", modules(), ids=lambda path: path.stem)
def test_the_application_layer_does_not_depend_on_an_adapter(path: Path) -> None:
    for name in imported_roots(path):
        assert not any(name == up or name.startswith(up + ".") for up in UPWARD), (
            f"{path.name} imports {name}; dependencies point downwards only"
        )


def test_the_application_layer_has_no_module_level_side_effects() -> None:
    """Importing it must not read configuration or touch the filesystem."""
    for path in modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                raise AssertionError(f"{path.name} runs {ast.unparse(node.value)} at import time")


def test_the_cli_does_not_reach_into_domain_privates() -> None:
    accessed = {
        node.attr
        for node in ast.walk(ast.parse(CLI_APP.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute)
    }
    assert not (accessed & FORBIDDEN_PRIVATE), (
        f"cli/app.py still calls {sorted(accessed & FORBIDDEN_PRIVATE)}; "
        "use the public API instead"
    )


def test_the_cli_no_longer_assembles_services() -> None:
    """The four assembly helpers the refactor removed must not come back."""
    source = CLI_APP.read_text(encoding="utf-8")
    for name in ("_embedding_pipeline", "_semantic_retriever", "_agent_runtime", "_organize"):
        assert f"def {name}(" not in source, f"{name} was moved to the application layer"


def test_the_cli_no_longer_assembles_the_moved_services() -> None:
    """These four modules are what ``_embedding_pipeline``, ``_semantic_retriever``,
    ``_agent_runtime`` and ``_organize_inbox`` used to wire together by hand.

    ``links suggest`` still drives ``TransactionService`` directly — it is
    orchestration of one interactive command, not a service assembly, and the plan
    left it in the adapter. That is why the assertion names modules rather than
    banning domain imports outright.
    """
    roots = imported_roots(CLI_APP)
    assert "obsai.application.paths" in roots
    for moved in ("obsai.embedding", "obsai.answering", "obsai.organizer", "obsai.agent"):
        assert not any(
            name == moved or name.startswith(moved + ".") for name in roots
        ), f"cli/app.py still wires {moved} itself"


# --------------------------------------------------------------------------- #
# B-1: the HTTP adapter follows the same rules
# --------------------------------------------------------------------------- #


def test_the_api_package_is_not_empty() -> None:
    assert {path.stem for path in api_modules()} >= {"app", "deps", "errors", "security"}
    assert {path.stem for path in route_modules()} >= {
        "status", "search", "consent", "ask", "notes", "jobs", "index_jobs"
    }


@pytest.mark.parametrize("path", api_modules(), ids=lambda path: path.stem)
def test_the_api_layer_imports_no_terminal_library(path: Path) -> None:
    roots = {name.split(".")[0] for name in imported_roots(path)}
    assert not (roots & FORBIDDEN_IN_API), (
        f"{path.name} imports {sorted(roots & FORBIDDEN_IN_API)}; "
        "a terminal prompt belongs to the CLI, a dialog belongs to the browser"
    )


@pytest.mark.parametrize("path", api_modules(), ids=lambda path: path.stem)
def test_the_api_layer_does_not_depend_on_the_cli(path: Path) -> None:
    for name in imported_roots(path):
        assert not (name == "obsai.cli" or name.startswith("obsai.cli.")), (
            f"{path.name} imports {name}; the two adapters are siblings, not a chain"
        )


@pytest.mark.parametrize("path", api_modules(), ids=lambda path: path.stem)
def test_the_api_layer_does_no_work_at_import_time(path: Path) -> None:
    """Importing the adapter must not read configuration, open a database or bind
    a socket: ``uvicorn --reload`` re-imports this package on every edit."""
    for call in module_level_calls(path):
        assert call.split("(")[0] in PURE_MODULE_CALLS, (
            f"{path.name} runs {call} at import time; "
            "configuration, databases and sockets belong in the lifespan"
        )


@pytest.mark.parametrize("path", route_modules(), ids=lambda path: path.stem)
def test_a_route_does_not_reach_past_the_application_layer(path: Path) -> None:
    for name in imported_roots(path):
        assert not any(
            name == package or name.startswith(package + ".") for package in DOMAIN_PACKAGES
        ), (
            f"{path.name} imports {name}; a route resolves its dependencies and calls "
            "one application service, it does not assemble domain objects"
        )


@pytest.mark.parametrize("path", route_modules(), ids=lambda path: path.stem)
def test_a_route_does_not_load_configuration_itself(path: Path) -> None:
    """Configuration arrives through ``deps.get_settings``, which is what makes it
    overridable in a test and re-read per request in production."""
    for name in imported_roots(path):
        assert name != "obsai.config.loader", (
            f"{path.name} loads settings directly; depend on deps.get_settings instead"
        )
