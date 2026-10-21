"""Bot-owned Work authorization over the Manager's original ACP operations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_queries import WorkQueries

if TYPE_CHECKING:
    from yuki.sandbox.client import SandboxClient


async def execute(
    client: SandboxClient, args: dict[str, Any], request_id: str, source: dict[str, Any] | None
) -> dict[str, Any]:
    action = args.get("action")
    fields = {
        "list": {"action"},
        "prompt": {"action", "agent_id", "prompt", "peer_id"},
        "get": {"action", "run_id", "request_id", "cursor"},
        "respond": {"action", "run_id", "permission_id", "option_id", "outcome"},
        "cancel": {"action", "run_id"},
        "close": {"action", "peer_id"},
    }
    if not isinstance(action, str) or action not in fields or set(args) - fields[action]:
        raise ValueError("invalid_external_agent_arguments")
    if action == "list":
        return await client.execute("acp_agents", {}, request_id=request_id)
    if client.tasks is None:
        raise ValueError("external_agent_unavailable")
    control = current_work_control.get()
    if control is None:
        raise ValueError("external_agent_source_unavailable")
    await control.validate()
    if not await control.repository.valid(control.lease):
        raise ValueError("external_agent_source_obsolete")
    peer_id = args.get("peer_id")
    row = None
    if peer_id is not None:
        row = await client.tasks.by_peer(peer_id)
    elif action != "prompt":
        if action == "get":
            references = {
                key: args[key]
                for key in ("run_id", "request_id")
                if isinstance(args.get(key), str) and args[key]
            }
            if len(references) != 1:
                raise ValueError("run_id_or_request_id_required")
            row = (
                await client.tasks.get(references["request_id"])
                if "request_id" in references
                else await client.tasks.by_run(references["run_id"])
            )
        elif not isinstance(args.get("run_id"), str):
            raise ValueError("run_id_required")
        else:
            row = await client.tasks.by_run(args["run_id"])
    if action != "prompt" or peer_id is not None:
        if row is None:
            raise ValueError("external_agent_not_found")
        owner = json.loads(row.source_json).get("work_id")
        original = await WorkQueries(control.repository).get(
            control.lease, control.source, owner, local=True
        )
        if original is None:
            raise ValueError("external_agent_not_found")
        original_progress = json.loads(row.progress_json)
        if action == "get" and original_progress.get("kind") != "acp_prompt":
            raise ValueError("external_agent_not_found")
        peer_id = original_progress.get("peer_id")
        if not peer_id and action != "get":
            raise ValueError("external_agent_peer_unconfirmed")
    if action == "prompt":
        if control.current is None:
            raise ValueError("accept_work_before_execution")
        if row is not None and json.loads(row.source_json).get("work_id") != control.current["id"]:
            raise ValueError("external_agent_peer_not_owned")
        if not isinstance(args.get("prompt"), str) or not args["prompt"].strip():
            raise ValueError("external_agent_prompt_required")
        if peer_id is None and not isinstance(args.get("agent_id"), str):
            raise ValueError("agent_id_required")
        prepared = await client.tasks.prepare(
            request_id,
            {"tool": "external_agent", "arguments": args},
            {
                **(source or {}),
                **control.source,
                "work_id": control.current["id"],
                "conversation_id": control.lease.conversation_id,
                "generation": control.lease.generation,
            },
        )
        if prepared.run_id:
            return await client.execute(
                "acp_prompt_query", {"run_id": prepared.run_id}, request_id=request_id
            )
        if prepared.status == "completed":
            return cast(dict[str, Any], json.loads(prepared.completion_json or "{}"))
        await client.tasks.validate_source(prepared, request_id)
        progress = {
            **json.loads(prepared.progress_json),
            "kind": "acp_prompt",
            "resource_request_id": f"{request_id}:peer",
        }
        peer_id = progress.get("peer_id", peer_id)
        await client.tasks.checkpoint(request_id, progress)
        if peer_id is None:
            opened = await client.execute(
                "acp_peer_open", {"agent_id": args["agent_id"]}, request_id=f"{request_id}:peer"
            )
            if "peer_id" not in opened:
                opened = {
                    **opened,
                    "request_id": request_id,
                    "resource_request_id": progress["resource_request_id"],
                }
                if opened.get("executed") is False:
                    await client.tasks.reject(request_id, opened)
                return opened
            peer_id = opened["peer_id"]
            progress = {
                **progress,
                "peer_id": peer_id,
                "agent_id": args["agent_id"],
                "session_id": opened.get("session_id"),
            }
        await client.tasks.checkpoint(request_id, {**progress, "peer_id": peer_id})
        # Opening a peer can outlive source revocation. Recheck before the new prompt.
        await control.validate()
        if not await control.repository.valid(control.lease):
            raise ValueError("external_agent_source_obsolete")
        await client.tasks.validate_source(prepared, request_id)
        result = await client.execute(
            "acp_prompt_submit",
            {"peer_id": peer_id, "prompt": args["prompt"]},
            request_id=request_id,
        )
        if result.get("error") and result.get("executed") is False:
            await client.tasks.reject(request_id, result)
            return result
        if result.get("error") and not result.get("run_id"):
            recovered = await client.execute(
                "acp_prompt_query", {"request_id": request_id}, request_id=request_id
            )
            if recovered.get("run_id"):
                result = recovered
        if result.get("run_id"):
            await client.tasks.bind_run(request_id, result["run_id"])
            await client.tasks.checkpoint(
                request_id,
                {
                    **progress,
                    "peer_id": peer_id,
                    "session_id": result.get("session_id"),
                },
            )
            await client._stage_result("external_agent", request_id, result)
            if result.get("pending") is False:
                return await client.execute(
                    "acp_prompt_query", {"run_id": result["run_id"]}, request_id=request_id
                )
        elif result.get("error"):
            result = {**result, "request_id": request_id, "uncertain": True}
        return result
    assert row is not None
    if action == "get":
        view = await client.execute(
            "acp_prompt_query",
            {
                **({"run_id": row.run_id} if row.run_id else {"request_id": row.request_id}),
                **({"after_cursor": args["cursor"]} if "cursor" in args else {}),
            },
            request_id=request_id,
        )
        if view.get("kind") == "acp_prompt":
            await client.tasks.bind_run(row.request_id, view["run_id"])
            await client.tasks.checkpoint(
                row.request_id,
                {
                    **original_progress,
                    "peer_id": view["peer_id"],
                    "session_id": view.get("session_id"),
                },
            )
            await client._stage_result("acp_prompt_query", row.request_id, view)
            return view
        result = {
            **view,
            "request_id": row.request_id,
            "resource_request_id": original_progress["resource_request_id"],
        }
        if view.get("error") == "unknown_request":
            peer = await client.execute(
                "acp_prompt_query",
                {"request_id": original_progress["resource_request_id"]},
                request_id=request_id,
            )
            if peer.get("kind") == "acp_peer":
                await client.tasks.checkpoint(
                    row.request_id,
                    {
                        **original_progress,
                        "peer_id": peer["peer_id"],
                        "session_id": peer.get("session_id"),
                    },
                )
            result.update(peer_resource=peer, uncertain=True)
        return result
    if action == "close":
        closed = await client.execute("acp_peer_close", {"peer_id": peer_id}, request_id=request_id)
        return {"peer_id": peer_id, "close": closed}
    if action == "respond":
        assert original is not None
        if original["state"] in {"completed", "failed", "cancelled"}:
            raise ValueError("external_agent_source_obsolete")
        await client.tasks.validate_source(row, request_id)
        if not isinstance(args.get("permission_id"), str) or (
            ("option_id" in args) == ("outcome" in args)
        ):
            raise ValueError("external_agent_permission_response_required")
        answered = await client.execute(
            "acp_permission_reply",
            {
                "peer_id": peer_id,
                "run_id": row.run_id,
                "permission_id": args["permission_id"],
                **(
                    {"option_id": args["option_id"]}
                    if "option_id" in args
                    else {"outcome": args["outcome"]}
                ),
            },
            request_id=request_id,
        )
        view = await client.execute(
            "acp_prompt_query", {"run_id": row.run_id}, request_id=request_id
        )
        return {
            **view,
            "permission_response": answered,
            **(
                {"error": answered["error"], "uncertain": bool(answered.get("uncertain"))}
                if answered.get("error")
                else {}
            ),
        }
    return await client.execute(
        "acp_prompt_cancel", {"peer_id": peer_id, "run_id": row.run_id}, request_id=request_id
    )
