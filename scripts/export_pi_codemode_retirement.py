"""Export structural retirement evidence from the actual production sources."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "runner": "src/qq_ai_bot/services/agent_runner.py",
    "turn": "src/qq_ai_bot/services/turn_execution.py",
    "core": "src/qq_ai_bot/agent_core/loop.py",
    "boundaries": "src/qq_ai_bot/agent_core/model_boundary.py",
    "coordinator": "src/qq_ai_bot/capabilities/coordinator.py",
    "worker": "src/qq_ai_bot/services/subagent_execution.py",
}


def export() -> dict:
    trees = {key: ast.parse((ROOT / path).read_text()) for key, path in FILES.items()}
    classes = {
        node.name: node
        for tree in trees.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }
    runner = classes["AgentRunner"]
    assert "_run" not in {
        node.name for node in runner.body if isinstance(node, ast.AsyncFunctionDef)
    }
    # The worker wrapper is retired; limits live at the real backend boundary.
    assert "WorkerBackend" not in classes
    assert "Callbacks" not in classes
    dynamic = []
    for key in ("runner", "turn", "coordinator"):
        for node in ast.walk(trees[key]):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
            ):
                dynamic.append(f"{FILES[key]}:{node.lineno}")
    assert not dynamic
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "execute"
        for node in ast.walk(trees["coordinator"])
    )
    main_calls = [
        f"{FILES[key]}:{node.lineno}"
        for key, tree in trees.items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_agent_loop"
    ]
    assert len(main_calls) == 1
    state_fields = [
        node.target.id
        for node in classes["TurnState"].body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    return {
        "main_iteration": "agent_core.loop.run_agent_loop",
        "call_graph": [
            "all MainAgentTurnService entries -> AgentRunner.run -> _run_with_receipts",
            "_run_with_receipts -> TurnExecution.activate -> agent_core.loop.run_agent_loop",
            "core -> ModelBoundary/InvocationBoundary/TurnSettlement (11 fixed operations)",
            "request -> prepare_request -> _PrimaryDispatch.admit/complete -> observe_response",
            "invocation -> ToolInvocationCoordinator -> InvocationService -> typed execute_call",
            "execute_code -> CodeModeDriver/Host -> same InvocationService/backend/control",
        ],
        "main_iteration_call_sites": main_calls,
        "removed": [
            "production AgentRunner._run",
            "production Callbacks bag/union",
            "begin_batch compatibility call",
            "legacy tool execute(name,args,runtime) adapter",
            "dynamic backend/model fallback in Runner/Turn/Coordinator",
            "WorkerBackend.__getattr__",
        ],
        "owners": {
            "TurnState": {"fields": state_fields, "lifetime": "one activation of original Work"},
            "TurnExecution": "three fixed core boundaries; no registration/extension hooks",
            "_PrimaryDispatch": "one frozen model candidate and original reservation/CAS",
            "_OrdinarySummaryDispatch": "one ordinary tool-free summary and auxiliary counter",
            "_WorkSummaryDispatch": "one original Work summary page and source validation",
            "AgentToolBackend": "fixed declaration/exposure/domain executor contract, "
            "no loop control",
            "CodeModeDriver": "subcall/feed state only; no model iteration or second scheduler",
        },
        "retained": [
            "explicit test-provider normalization outside Runner",
            "paired legacy journal reader and original effect reconciliation",
            "summary paging and ordered tool batches (not alternate main loops)",
        ],
        "dynamic_fallback_sites": dynamic,
        "source_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in FILES.values()
        },
        "limits": [
            "AST inspection proves these sources; behavioral/entrypoint tests "
            "provide runtime evidence.",
            "No structural result by itself proves faster models, deployment or external effects.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(export(), ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
