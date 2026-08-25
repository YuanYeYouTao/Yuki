"""C27 complete-v2 contract: AST gates, binary epoch, legacy write helpers."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_type_hints

import pytest

from qq_ai_bot.health import HealthPayload
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.metadata import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "qq_ai_bot"
ADMIN_ACTOR_ADAPTERS = frozenset(
    {
        "src/qq_ai_bot/services/command_service.py",
        "src/qq_ai_bot/admin/capabilities.py",
    }
)
NO_FABRICATED_SUPERUSER_FILES = (
    "src/qq_ai_bot/automation/handlers.py",
    "src/qq_ai_bot/automation/service.py",
    "src/qq_ai_bot/plugin_host/facades.py",
    "src/qq_ai_bot/mcp/admin.py",
)
FORBIDDEN_TABLES = frozenset(
    {
        "yuki",
        "yukis",
        "yuki_self",
        "yukiself",
        "gateway_connections",
        "presence_active_routes",
        "delivery_routes",
    }
)
V2_HELPER_CALLS = frozenset(
    {
        "ensure_runtime_people_row",
        "ensure_runtime_group_row",
    }
)
FROZEN_HEALTH_KEYS = frozenset(get_type_hints(HealthPayload))


def _python_files(root: Path) -> tuple[Path, ...]:
    if root.is_file():
        return (root,)
    return tuple(sorted(path for path in root.rglob("*.py") if path.is_file()))


def _posix_rel(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _literal_true_keyword(call: ast.Call, name: str) -> bool:
    return any(
        keyword.arg == name
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


def test_healthz_public_shape_is_unchanged() -> None:
    assert {"status", "version", "database"} <= FROZEN_HEALTH_KEYS
    assert not {"token", "secret", "admin"} & {key.casefold() for key in FROZEN_HEALTH_KEYS}
    source = (SRC_ROOT / "main.py").read_text(encoding="utf-8")
    assert "/admin" not in source
    assert "include_router" not in source


def test_production_tree_forbids_get_bots_and_fabricated_admin_authority() -> None:
    get_bots_calls: set[str] = set()
    admin_actor_calls: set[str] = set()
    fabricated_superuser: set[str] = set()
    for path in _python_files(SRC_ROOT):
        rel = _posix_rel(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name == "get_bots":
                get_bots_calls.add(rel)
            if name == "AdminActor":
                admin_actor_calls.add(rel)
            if _literal_true_keyword(node, "actor_is_superuser"):
                fabricated_superuser.add(rel)
    assert get_bots_calls == set()
    assert admin_actor_calls == ADMIN_ACTOR_ADAPTERS
    assert fabricated_superuser == set()


def test_automation_plugin_and_mcp_do_not_construct_admin_actor() -> None:
    for rel in NO_FABRICATED_SUPERUSER_FILES:
        path = REPO_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        assert not any(_call_name(node.func) == "AdminActor" for node in calls)
        assert not any(_literal_true_keyword(node, "actor_is_superuser") for node in calls)
        assert rel not in ADMIN_ACTOR_ADAPTERS


def test_persistence_has_no_forbidden_identity_tables() -> None:
    tables = set(Base.metadata.tables)
    assert not (FORBIDDEN_TABLES & tables)
    for name in tables:
        assert "presence_active_route" not in name
        assert name not in FORBIDDEN_TABLES


def test_no_fourth_capability_registry() -> None:
    names: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "CapabilityRegistry":
                names.append(f"{_posix_rel(path)}:{node.name}")
    assert set(names) == {
        "src/qq_ai_bot/admin/capabilities.py:CapabilityRegistry",
        "src/qq_ai_bot/capabilities/registry.py:CapabilityRegistry",
    }


def test_complete_v2_does_not_call_legacy_carrier_helpers() -> None:
    found: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if fn.name in V2_HELPER_CALLS:
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and _call_name(node.func) in V2_HELPER_CALLS:
                    found.append(f"{_posix_rel(path)}:{fn.name}")
    assert found == []


def test_apply_uses_single_begin_immediate() -> None:
    source = (SRC_ROOT / "identity" / "cutover_service.py").read_text(encoding="utf-8")
    assert source.count('execute("BEGIN IMMEDIATE")') == 1
    assert "def apply(" in source
    tree = ast.parse(source)
    apply_fns = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name in {"apply", "_apply_locked"}
    }
    assert "apply" in apply_fns
    rendered = "\n".join(ast.unparse(node) for node in apply_fns.values())
    assert "BEGIN IMMEDIATE" in rendered
    assert 'execute("BEGIN")' not in rendered.replace('execute("BEGIN IMMEDIATE")', "")


def test_append_complete_v2_does_not_touch_legacy_scope_or_people() -> None:
    source = (SRC_ROOT / "persistence" / "scoped_event_uow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if fn.name != "_append_complete_v2":
            continue
        names = {_call_name(node.func) for node in ast.walk(fn) if isinstance(node, ast.Call)}
        assert "get_or_create_scope_row" not in names
        assert "_ensure_person" not in names
        assert "_ensure_group" not in names
        assert "ensure_runtime_people_row" not in names
        assert "ensure_runtime_group_row" not in names


@pytest.mark.asyncio
async def test_legacy_carrier_helpers_fail_closed(database: Database) -> None:
    from qq_ai_bot.identity.dual_write import (
        ensure_runtime_group_row,
        ensure_runtime_people_row,
    )

    async with database.sessions() as session:
        with pytest.raises(IdentityDualWriteError) as people:
            await ensure_runtime_people_row(session, "1001")
        with pytest.raises(IdentityDualWriteError) as groups:
            await ensure_runtime_group_row(session, "2001")
    assert people.value.category == "legacy_carrier_write"
    assert groups.value.category == "legacy_carrier_write"
