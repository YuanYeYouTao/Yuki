"""Sandbox computer/browser tools end to end: real app, Manager, Xvnc, Chromium and driver.

Failure cases were listed before the implementation in
docs/operations/computer-browser-sandbox-20261109.md. One scripted Main model
drives the real offline application (App private chat → Work) through the 29-tool
contract against a local fixture site:

* B1–B7: multi-step form with ``<select>`` and Enter submit, alert/confirm/prompt,
  a download, ``<input type=file>`` without a native dialog, a cross-SITE iframe
  (``127.0.0.1`` page, ``localhost`` frame → out-of-process), a second tab
  attributed to the Work, Unicode through Chromium and xdotool.
* M5/M6: a 30-step screenshot loop whose clicks use the coordinates of the image
  they came with; no request is refused at image 17.
* C7–C9: a human takes the screen mid-loop, the next input is ``control_held``,
  Yuki waits on the original ``wait-control`` run, the human returns and Yuki
  continues; every input has one durable receipt and no lease is left behind.
* C1/H1: the frozen manifest is the 29 tools; Work steps project as 操作電腦/使用瀏覽器.

Artifacts: ``test-results/computer-browser/`` — manifest, every frame, the
model requests (with replaced-image texts), the site's input log, the lease
timeline and the Manager receipts. Requires the headed environment packages
(CI ``desktop-headed`` job).
"""

import asyncio
import base64
import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from tests.conftest import make_settings
from tests.support.model_profiles import write_fake_profiles

from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "test-results" / "computer-browser"
LOOP = 30
TAKE_AT = 13  # the loop step (a click) at which the human takes the screen

pytestmark = pytest.mark.skipif(
    not all(shutil.which(name) for name in ("Xvnc", "openbox", "chromium", "xdotool", "import")),
    reason="Requires the actual headed environment packages",
)

MAIN = """<!doctype html><meta charset=utf-8><title>Yuki E2E</title>
<body style="font:18px sans-serif;margin:24px">
<h1>Yuki E2E</h1>
<form method=post action=/submit>
 <fieldset id=one><legend>Step 1</legend>
  <label>Name <input name=name></label>
  <label>Plan <select name=plan><option>Basic</option><option>Pro</option></select></label>
  <button type=button onclick="one.hidden=true;two.hidden=false">Next</button>
 </fieldset>
 <fieldset id=two hidden><legend>Step 2</legend>
  <label>Email <input name=email></label> <button>Submit</button>
 </fieldset>
</form>
<p>
 <button onclick="alert('hello');log('alert:done')">Alert</button>
 <button onclick="log('confirm:'+confirm('sure?'))">Confirm</button>
 <button onclick="log('prompt:'+prompt('Your city?','x'))">Prompt</button>
</p>
<p><a href=/report.txt download>Download report</a></p>
<form method=post action=/upload enctype=multipart/form-data target=sink>
 <label>Attachment <input type=file name=file></label><button>Upload</button>
</form>
<iframe name=sink title=Sink style="width:10px;height:10px"></iframe>
<iframe src="http://localhost:{port}/frame" title=Partner style="width:420px;height:120px">
</iframe>
<p><a href=/second target=_blank>Open second</a></p>
<script>function log(v){{fetch('/dialog',{{method:'POST',body:v}})}}</script>
"""
FRAME = """<!doctype html><meta charset=utf-8><title>Partner</title>
<form method=post action=/frame-submit><label>Code <input name=code></label>
<button>Send code</button></form>"""
SECOND = """<!doctype html><meta charset=utf-8><title>Second</title>
<body style="margin:0"><h1 style="margin:0;font:16px sans-serif">Second page</h1>
<textarea aria-label=Notes autofocus style="width:100vw;height:90vh;font:24px sans-serif"
 oninput="fetch('/typed',{method:'POST',body:this.value})"></textarea>"""


class Site:
    def __init__(self):
        self.log: list[dict] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                port = self.server.server_port
                if self.path == "/report.txt":
                    body, kind = "Yuki report 報告\n".encode(), "text/plain; charset=utf-8"
                else:
                    page = {"/": MAIN.format(port=port), "/frame": FRAME, "/second": SECOND}.get(
                        self.path, "<!doctype html><meta charset=utf-8><title>Done</title>ok"
                    )
                    body, kind = page.encode(), "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                if self.path == "/report.txt":
                    self.send_header("Content-Disposition", "attachment; filename=report.txt")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                site.log.append(
                    {
                        "path": self.path,
                        "host": self.headers.get("Host", "").split(":")[0],
                        "body": data.decode("utf-8", "replace"),
                        "at": time.time(),
                    }
                )
                body = b"<!doctype html><meta charset=utf-8><title>Thanks</title>Thanks"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def posts(self, path):
        return [item for item in self.log if item["path"] == path]


def node(result, pattern):
    """The id of the first snapshot line containing ``pattern``; ids are never guessed."""
    for line in result["snapshot"].splitlines():
        if pattern in line:
            found = re.search(r"\[((?:[0-9a-f]{8}_)?e\d+)\]\s*$", line) or re.search(
                r"\[((?:[0-9a-f]{8}_)?e\d+)\]", line
            )
            assert found, line
            return found.group(1)
    raise AssertionError(f"{pattern!r} not in snapshot:\n{result['snapshot']}")


def script(site_url, upload, take, give_back):
    """Generator Main model: each yield is one tool call, each send its parsed result."""
    out = {}
    r = yield ("browser", {"action": "open", "url": site_url + "/"})
    out["tab"] = tab = r["tab"]["id"]
    assert r["snapshot"].startswith("page: Yuki E2E"), r
    r = yield (
        "browser",
        {"action": "type", "node": node(r, 'textbox "Name"'), "text": "雪 Yuki🙂"},
    )
    r = yield ("browser", {"action": "select", "node": node(r, 'combobox "Plan"'), "text": "Pro"})
    r = yield ("browser", {"action": "click", "node": node(r, 'button "Next"')})
    email = node(r, 'textbox "Email"')
    r = yield ("browser", {"action": "type", "node": email, "text": "yuki@example.test"})
    out["email"] = email
    for button, decision in (
        ("Alert", ("accept_dialog", None)),
        ("Confirm", ("dismiss_dialog", None)),
        ("Prompt", ("accept_dialog", "東京")),
    ):
        r = yield ("browser", {"action": "click", "node": node(r, f'button "{button}"')})
        dialog = [event for event in r["events"] if event["type"] == "dialog"]
        assert dialog, r
        # B3: any other action while the dialog is open is refused, never hangs.
        blocked = yield ("browser", {"action": "snapshot", "tab": tab})
        assert blocked["error"] == "dialog_open", blocked
        action, text = decision
        r = yield ("browser", {"action": action, **({"text": text} if text else {})})
    r = yield ("browser", {"action": "click", "node": node(r, 'link "Download report"')})
    out["download"] = [event for event in r["events"] if event["type"] == "download"]
    assert out["download"], r
    r = yield ("browser", {"action": "click", "node": node(r, '"Attachment"')})
    assert any(event["type"] == "file_chooser" for event in r["events"]), r
    r = yield ("browser", {"action": "type", "node": node(r, '"Attachment"'), "text": upload})
    r = yield ("browser", {"action": "click", "node": node(r, 'button "Upload"')})
    # B2: the cross-site frame's form, by its own out-of-process node ids.
    code = node(r, 'textbox "Code"')
    out["frame_node"] = code
    r = yield ("browser", {"action": "type", "node": code, "text": "42"})
    r = yield ("browser", {"action": "click", "node": node(r, 'button "Send code"')})
    r = yield ("browser", {"action": "click", "node": node(r, 'link "Open second"')})
    tabs = [event["tab"] for event in r["events"] if event["type"] == "new_tab"]
    assert tabs, r
    out["second"] = second = tabs[0]["id"]
    r = yield ("browser", {"action": "snapshot", "tab": second})
    assert "Second page" in r["snapshot"], r
    r = yield ("browser", {"action": "screenshot", "tab": second})
    out["page_image"] = (r["width"], r["height"])
    # B3: Enter really submits the first tab's form.
    r = yield ("browser", {"action": "type", "tab": tab, "node": out["email"], "text": "y@e.test"})
    r = yield ("browser", {"action": "press", "tab": tab, "text": "Enter"})
    out["submitted"] = r
    # M5/M6: 30 screen steps on the browser window; coordinates always come from
    # the (window-relative) image just received.
    r = yield ("computer", {"action": "screenshot"})
    out["windows"] = r["windows"]
    window = next(item["id"] for item in r["windows"] if "Second" in item["title"])
    step = 0
    while step < LOOP:
        if step % 3 == 0:
            call = {"action": "screenshot", "window": window}
        elif step % 3 == 1:
            call = {
                "action": "click",
                "window": window,
                "x": r["width"] // 2,
                "y": r["height"] // 2,
            }
        else:
            call = {"action": "type", "text": f"中文{step} "}
        if step == TAKE_AT and "held" not in out:
            take()
        result = yield ("computer", call)
        if result.get("error") == "control_held":
            out["held"] = result
            run = yield ("run_command", {"command": "yuki-computer wait-control"})
            give_back()
            yield ("wait", {"for": [{"run": run["id"]}]})
            continue  # the same step again, after the human returned
        assert "width" in result, result
        if "window" in call:
            r = result
        step += 1
    out["final"] = r
    yield ("manage_task", {"action": "finish", "result": "表单、下载、上传与屏幕循环完成"})
    return out


async def test_sandbox_computer_and_browser_drive_real_desktop_end_to_end(tmp_path, monkeypatch):
    from tests.integration.test_current_material_main_contract import _catalog
    from tests.support.desktop_headed_computer_fixture import DesktopHeadedComputerFixture

    from yuki.container import ApplicationContainer
    from yuki.identity.canonical_repository import create_presence
    from yuki.identity.db_models import CanonicalPersonModel
    from yuki.llm.fake import FakeLLMProvider
    from yuki.model_runtime.models import (
        ModelCapability,
        ModelProfile,
        ModelProtocol,
    )
    from yuki.model_runtime.pool import ModelClientPool
    from yuki.persistence.database import Database

    site = Site()
    site.thread.start()
    port = site.server.server_port
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'yuki.sqlite3').as_posix()}")
    await database.create_schema()
    app = ApplicationContainer(
        make_settings(
            database.url,
            model_profiles_file=write_fake_profiles(tmp_path / "models.toml"),
            plugin_system_enabled=False,
            plugin_directory=tmp_path / "plugins",
            workspace_directory=tmp_path / "workspace",
            conversation_media_cache_directory=tmp_path / "media",
            social_transfer_directory=tmp_path / "transfer",
            runtime_work_enabled=True,
            self_memory_enabled=False,
            memory_self_reflection_enabled=False,
            memory_dream_enabled=False,
            memory_maintenance_enabled=False,
            memory_embedding_enabled=False,
            memory_embedding_worker_enabled=False,
            conversation_proactive_wakeup_enabled=False,
            conversation_autonomous_enabled=False,
            telegram_bot_token="",
            asr_enabled=False,
            web_mode="disabled",
        ),
        database=database,
    )
    await app.character.initialize("Yuki")
    owner = str(uuid4())
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        session.add(
            CanonicalPersonModel(
                id=owner, display_name="Owner", is_superuser=True, created_at=now, updated_at=now
            )
        )
        await create_presence(session, external_account_id="yuki-app", platform="app", now=now)

    computer = DesktopHeadedComputerFixture(tmp_path, app)
    manager = computer.manager
    timeline: list[dict] = []
    record = manager.computer.set

    def recorded_set(key, args):
        result = record(key, args)
        timeline.append(
            {
                "at": time.time(),
                "action": args.get("action"),
                "actor": sorted(args.get("actor", {})),
                "holder": manager.computer.control()["holder"],
                "error": result.get("error"),
            }
        )
        return result

    manager.computer.set = recorded_set
    upload = manager.files.root / "upload.txt"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_text("上傳內容 upload\n", encoding="utf-8")
    human = {"lease": None}

    def take():
        human["lease"] = manager.computer.set(
            str(uuid4()),
            {
                "action": "take",
                "actor": {"person_id": owner, "device_id": str(uuid4()), "device_label": "E2E"},
            },
        )["control"]["lease_id"]

    returned = asyncio.Event()

    def give_back():
        returned.set()

    generator = script(f"http://127.0.0.1:{port}", str(upload), take, give_back)
    finished = asyncio.Event()
    outcome: dict = {}
    pending = {"first": True}

    def respond(request):
        if finished.is_set() or not request.tools:
            return "完成。"
        tool = next((m for m in reversed(request.messages) if m.role == "tool"), None)
        try:
            if pending["first"]:
                pending["first"] = False
                name, arguments = next(generator)
            else:
                name, arguments = generator.send(json.loads(tool.content))
        except StopIteration as stop:
            outcome.update(stop.value)
            finished.set()
            return "表单与屏幕操作都完成了。"
        except Exception as exc:  # the scripted model's own check failed: stop and report
            outcome["script_error"] = repr(exc)
            outcome["last_result"] = tool.content if tool is not None else None
            finished.set()
            return "脚本检查失败。"
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(ToolCall(str(uuid4()), ToolFunction(name, json.dumps(arguments))),),
        )

    model = FakeLLMProvider(respond)
    profile = ModelProfile(
        id="main",
        provider="gemini",
        protocol=ModelProtocol.GEMINI,
        base_url="https://wire.invalid/",
        api_key_env="SYNTHETIC_ONLY",
        model="gemini-main",
        timeout_seconds=2,
        max_retries=0,
        default_temperature=0.2,
        default_max_output_tokens=4096,
        capabilities=frozenset(
            {ModelCapability.TOOLS, ModelCapability.REASONING, ModelCapability.IMAGE_INPUT}
        ),
    )
    app.models.apply_catalog(_catalog(profile), ModelClientPool(injected_profiles={"main": model}))

    async def human_returns():
        await returned.wait()
        # The human returns only after Yuki registered its original wait.
        changed = asyncio.Event()
        manager.computer.changed.add(changed)
        try:
            while not any(
                item["kind"] == "wait_control"
                for item in manager.computer.snapshot()["help_requests"]
            ):
                changed.clear()
                await changed.wait()
        finally:
            manager.computer.changed.discard(changed)
        manager.computer.set(
            str(uuid4()),
            {
                "action": "return",
                "actor": {"person_id": owner, "device_id": str(uuid4()), "device_label": "E2E"},
                "lease_id": human["lease"],
            },
        )

    started = False
    manifest: dict = {}
    collected: dict = {}
    helper = asyncio.create_task(human_returns())
    try:
        await app.model_invocations.writer.start()
        await app.app_gateway.start()
        await computer.start(owner)
        await app._freeze_main_manifest()
        await app.work_scheduler.start()
        started = True
        manifest = {
            tool.name: sorted(tool.parameters.get("properties", {}))
            for tool in await app.main_agent_contract.model_definitions()
        }
        code = await app.client_identity.issue_for_operator(owner, str(uuid4()), str(uuid4()))
        device = await app.client_identity.pair(code["code"], "E2E device")
        principal = await app.client_identity.authenticate(device["token"])
        await app.client_api.messages.submit(principal, str(uuid4()), "請把網頁表單和桌面步驟做完")
        try:
            async with asyncio.timeout(600):
                await finished.wait()
                await asyncio.gather(*tuple(app.app_gateway.tasks), return_exceptions=True)
        finally:
            # Partial runs still leave their evidence for diagnosis.
            collected = {
                "receipts": {
                    row["request_id"]: json.loads(row["result"])
                    for row in manager.db.execute(
                        "SELECT request_id,result FROM environment_mutations"
                    )
                },
                "downloads": sorted(
                    path.name
                    for path in (manager.files.root / "downloads").glob("*")
                    if path.is_file()
                ),
                "frames": _frames(model.requests),
                "requests": _requests(model.requests),
                "steps": await _steps(app),
            }
    finally:
        helper.cancel()
        try:
            if started:
                await computer.close()
                await app.work_scheduler.close()
            assert not await app.lifecycle._close_entries(app.lifecycle._entries)
        finally:
            await database.close()
            site.server.shutdown()
            site.thread.join(5)
            _write(manifest=manifest, log=site.log, timeline=timeline, outcome=outcome, **collected)

    assert "script_error" not in outcome, outcome
    receipts, downloads, frames, requests, steps = (
        collected[name] for name in ("receipts", "downloads", "frames", "requests", "steps")
    )
    # C1: the 29-tool contract.
    assert len(manifest) == 29 and {"computer", "browser"} <= set(manifest)
    # B3: Enter submitted the multi-step form with the selected option and Unicode text.
    (submit,) = site.posts("/submit")
    assert "Pro" in submit["body"] and "y%40e.test" in submit["body"], submit
    assert "%E9%9B%AA+Yuki" in submit["body"], submit
    assert [item["body"] for item in site.posts("/dialog")] == [
        "alert:done",
        "confirm:false",
        "prompt:東京",
    ]
    # B4: the download landed in /workspace/downloads and its path was reported.
    (download,) = outcome["download"]
    assert download["path"].startswith(str(manager.files.root / "downloads"))
    assert downloads and (manager.files.root / "downloads" / Path(download["path"]).name).exists()
    # B3: the file input received the workspace file, no native dialog.
    (uploaded,) = site.posts("/upload")
    assert "upload.txt" in uploaded["body"] and "上傳內容 upload" in uploaded["body"]
    # B2: the cross-site frame was an out-of-process target and its form was submitted.
    assert "_e" in outcome["frame_node"]
    (frame,) = site.posts("/frame-submit")
    assert frame["host"] == "localhost" and frame["body"] == "code=42"
    # B7: xdotool typed Unicode into the visible second tab.
    typed = site.posts("/typed")
    assert typed and "中文" in typed[-1]["body"], typed
    # C8: the human's take refused the next input; Yuki continued after the return.
    assert outcome["held"]["control"]["holder"] == "human"
    holders = [(item["action"], item["error"]) for item in timeline]
    assert ("take", None) in holders and ("return", None) in holders
    # C4/C5/C9: every input has its final receipt and no lease remains.
    assert not [key for key, value in receipts.items() if value.get("pending")]
    assert manager_control_after(timeline) == "none"
    # M5: 30+ screenshots in one Work and no request carried more than the frame limit.
    assert len(frames) >= LOOP
    assert max(item["images"] for item in requests) <= 16
    # H1: Work steps project the new tools into their Desktop categories.
    assert {"computer", "browser"} <= {step["category"] for step in steps}


def manager_control_after(timeline):
    return timeline[-1]["holder"] if timeline else "none"


def _frames(requests):
    frames: dict[str, dict] = {}
    for request in requests:
        for message in request.messages:
            for image in message.images:
                digest = hashlib.sha256(image.data_url.encode()).hexdigest()
                if digest not in frames:
                    frames[digest] = {
                        "index": len(frames),
                        "source": image.source,
                        "note": (message.content or "")[:200],
                        "data_url": image.data_url,
                    }
    return list(frames.values())


def _requests(requests):
    rows = []
    for index, request in enumerate(requests):
        notes = [
            message.content
            for message in request.messages
            if message.role == "user" and (message.content or "").startswith("[Host 工具媒体")
        ]
        rows.append(
            {
                "index": index,
                "messages": len(request.messages),
                "images": sum(len(message.images) for message in request.messages),
                "media_observations": notes,
            }
        )
    return rows


async def _steps(app):
    from sqlalchemy import select

    from yuki.client_api.work_projection import public_step
    from yuki.runtime.work_schema_v1 import effects

    async with app.database.read_session() as reader:
        rows = (await reader.execute(select(effects).where(effects.c.kind == "tool"))).mappings()
        return [public_step(row, b"e2e") for row in rows]


def _write(*, frames=(), **values):
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    # The CI image runs as root over a checkout owned by the runner user.
    git = ["git", "-c", "safe.directory=*"]
    head = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True, cwd=ROOT).strip()
    dirty = subprocess.check_output([*git, "status", "--porcelain"], text=True, cwd=ROOT)
    revision = {"commit": head, "worktree_dirty": bool(dirty.strip())}
    directory = ARTIFACT / "frames"
    directory.mkdir(exist_ok=True)
    for frame in frames:
        header, encoded = frame.pop("data_url").split(",", 1)
        suffix = "png" if "png" in header else "jpg"
        name = f"{frame['index']:03d}.{suffix}"
        (directory / name).write_bytes(base64.b64decode(encoded))
        frame["file"] = "frames/" + name
    values["frames"] = frames
    for name, value in values.items():
        (ARTIFACT / f"{name}.json").write_text(
            json.dumps({**revision, name: value}, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
