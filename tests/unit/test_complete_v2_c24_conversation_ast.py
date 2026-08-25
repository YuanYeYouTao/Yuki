"""AST gate: C24b live writers assign Conversation correlation and never pick first row."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "qq_ai_bot"
WRITER_FILES = (
    SRC_ROOT / "identity" / "c24_conversation.py",
    SRC_ROOT / "model_runtime" / "repository.py",
    SRC_ROOT / "conversation" / "cadence.py",
    SRC_ROOT / "persistence" / "web_repository.py",
    SRC_ROOT / "mcp" / "repository.py",
    SRC_ROOT / "speech" / "repository.py",
)
CALLER_FILES = (
    SRC_ROOT / "services" / "chat.py",
    SRC_ROOT / "services" / "agent_tools.py",
    SRC_ROOT / "speech" / "reply_effect.py",
    SRC_ROOT / "speech" / "service.py",
)
CALLER_METHODS = {
    "chat.py": (
        "_trusted_conversation_write_kwargs",
        "_save_native_web_response",
        "_record_mcp_invocation",
        "_record_reply_effects",
    ),
    "agent_tools.py": ("_persist_web_response",),
    "reply_effect.py": ("VoiceReplyEffectService.prepare",),
    "service.py": ("GenieTTSProvider.synthesize", "SpeechService.synthesize"),
}
_WRITE_FNS = frozenset(
    {
        "record",
        "create",
        "record_invocation",
        "save_response",
        "stamp_conversation_correlation",
    }
)
_EVENT_HELPERS = frozenset(
    {
        "load_unique_live_chat_event",
        "resolve_conversation_id_for_chat_event",
        "resolve_conversation_id_for_event",
    }
)
_HELPER_PREFIXES = (
    "load_unique_",
    "resolve_",
    "try_live_",
    "require_live_",
    "stamp_",
)
_FIRST_ROW_ATTRS = frozenset({"first"})


def _posix_rel(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _attr_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _class_method(
    tree: ast.AST, class_name: str, method_name: str
) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, ast.AsyncFunctionDef | ast.FunctionDef)
                    and item.name == method_name
                ):
                    return item
    raise AssertionError(f"{class_name}.{method_name} missing")


def _mentions_canonical(node: ast.AST) -> bool:
    if isinstance(node, ast.keyword) and node.arg == "canonical_conversation_id":
        return True
    if isinstance(node, ast.arg) and node.arg == "canonical_conversation_id":
        return True
    if isinstance(node, ast.Name) and node.id in {
        "canonical_conversation_id",
        "_trusted_conversation_write_kwargs",
    }:
        return True
    if isinstance(node, ast.Attribute) and node.attr in {
        "canonical_conversation_id",
        "_trusted_conversation_write_kwargs",
    }:
        return True
    if isinstance(node, ast.Constant) and node.value == "canonical_conversation_id":
        return True
    return any(_mentions_canonical(child) for child in ast.iter_child_nodes(node))


def _lookup_caller(tree: ast.AST, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    if "." in name:
        class_name, method_name = name.split(".", 1)
        return _class_method(tree, class_name, method_name)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} missing")


def _is_helper(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in _HELPER_PREFIXES)


def _contains_canonical_assignment(node: ast.AST) -> bool:
    if isinstance(node, ast.Assign):
        if any(_attr_name(target) == "canonical_conversation_id" for target in node.targets):
            return True
    if isinstance(node, ast.Call) and _attr_name(node.func) == "stamp_conversation_correlation":
        return True
    return any(_contains_canonical_assignment(child) for child in ast.iter_child_nodes(node))


def _is_first_row_selection(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and _attr_name(node.func) in _FIRST_ROW_ATTRS:
        return True
    if isinstance(node, ast.Subscript):
        slice_node = node.slice
        if isinstance(slice_node, ast.Constant) and slice_node.value == 0:
            return True
    return False


class _WriterVisitor(ast.NodeVisitor):
    def __init__(self, *, allow_unique_index: bool) -> None:
        self.allow_unique_index = allow_unique_index
        self.first_row_hits: list[int] = []
        self.has_len_guard = False

    def visit_Call(self, node: ast.Call) -> None:
        if _attr_name(node.func) == "len":
            self.has_len_guard = True
        if _is_first_row_selection(node):
            self.first_row_hits.append(node.lineno)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _is_first_row_selection(node) and not (self.allow_unique_index and self.has_len_guard):
            self.first_row_hits.append(node.lineno)
        self.generic_visit(node)


def _scan_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[bool, list[int]]:
    allow_unique_index = _is_helper(node.name)
    visitor = _WriterVisitor(allow_unique_index=allow_unique_index)
    for statement in node.body:
        visitor.visit(statement)
    assigned = _contains_canonical_assignment(node)
    if node.name not in _WRITE_FNS:
        assigned = True
    return assigned, visitor.first_row_hits


def _scan_module(tree: ast.AST) -> tuple[list[str], list[int]]:
    missing: list[str] = []
    first_rows: list[int] = []

    class Collector(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            assigned, hits = _scan_function(node)
            if node.name in _WRITE_FNS and not assigned:
                missing.append(node.name)
            if node.name in _WRITE_FNS or node.name in _EVENT_HELPERS:
                first_rows.extend(hits)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            assigned, hits = _scan_function(node)
            if node.name in _WRITE_FNS and not assigned:
                missing.append(node.name)
            if node.name in _WRITE_FNS or node.name in _EVENT_HELPERS:
                first_rows.extend(hits)
            self.generic_visit(node)

    Collector().visit(tree)
    return missing, first_rows


def test_complete_v2_c24b_live_callers_forward_trusted_ids() -> None:
    failures: list[str] = []
    for path in CALLER_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = _posix_rel(path)
        for name in CALLER_METHODS[path.name]:
            node = _lookup_caller(tree, name)
            if not _mentions_canonical(node):
                failures.append(f"{rel} {name} missing trusted Conversation forward")
            _, first_rows = _scan_function(node)
            for lineno in first_rows:
                failures.append(f"{rel}:{lineno} {name} first-row selection")
    assert not failures, "\n".join(failures)


def test_complete_v2_c24b_live_writers_assign_and_never_pick_first() -> None:
    failures: list[str] = []
    for path in WRITER_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        missing, first_rows = _scan_module(tree)
        rel = _posix_rel(path)
        for name in missing:
            failures.append(f"{rel} {name} missing canonical assignment")
        for lineno in first_rows:
            failures.append(f"{rel}:{lineno} first-row selection")
        source = path.read_text(encoding="utf-8")
        if "ChatEventModel" in source and path.name != "c24_conversation.py":
            if ".first(" in source.replace(" ", ""):
                failures.append(f"{rel} ChatEvent first-row helper")
    assert not failures, "\n".join(failures)


def test_ast_gate_requires_canonical_assignment_on_write_path() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "async def record(self, *, conversation_key):",
                "    row = Model(conversation_key_hash=conversation_key)",
                "    session.add(row)",
            )
        )
    )
    missing, first_rows = _scan_module(tree)
    assert missing == ["record"]
    assert first_rows == []


def test_ast_gate_catches_first_row_selection() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "async def record(self, session):",
                "    if complete_v2:",
                "        rows = await session.scalars(select(ChatEventModel))",
                "        row.canonical_conversation_id = rows[0].canonical_conversation_id",
            )
        )
    )
    missing, first_rows = _scan_module(tree)
    assert missing == []
    assert first_rows, "AST gate must catch first-row ChatEvent selection"


def test_ast_gate_allows_unique_len_guard_in_helper() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "async def load_unique_live_chat_event(session):",
                "    rows = list(await session.scalars(select(ChatEventModel)))",
                "    if len(rows) != 1:",
                "        return None",
                "    return rows[0]",
            )
        )
    )
    missing, first_rows = _scan_module(tree)
    assert missing == []
    assert first_rows == []
