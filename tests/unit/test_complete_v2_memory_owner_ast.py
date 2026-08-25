"""AST gate: complete-v2 Memory owner is never bot QQ, scope.key, or Conversation UUID.

This is a semantic scan of owner-construction regions. It is not a line-number
allowlist and not a complete-v2 function-name allowlist: a new helper that
builds a Memory partition from those sources must fail.

v1/legacy-named helpers and ``if not complete_v2`` bodies are ignored.
Provenance keyword arguments such as ``bot_user_id=event.bot_user_id`` are
allowed. Assigning those values to partition/conversation_key/hash sinks is not.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "qq_ai_bot"
OWNER_FILES = (
    SRC_ROOT / "memory" / "partition.py",
    SRC_ROOT / "memory" / "repository.py",
    SRC_ROOT / "memory" / "worker.py",
    SRC_ROOT / "memory" / "dream" / "repository.py",
    SRC_ROOT / "memory" / "self_reflection" / "repository.py",
    SRC_ROOT / "memory" / "runtime" / "turn_session.py",
    SRC_ROOT / "mcp" / "repository.py",
    SRC_ROOT / "identity" / "memory_guard.py",
)
_V1_NAME_MARKERS = ("legacy", "_v1_", "v1_")
_OWNER_SINKS = frozenset(
    {
        "conversation_key",
        "stored_key",
        "batch_key",
        "key_hash",
        "conversation_key_hash",
        "value",
    }
)
_FORBIDDEN_SOURCES = frozenset({"bot_user_id", "canonical_conversation_id"})
_KEY_OWNERS = frozenset({"identity", "_identity", "scope", "_scope"})


def _posix_rel(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _attr_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_named_v1_helper(name: str) -> bool:
    lowered = name.casefold()
    return any(marker in lowered for marker in _V1_NAME_MARKERS)


def _is_canonical_owner_fn(name: str) -> bool:
    lowered = name.casefold()
    if _is_named_v1_helper(name):
        return False
    return "canonical" in lowered or name in {
        "resolve_fact_canonical_owners",
        "canonical_fact_owner_complete",
        "dream_canonical_owner_complete",
        "require_xor_memory_owner",
        "resolve_active_person_id",
        "resolve_active_space_id",
    }


def _is_forbidden_source(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_SOURCES:
        return True
    if isinstance(node, ast.Name) and node.id in _FORBIDDEN_SOURCES:
        return True
    if isinstance(node, ast.Attribute) and node.attr == "key":
        return _attr_name(node.value) in _KEY_OWNERS
    return False


def _contains_forbidden_source(node: ast.AST) -> bool:
    if isinstance(node, ast.expr) and _is_forbidden_source(node):
        return True
    return any(_contains_forbidden_source(child) for child in ast.iter_child_nodes(node))


def _is_complete_v2_test(expr: ast.expr) -> bool:
    if isinstance(expr, ast.Name) and expr.id == "complete_v2":
        return True
    if isinstance(expr, ast.Attribute) and expr.attr == "complete_v2":
        return True
    if isinstance(expr, ast.Await):
        return _is_complete_v2_test(expr.value)
    if isinstance(expr, ast.Call) and _attr_name(expr.func) == "identity_runtime_is_complete_v2":
        return True
    return False


def _is_not_complete_v2_test(expr: ast.expr) -> bool:
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        return _is_complete_v2_test(expr.operand)
    return False


class _SinkVisitor(ast.NodeVisitor):
    def __init__(self, *, proven_v2: bool) -> None:
        self.proven_v2 = proven_v2
        self.hits: list[int] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_If(self, node: ast.If) -> None:
        if _is_complete_v2_test(node.test):
            nested = _SinkVisitor(proven_v2=True)
            for statement in node.body:
                nested.visit(statement)
            self.hits.extend(nested.hits)
            nested_else = _SinkVisitor(proven_v2=False)
            for statement in node.orelse:
                nested_else.visit(statement)
            self.hits.extend(nested_else.hits)
            return
        if _is_not_complete_v2_test(node.test):
            nested = _SinkVisitor(proven_v2=False)
            for statement in node.body:
                nested.visit(statement)
            self.hits.extend(nested.hits)
            nested_else = _SinkVisitor(proven_v2=True)
            for statement in node.orelse:
                nested_else.visit(statement)
            self.hits.extend(nested_else.hits)
            return
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self.proven_v2 and any(_attr_name(target) in _OWNER_SINKS for target in node.targets):
            if _contains_forbidden_source(node.value):
                self.hits.append(node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if (
            self.proven_v2
            and node.value is not None
            and _attr_name(node.target) in _OWNER_SINKS
            and _contains_forbidden_source(node.value)
        ):
            self.hits.append(node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.proven_v2:
            for keyword in node.keywords:
                if keyword.arg in _OWNER_SINKS and _contains_forbidden_source(keyword.value):
                    self.hits.append(node.lineno)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if self.proven_v2 and node.value is not None and _contains_forbidden_source(node.value):
            if isinstance(node.value, ast.Tuple):
                self.hits.append(node.lineno)
        self.generic_visit(node)


def _scan_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[int]:
    if _is_named_v1_helper(node.name):
        return []
    proven = _is_canonical_owner_fn(node.name)
    visitor = _SinkVisitor(proven_v2=proven)
    for statement in node.body:
        visitor.visit(statement)
    return visitor.hits


def _scan_module(tree: ast.AST) -> list[int]:
    hits: list[int] = []

    class Collector(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            hits.extend(_scan_function(node))
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            hits.extend(_scan_function(node))
            self.generic_visit(node)

    Collector().visit(tree)
    return hits


_BINDING_TYPE_NAMES = frozenset({"IdentityBindingModel", "SpaceBindingModel"})
_BINDING_ATTRS = frozenset({"external_account_id", "external_space_id"})


class _BindingBoundaryVisitor(ast.NodeVisitor):
    def __init__(self, *, proven_v2: bool) -> None:
        self.proven_v2 = proven_v2
        self.hits: list[int] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_If(self, node: ast.If) -> None:
        if _is_complete_v2_test(node.test):
            nested = _BindingBoundaryVisitor(proven_v2=True)
            for statement in node.body:
                nested.visit(statement)
            self.hits.extend(nested.hits)
            nested_else = _BindingBoundaryVisitor(proven_v2=False)
            for statement in node.orelse:
                nested_else.visit(statement)
            self.hits.extend(nested_else.hits)
            return
        if _is_not_complete_v2_test(node.test):
            nested = _BindingBoundaryVisitor(proven_v2=False)
            for statement in node.body:
                nested.visit(statement)
            self.hits.extend(nested.hits)
            nested_else = _BindingBoundaryVisitor(proven_v2=True)
            for statement in node.orelse:
                nested_else.visit(statement)
            self.hits.extend(nested_else.hits)
            return
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if self.proven_v2 and node.id in _BINDING_TYPE_NAMES:
            self.hits.append(node.lineno)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self.proven_v2 and node.attr in _BINDING_ATTRS:
            self.hits.append(node.lineno)
        self.generic_visit(node)


def complete_v2_binding_boundary_hits(tree: ast.AST) -> list[int]:
    hits: list[int] = []

    class Collector(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            hits.extend(_binding_hits_for_function(node))
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            hits.extend(_binding_hits_for_function(node))
            self.generic_visit(node)

    Collector().visit(tree)
    return hits


def _binding_hits_for_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[int]:
    if _is_named_v1_helper(node.name):
        return []
    visitor = _BindingBoundaryVisitor(proven_v2=_is_canonical_owner_fn(node.name))
    for statement in node.body:
        visitor.visit(statement)
    return visitor.hits


def _has_canonical_conversation_owner_relation(node: ast.AST) -> bool:
    names: set[str] = set()
    attrs: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        if isinstance(child, ast.Attribute):
            attrs.add(child.attr)
    return (
        "CanonicalConversationModel" in names
        and "canonical_conversation_id" in attrs
        and "kind" in attrs
        and "person_id" in attrs
        and "space_id" in attrs
    )


def test_complete_v2_memory_owner_ast_forbids_legacy_sources() -> None:
    failures: list[str] = []
    partition = ast.parse((SRC_ROOT / "memory" / "partition.py").read_text(encoding="utf-8"))
    for node in ast.walk(partition):
        if isinstance(node, ast.Attribute) and node.attr == "bot_user_id":
            failures.append("memory/partition.py uses bot_user_id")
        if isinstance(node, ast.Attribute) and node.attr == "canonical_conversation_id":
            failures.append("memory/partition.py uses canonical_conversation_id")
        if isinstance(node, ast.Attribute) and node.attr == "key":
            failures.append("memory/partition.py uses .key")
    for path in OWNER_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno in _scan_module(tree):
            failures.append(f"{_posix_rel(path)}:{lineno} complete-v2 owner from forbidden source")
        if path.name == "repository.py" and path.parent.name == "self_reflection":
            for lineno in complete_v2_binding_boundary_hits(tree):
                failures.append(
                    f"{_posix_rel(path)}:{lineno} complete-v2 event boundary uses Binding externals"
                )
    assert not failures, "\n".join(failures)


def test_v2_reflection_history_uses_canonical_conversation_owner() -> None:
    path = SRC_ROOT / "memory" / "self_reflection" / "repository.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    helpers: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    class Collector(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if "canonical" in node.name and "conversation" in node.name:
                helpers.append(node)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if "canonical" in node.name and "conversation" in node.name:
                helpers.append(node)
            self.generic_visit(node)

    Collector().visit(tree)
    assert helpers, "v2 reflection history helper missing canonical conversation owner"
    assert all(_has_canonical_conversation_owner_relation(item) for item in helpers)
    source = path.read_text(encoding="utf-8")
    assert "IdentityBindingModel" not in source
    assert "SpaceBindingModel" not in source


def test_complete_v2_binding_boundary_ast_catches_synthetic_negative() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "async def _event_scope_filter(self, row, *, complete_v2):",
                "    if complete_v2:",
                "        return ChatEventModel.sender_user_id.in_(",
                "            select(IdentityBindingModel.external_account_id)",
                "        )",
                "    return ChatEventModel.private_peer_user_id == row.private_peer_user_id",
                "",
                "def _space_scope(row, complete_v2):",
                "    if complete_v2:",
                "        return ChatEventModel.group_id.in_(",
                "            select(SpaceBindingModel.external_space_id)",
                "        )",
                "    return ChatEventModel.group_id == row.group_id",
            )
        )
    )
    hits = complete_v2_binding_boundary_hits(tree)
    assert hits, "AST gate must catch complete-v2 Binding external event boundaries"


def test_complete_v2_owner_sink_ast_catches_bot_hash_synthetic() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "async def scan_new_events(self):",
                "    if complete_v2:",
                "        key_hash = row.bot_user_id",
                "        conversation_key_hash = row.bot_user_id",
                "    else:",
                "        key_hash = conversation_key_hash(",
                "            scope_type, group_id=row.group_id, private_peer_user_id=peer",
                "        )",
            )
        )
    )
    hits = _scan_module(tree)
    assert hits, "AST gate must catch complete-v2 bot_user_id/hash owner sinks"


def test_canonical_conversation_owner_relation_rejects_synthetic_without_join() -> None:
    missing = ast.parse(
        "\n".join(
            (
                "def _canonical_conversation_event_scope(self, person_id, space_id):",
                "    return ChatEventModel.private_peer_user_id.in_(",
                "        select(IdentityBindingModel.external_account_id)",
                "    )",
            )
        )
    )
    present = ast.parse(
        "\n".join(
            (
                "def _canonical_conversation_event_scope(self, person_id, space_id):",
                "    return and_(",
                "        ChatEventModel.canonical_conversation_id",
                "        == CanonicalConversationModel.id,",
                "        CanonicalConversationModel.kind == 'private',",
                "        CanonicalConversationModel.person_id == person_id,",
                "        CanonicalConversationModel.space_id.is_(None),",
                "    )",
            )
        )
    )
    missing_fn = missing.body[0]
    present_fn = present.body[0]
    assert isinstance(missing_fn, ast.FunctionDef)
    assert isinstance(present_fn, ast.FunctionDef)
    assert not _has_canonical_conversation_owner_relation(missing_fn)
    assert _has_canonical_conversation_owner_relation(present_fn)
