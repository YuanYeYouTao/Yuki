"""AST gate: sender==bot is identity ownership unless v1-named or author_kind-null.

This is a semantic scan of every function in the listed packages. It is not a
line-number allowlist and not a complete-v2 function-name allowlist: a new
helper that compares sender to bot without a named v1/legacy wrapper or a
proven ``author_kind is None`` region must fail.

Guarded regions are conservative:
- ``if author_kind is None:`` guards only the body, never orelse.
- a compare in an if-test is guarded only as an AND conjunct beside that proof;
  OR never proves the region and never guards.
- SQLAlchemy ``and_(author_kind.is_(None), compare)`` may guard; ``isnot`` never.
- nested scopes must not expand the proof.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (
    "conversation",
    "services",
    "persistence",
    "memory",
    "plugin_host",
    "identity",
)
SRC_ROOT = REPO_ROOT / "src" / "qq_ai_bot"
_SENDER_ATTRS = frozenset(
    {
        "sender_user_id",
        "sender_id",
        "reply_sender_user_id",
        "reply_sender",
    }
)
_BOT_ATTRS = frozenset({"bot_user_id", "bot_id"})
_V1_NAME_MARKERS = ("legacy", "_v1_", "v1_", "author_kind_is_none", "null_author")


def _python_files() -> tuple[Path, ...]:
    files: list[Path] = []
    for package in PACKAGES:
        root = SRC_ROOT / package
        files.extend(path for path in root.rglob("*.py") if path.is_file())
    return tuple(sorted(files))


def _posix_rel(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _attr_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_named_v1_helper(name: str) -> bool:
    lowered = name.casefold()
    return any(marker in lowered for marker in _V1_NAME_MARKERS)


def _is_sender_bot_compare(node: ast.Compare) -> bool:
    if not any(isinstance(op, ast.Eq | ast.NotEq) for op in node.ops):
        return False
    names = {_attr_name(node.left), *(_attr_name(item) for item in node.comparators)}
    return bool(names & _SENDER_ATTRS) and bool(names & _BOT_ATTRS)


def _is_author_kind_none_atom(expr: ast.expr) -> bool:
    """True only for a direct author_kind-is-None proof. isnot / OR never qualify."""

    if isinstance(expr, ast.Compare):
        if any(isinstance(op, ast.IsNot | ast.NotEq) for op in expr.ops):
            return False
        if not any(isinstance(op, ast.Is | ast.Eq) for op in expr.ops):
            return False
        names = {_attr_name(expr.left), *(_attr_name(item) for item in expr.comparators)}
        constants = [
            item.value for item in (expr.left, *expr.comparators) if isinstance(item, ast.Constant)
        ]
        return "author_kind" in names and None in constants
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
        if expr.func.attr != "is_":
            return False
        if _attr_name(expr.func.value) != "author_kind":
            return False
        return bool(
            expr.args and isinstance(expr.args[0], ast.Constant) and expr.args[0].value is None
        )
    return False


def _proves_author_kind_none(expr: ast.expr) -> bool:
    if _is_author_kind_none_atom(expr):
        return True
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.And):
        return any(_proves_author_kind_none(value) for value in expr.values)
    if isinstance(expr, ast.Call) and _attr_name(expr.func) == "and_":
        return any(_proves_author_kind_none(arg) for arg in expr.args)
    return False


class _RegionCompareVisitor(ast.NodeVisitor):
    """Collect sender==bot compares in a proven region without entering new scopes."""

    def __init__(self) -> None:
        self.found: set[int] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Compare(self, node: ast.Compare) -> None:
        if _is_sender_bot_compare(node):
            self.found.add(id(node))
        self.generic_visit(node)


def _region_sender_bot_compares(statements: list[ast.stmt]) -> set[int]:
    visitor = _RegionCompareVisitor()
    for statement in statements:
        visitor.visit(statement)
    return visitor.found


def _python_and_guarded_compares(expr: ast.expr, *, proven: bool = False) -> set[int]:
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.And):
        proven = proven or any(_is_author_kind_none_atom(value) for value in expr.values)
        allowed: set[int] = set()
        for value in expr.values:
            if proven and isinstance(value, ast.Compare) and _is_sender_bot_compare(value):
                allowed.add(id(value))
            allowed |= _python_and_guarded_compares(value, proven=proven)
        return allowed
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or):
        allowed = set()
        for value in expr.values:
            allowed |= _python_and_guarded_compares(value, proven=False)
        return allowed
    return set()


def _sql_and_mark_compares(call: ast.Call, *, proven: bool) -> set[int]:
    if _attr_name(call.func) != "and_":
        return set()
    proven = proven or any(_is_author_kind_none_atom(arg) for arg in call.args)
    allowed: set[int] = set()
    for arg in call.args:
        if proven and isinstance(arg, ast.Compare) and _is_sender_bot_compare(arg):
            allowed.add(id(arg))
        if isinstance(arg, ast.Call) and _attr_name(arg.func) == "and_":
            allowed |= _sql_and_mark_compares(arg, proven=proven)
    return allowed


def _guarded_compares(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[int]:
    allowed: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            if _proves_author_kind_none(node.test):
                allowed |= _region_sender_bot_compares(node.body)
            allowed |= _python_and_guarded_compares(node.test)
        if isinstance(node, ast.Call) and _attr_name(node.func) == "and_":
            allowed |= _sql_and_mark_compares(node, proven=False)
    return allowed


def _ungarded_sender_bot_sites(tree: ast.AST, *, filename: str) -> list[str]:
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if _is_named_v1_helper(fn.name):
            continue
        guarded = _guarded_compares(fn)
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Compare)
                and _is_sender_bot_compare(node)
                and id(node) not in guarded
            ):
                found.append(f"{filename}:{fn.name}:{node.lineno}")
    return found


def test_no_ungarded_sender_bot_ownership_in_scanned_packages() -> None:
    found: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.extend(_ungarded_sender_bot_sites(tree, filename=_posix_rel(path)))
    assert found == []


def test_ast_gate_flags_new_ungarded_equality_outside_any_name_list() -> None:
    """A newly added helper must not hide sender==bot behind an omitted v2 name."""

    tree = ast.parse(
        "\n".join(
            (
                "def enrich_reference_targets(event):",
                "    if event.sender_user_id != event.bot_user_id:",
                "        return event.sender_user_id",
                "    return event.bot_user_id",
                "",
                "def complete_v2_enrich(event):",
                "    return event.reply_sender_user_id == event.bot_user_id",
            )
        )
    )
    found = _ungarded_sender_bot_sites(tree, filename="synthetic.py")
    assert "synthetic.py:enrich_reference_targets:2" in found
    assert "synthetic.py:complete_v2_enrich:7" in found


def test_ast_gate_allows_named_v1_and_author_kind_none_guard() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "def _legacy_v1_sender_role(sender_user_id, bot_user_id):",
                "    return sender_user_id == bot_user_id",
                "",
                "def classify(event):",
                "    if event.author_kind is None:",
                "        return event.sender_user_id == event.bot_user_id",
                "    return False",
                "",
                "def display_name(event):",
                "    if event.author_kind is None and event.sender_user_id == event.bot_user_id:",
                "        return 'Yuki'",
                "    return event.sender_user_id",
                "",
                "def sql_and(event):",
                "    return and_(",
                "        event.author_kind.is_(None),",
                "        event.sender_user_id != event.bot_user_id,",
                "    )",
            )
        )
    )
    assert _ungarded_sender_bot_sites(tree, filename="synthetic.py") == []


def test_ast_gate_rejects_else_or_isnot_and_nested_nonnull() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "def bad_else(event):",
                "    if event.author_kind is None:",
                "        return False",
                "    else:",
                "        return event.sender_user_id == event.bot_user_id",
                "",
                "def bad_or(event):",
                "    if event.author_kind is None or event.sender_user_id == event.bot_user_id:",
                "        return True",
                "    return False",
                "",
                "def bad_or_body(event):",
                "    if event.author_kind is None or event.flag:",
                "        return event.sender_user_id == event.bot_user_id",
                "    return False",
                "",
                "def bad_isnot(event):",
                "    return and_(",
                "        event.author_kind.isnot(None),",
                "        event.sender_user_id != event.bot_user_id,",
                "    )",
                "",
                "def bad_nested_nonnull(event):",
                "    if event.ready:",
                "        if event.author_kind is None:",
                "            return False",
                "        else:",
                "            return event.sender_user_id == event.bot_user_id",
                "    return False",
            )
        )
    )
    found = _ungarded_sender_bot_sites(tree, filename="synthetic.py")
    names = {item.split(":")[1] for item in found}
    assert names == {
        "bad_else",
        "bad_or",
        "bad_or_body",
        "bad_isnot",
        "bad_nested_nonnull",
    }
