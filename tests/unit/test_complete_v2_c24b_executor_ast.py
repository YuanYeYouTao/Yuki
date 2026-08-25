"""AST gate: C24b-2a executor records and allowed callers forward Conversation id."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "qq_ai_bot"
EXECUTOR_PATH = SRC_ROOT / "model_runtime" / "executor.py"
CALLER_PATHS = (
    SRC_ROOT / "services" / "agent_runner.py",
    SRC_ROOT / "services" / "chat.py",
    SRC_ROOT / "services" / "plugin_sessions.py",
    SRC_ROOT / "plugin_host" / "facades.py",
    SRC_ROOT / "automation" / "handlers.py",
    SRC_ROOT / "conversation" / "rollup" / "service.py",
    SRC_ROOT / "model_runtime" / "structured.py",
)
_EXECUTE_OWNERS = frozenset({"ModelExecutor", "LegacyTaskModelExecutor", "TaskModelExecutor"})


def _posix_rel(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _attr_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _has_arg(fn: ast.AsyncFunctionDef | ast.FunctionDef, name: str) -> bool:
    return any(arg.arg == name for arg in (*fn.args.args, *fn.args.kwonlyargs))


def _record_calls(fn: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and _attr_name(node.func) == "record"
    ]


def _has_keyword(call: ast.Call, name: str) -> bool:
    return any(keyword.arg == name for keyword in call.keywords)


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
    if isinstance(node, ast.Name) and node.id == "canonical_conversation_id":
        return True
    if isinstance(node, ast.Attribute) and node.attr == "canonical_conversation_id":
        return True
    if isinstance(node, ast.Constant) and node.value == "canonical_conversation_id":
        return True
    return any(_mentions_canonical(child) for child in ast.iter_child_nodes(node))


def test_both_record_sites_forward_canonical_conversation_id() -> None:
    tree = ast.parse(EXECUTOR_PATH.read_text(encoding="utf-8"))
    for owner in _EXECUTE_OWNERS:
        execute = _class_method(tree, owner, "execute")
        assert _has_arg(execute, "canonical_conversation_id"), owner

    execute = _class_method(tree, "TaskModelExecutor", "execute")
    records = _record_calls(execute)
    assert len(records) == 2, "success and failure record sites required"
    missing = [
        call.lineno for call in records if not _has_keyword(call, "canonical_conversation_id")
    ]
    assert missing == [], f"record sites missing canonical_conversation_id: {missing}"

    shape = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "request_shape_hash":
            shape = node
            break
    assert shape is not None
    assert not _mentions_canonical(shape)


def test_allowed_callers_forward_trusted_conversation_id() -> None:
    failures: list[str] = []
    required = {
        SRC_ROOT / "services" / "agent_runner.py": ("AgentRuntime", "AgentRunner"),
        SRC_ROOT / "services" / "chat.py": ("_run_agent",),
        SRC_ROOT / "services" / "plugin_sessions.py": ("run",),
        SRC_ROOT / "plugin_host" / "facades.py": (
            "_agent_dependencies",
            "_AgentFacade.run",
            "_SpeechFacade.synthesize",
        ),
        SRC_ROOT / "automation" / "handlers.py": ("generate", "agent"),
        SRC_ROOT / "conversation" / "rollup" / "service.py": ("_model_summary",),
        SRC_ROOT / "model_runtime" / "structured.py": ("run", "run_with_response"),
    }
    for path in CALLER_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = _posix_rel(path)
        if path.name == "agent_runner.py":
            runtime = None
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "AgentRuntime":
                    runtime = node
                    break
            field_names = {
                _attr_name(item.target)
                for item in (runtime.body if runtime is not None else [])
                if isinstance(item, ast.AnnAssign)
            }
            if "canonical_conversation_id" not in field_names:
                failures.append(f"{rel} AgentRuntime missing field")
            runner = _class_method(tree, "AgentRunner", "run")
            if not _mentions_canonical(runner):
                failures.append(f"{rel} AgentRunner does not forward field")
            continue
        for name in required[path]:
            if "." in name:
                class_name, method_name = name.split(".", 1)
                node = _class_method(tree, class_name, method_name)
                if not _mentions_canonical(node):
                    failures.append(f"{rel} {name} missing canonical forward")
                continue
            found = False
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
                    found = True
                    if not _mentions_canonical(node):
                        failures.append(f"{rel} {name} missing canonical forward")
                    break
            if not found:
                failures.append(f"{rel} {name} missing")
    assert not failures, "\n".join(failures)


def test_ast_gate_requires_both_record_keywords() -> None:
    tree = ast.parse(
        "\n".join(
            (
                "class TaskModelExecutor:",
                "    async def execute(self, task, request, *, canonical_conversation_id=None):",
                "        await self._invocations.record(task=task, success=False)",
                "        await self._invocations.record(",
                "            task=task,",
                "            success=True,",
                "            canonical_conversation_id=canonical_conversation_id,",
                "        )",
            )
        )
    )
    execute = _class_method(tree, "TaskModelExecutor", "execute")
    records = _record_calls(execute)
    assert len(records) == 2
    assert not _has_keyword(records[0], "canonical_conversation_id")
    assert _has_keyword(records[1], "canonical_conversation_id")
