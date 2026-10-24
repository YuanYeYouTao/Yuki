"""Fault inputs for the isolated Desktop server, over actual domain operations."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from tests.support.workspace_snapshots import snapshot_bytes

from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.social.service import SocialContext


class DesktopFaultFixture:
    def __init__(self, runtime, root, person, other_code, model, work_script):
        self.runtime, self.root, self.person = runtime, root, person
        self.other_code, self.model, self.work_script = other_code, model, work_script
        self.lose_response = ""
        self.uploaded_requests = []
        self.pause_next_me = False
        self.me_blocked, self.resume_me = asyncio.Event(), asyncio.Event()
        self.streams = set()
        self.stream_ready = asyncio.Event()
        self.stream_ready.set()

    async def close(self):
        self.resume_me.set()
        self.disconnect()

    def disconnect(self):
        for stopped in tuple(self.streams):
            stopped.set()

    async def stream(self, original):
        """Close only client-facing SSE; internal authority watchers stay subscribed."""
        await self.stream_ready.wait()
        stopped = asyncio.Event()
        self.streams.add(stopped)
        disconnect = asyncio.create_task(stopped.wait())
        pending = None
        try:
            while not stopped.is_set():
                pending = asyncio.create_task(anext(original))
                done, _ = await asyncio.wait(
                    (pending, disconnect), return_when=asyncio.FIRST_COMPLETED
                )
                if disconnect in done:
                    return
                try:
                    yield pending.result()
                except StopAsyncIteration:
                    return
        finally:
            tasks = [disconnect] + ([pending] if pending is not None else [])
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await original.aclose()
            self.streams.discard(stopped)

    async def artifacts(self, long_names):
        runtime = self.runtime
        async with runtime.database.read_session() as reader:
            original = await reader.scalar(
                select(ChatEventModel)
                .where(
                    ChatEventModel.author_person_id == self.person,
                    ChatEventModel.direction == "inbound",
                )
                .order_by(ChatEventModel.id.desc())
                .limit(1)
            )
        assert original is not None
        name = "研究Data_" * 5 + "最終.md" if long_names else "same.md"
        files = [
            (name, b"# First artifact\n\nOriginal immutable content.\n"),
            (name, b"# Another artifact\n\nDifferent immutable content.\n"),
            ("manual.pdf", pdf_document()),
            ("unknown.custom", b"binary artifact content\x00\x01"),
        ]
        metadata = [
            await asyncio.to_thread(
                snapshot_bytes,
                runtime.workspace_service.store,
                filename,
                content,
                artifact_id=str(
                    uuid5(NAMESPACE_URL, f"{self.root}/{index}" + ("/long" if long_names else ""))
                ),
            )
            for index, (filename, content) in enumerate(files)
        ]
        delivered = []
        for index in (0, 1, 2, 3, 0):
            item = metadata[index]
            result = await runtime.social_service.execute(
                "send",
                {
                    "artifact_id": item["artifact_id"],
                    "attachment_kind": "file",
                    "text": "成果 " + item["name"],
                },
                SocialContext(
                    f"fixture:{uuid4()}",
                    "artifact",
                    original.canonical_conversation_id,
                    person_refs={"current_speaker": self.person},
                    trigger_event_id=original.id,
                    reply_presence_id=original.ingress_presence_id,
                ),
            )
            assert result["status"] == "succeeded", result
            delivered.append({**item, "event_id": result["event_id"]})
        return delivered

    def attach(self, app: FastAPI):
        views = self.runtime.client_api.views
        original_stream = views.stream

        def stream(principal, *, active=False):
            return self.stream(original_stream(principal, active=active))

        views.stream = stream

        @app.middleware("http")
        async def failure_injection(request: Request, call_next):
            if request.method == "POST" and request.url.path.endswith("/client/v1/uploads"):
                self.uploaded_requests.append(request.query_params.get("request_id"))
            if (
                self.pause_next_me
                and request.method == "GET"
                and request.url.path.endswith("/client/v1/me")
            ):
                self.pause_next_me = False
                self.me_blocked.set()
                await self.resume_me.wait()
            response = await call_next(request)
            if (
                self.lose_response
                and request.method in {"POST", "PUT", "DELETE"}
                and "/client/v1/" in request.url.path
                and request.url.path.endswith("/" + self.lose_response)
                and response.status_code == 200
            ):
                # Consume the real response after its command/receipt committed; discard only ACK.
                async for _ in response.body_iterator:
                    pass
                self.lose_response = ""
                return JSONResponse({"problem": {"code": "unknown"}}, status_code=503)
            return response

        @app.post("/__test/pause-next-me")
        async def pause_me():
            self.pause_next_me = True
            self.resume_me.clear()
            self.me_blocked.clear()
            return {"ok": True}

        @app.get("/__test/me-blocked")
        async def me_blocked():
            return {"blocked": self.me_blocked.is_set()}

        @app.post("/__test/resume-me")
        async def resume_me():
            self.resume_me.set()
            return {"ok": True}

        @app.post("/__test/lose-next-response")
        async def lose_message():
            self.lose_response = "messages"
            return {"ok": True}

        @app.post("/__test/lose-next-upload-response")
        async def lose_upload():
            self.lose_response = "uploads"
            return {"ok": True}

        @app.post("/__test/lose-next-work-response/{action}")
        async def lose_work(action: str):
            assert action in {"inputs", "cancel"}
            self.lose_response = action
            return {"ok": True}

        @app.post("/__test/lose-next-computer-response")
        async def lose_computer():
            self.lose_response = "control"
            return {"ok": True}

        @app.get("/__test/uploads")
        async def uploads():
            return {"requests": self.uploaded_requests}

        @app.post("/__test/other-person-code")
        async def other_person_code():
            return await self.other_code()

        @app.post("/__test/model-delay/{seconds}")
        async def model_delay(seconds: float):
            self.model._delay_seconds = seconds
            return {"ok": True}

        @app.post("/__test/expire-attachments/{event_id}")
        async def expire_attachments(event_id: int):
            # Expiry does not revoke an original that is still present. Model an
            # unavailable temporary original, retaining its bytes inside this test root.
            media = self.runtime.conversation_media
            async with self.runtime.database.read_session() as reader:
                items = list(
                    await reader.scalars(
                        select(ConversationMediaItemModel).where(
                            ConversationMediaItemModel.source_event_id == event_id
                        )
                    )
                )
            paths = {media._path(item).resolve() for item in items if item.cache_name}
            root = self.root.resolve()
            assert all(path.is_relative_to(root) for path in paths)
            retained = root / "expired-originals"
            await asyncio.to_thread(retained.mkdir, parents=True, exist_ok=True)
            async with self.runtime.database.immediate_session() as writer:
                rows = await writer.scalars(
                    select(ConversationMediaItemModel).where(
                        ConversationMediaItemModel.source_event_id == event_id
                    )
                )
                for item in rows:
                    item.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            for path in paths:
                await asyncio.to_thread(path.replace, retained / (str(uuid4()) + ".blob"))
            return {"ok": True, "originals_unavailable": len(paths)}

        @app.post("/__test/disconnect")
        async def disconnect():
            self.disconnect()
            return {"ok": True}

        @app.delete("/__test/events/{event_id}")
        async def delete_event(event_id: int, lose_hint: bool = False):
            disconnected = len(self.streams) if lose_hint else 0
            if lose_hint:
                # End client streams before the real commit and let reconnects
                # read a fresh sync afterwards. Internal subscriptions keep all hints.
                self.stream_ready.clear()
                self.disconnect()
            try:
                async with self.runtime.database.immediate_session() as writer:
                    await writer.execute(
                        delete(ChatEventModel).where(ChatEventModel.id == event_id)
                    )
            finally:
                self.stream_ready.set()
            return {
                "deleted": event_id,
                "lost_hint": lose_hint,
                "client_streams_disconnected": disconnected,
            }

        @app.post("/__test/artifacts")
        async def artifacts(long_names: bool = False):
            return await self.artifacts(long_names)

        @app.post("/__test/work-scenario")
        async def work_scenario():
            self.work_script[:] = [
                ("task_control", {"action": "accept", "goal": "原工作整合測試"}),
                ("send", {"text": "需要你提供城市"}),
                ("task_control", {"action": "need_input", "reason": "city missing"}),
                ("send", {"text": "已依原工作完成"}),
                ("task_control", {"action": "complete", "result": "original work finished"}),
            ]
            return {"ok": True}


def pdf_document() -> bytes:
    stream = b"BT /F1 14 Tf 20 180 Td (Actual artifact PDF) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    result, offsets = b"%PDF-1.4\n", []
    for index, item in enumerate(objects, 1):
        offsets.append(len(result))
        result += str(index).encode() + b" 0 obj\n" + item + b"\nendobj\n"
    xref = len(result)
    result += b"xref\n0 6\n0000000000 65535 f \n"
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    return (
        result
        + b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref).encode()
        + b"\n%%EOF\n"
    )
