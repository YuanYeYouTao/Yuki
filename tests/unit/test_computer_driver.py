"""The long-lived environment driver against a real Chromium, plus its file protocol.

Failure cases are listed first in docs/operations/computer-browser-sandbox-20261109.md
(B1-B7, C8, M3, M6, P1-P9). Browser tests need ``YUKI_TEST_CHROMIUM``; the screen
tests additionally need the headed environment's Xvnc, Openbox, xdotool and
ImageMagick, so they run in the ``desktop-headed`` CI job.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from yuki_agent_connector import cdp

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "src/yuki/sandbox/yuki_computer.py"
SERVICE = ROOT / "src/yuki/sandbox/desktop_service.py"
CONNECTOR = ROOT / "packages/yuki-agent-connector/src"
NODE = re.compile(r"\[((?:[0-9a-f]{8}_)?e\d+)\]")


# Local fixture site -----------------------------------------------------------


@dataclass
class Site:
    port: int
    pages: dict[str, tuple[bytes, dict[str, str]]] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    # Paths under /slow/ answer only once this is set.
    release: threading.Event = field(default_factory=threading.Event)

    def page(self, path: str, html: str, **headers: str) -> str:
        self.pages[path] = (html.encode(), {"Content-Type": "text/html; charset=utf-8", **headers})
        return self.url(path)

    def file(self, path: str, data: bytes, **headers: str) -> str:
        self.pages[path] = (data, headers)
        return self.url(path)

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"


@pytest.fixture(scope="module")
def site() -> Iterator[Site]:
    state: Site

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append(self.path)
            if self.path.startswith("/slow/"):
                state.release.wait(30)
            body, headers = state.pages.get(self.path.split("?")[0], (b"missing", {}))
            self.send_response(200 if self.path.split("?")[0] in state.pages else 404)
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state = Site(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()


# Real Chromium ----------------------------------------------------------------


@pytest.fixture(scope="module")
def chromium(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    binary = os.environ.get("YUKI_TEST_CHROMIUM")
    if not binary:
        pytest.skip("Set YUKI_TEST_CHROMIUM to an actual isolated Chromium executable")
    profile = tmp_path_factory.mktemp("chromium")
    process = subprocess.Popen(
        [
            binary,
            "--headless",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-port=0",
            "--user-data-dir=" + str(profile),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        active = profile / "DevToolsActivePort"
        while not (active.exists() and (port := active.read_text().partition("\n")[0])):
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
        yield "http://127.0.0.1:" + port
    finally:
        process.terminate()
        process.wait(timeout=15)


@pytest.fixture
def driver(chromium: str, tmp_path: Path) -> Iterator[cdp.Browser]:
    browser = cdp.Browser(chromium, downloads=str(tmp_path / "downloads"), timeout=10)
    yield browser
    browser.close()


def allow() -> None:
    return None


def act(
    browser: cdp.Browser,
    owner: str | None = "work-a",
    before_input: Callable[[], None] = allow,
    wait_seconds: float = 15,
    **args: Any,
) -> dict[str, Any]:
    return execute(browser, owner, before_input, wait_seconds=wait_seconds, **args)[0]


def execute(
    browser: cdp.Browser,
    owner: str | None = "work-a",
    before_input: Callable[[], None] = allow,
    max_dimension: int = 1280,
    wait_seconds: float = 15,
    **args: Any,
) -> tuple[dict[str, Any], cdp.Image | None]:
    try:
        return browser.execute(
            args,
            owner=owner,
            before_input=before_input,
            max_dimension=max_dimension,
            max_pixels=1_500_000,
            wait_seconds=wait_seconds,
        )
    except cdp.Failure as failure:
        return failure.result, None


def node(snapshot: str, fragment: str) -> str:
    line = next(line for line in snapshot.splitlines() if fragment in line)
    return NODE.findall(line)[-1]


def held_after(allowed: int) -> Callable[[], None]:
    """A lease that is taken away after ``allowed`` inputs were dispatched."""
    calls = 0

    def check() -> None:
        nonlocal calls
        if calls >= allowed:
            raise cdp.Failure(
                {"error": "control_held", "message": "接管", "control": {"holder": "human"}}
            )
        calls += 1

    return check


def jpeg_size(data: bytes) -> tuple[int, int]:
    assert data.startswith(b"\xff\xd8")
    return cdp.jpeg_size(data)


# Snapshot format (§5.4) ---------------------------------------------------------


def test_snapshot_is_a_lossless_outline_with_states_and_node_ids(driver, site):
    url = site.page(
        "/outline",
        "<title>大綱</title><h2>Account</h2>"
        "<p>Read <b>the</b> <a href='/terms'>terms</a> first</p>"
        "<div><div><button>Sign in</button></div></div>"
        "<label>Remember <input type=checkbox checked></label>"
        "<input aria-label=Email required value='a@b.c'>"
        "<button disabled>Later</button>"
        "<ol><li>one</li><li>two</li></ol>"
        "<span aria-hidden=true>hidden text</span>",
    )
    result = act(driver, action="open", url=url)
    assert result["tab"]["loading"] is False
    assert result["events"] == [{"type": "navigated", "url": url}]
    lines = result["snapshot"].splitlines()
    assert lines[0] == f"page: 大綱 — {url} [{result['tab']['id']}]"
    body = "\n".join(lines[1:])
    assert re.search(r'^- heading "Account" level=2 \[e\d+\]$', body, re.M)
    # Mixed children keep their order; text runs and links each get a line.
    assert re.search(
        r'^- paragraph \[e\d+\]\n  - text: Read the\n  - link "terms" \[e\d+\]', body, re.M
    )
    # Unnamed generic wrappers vanish; text equal to the name is not repeated.
    assert re.search(r'^- button "Sign in" \[e\d+\]$', body, re.M)
    assert re.search(r'^- checkbox "Remember" checked \[e\d+\]$', body, re.M)
    assert re.search(r'^- textbox "Email" value="a@b.c" required \[e\d+\]$', body, re.M)
    assert re.search(r'^- button "Later" disabled \[e\d+\]$', body, re.M)
    assert re.search(r"^  - listitem \[e\d+\]: one$", body, re.M)
    for absent in ("InlineTextBox", "generic", "ListMarker", "hidden text", "StaticText"):
        assert absent not in body
    filtered = act(driver, action="snapshot", text="SIGN")
    assert filtered["snapshot"].splitlines()[1:] == [
        line for line in lines[1:] if "Sign in" in line
    ]
    nested = act(driver, action="snapshot", text="two")["snapshot"].splitlines()
    assert nested[1].startswith("- list ") and nested[2].endswith(": two") and len(nested) == 3


def test_huge_page_snapshot_is_complete_not_truncated(driver, site):
    # B6: size is handled by the Host's result budget and read_result, not here.
    items = "".join(f"<li>item-{index}</li>" for index in range(20000))
    result = act(driver, action="open", url=site.page("/huge", f"<ul>{items}</ul>"))
    assert len(result["snapshot"]) > 300_000
    assert result["snapshot"].rstrip().endswith(": item-19999")
    assert all(f": item-{index}" in result["snapshot"] for index in (0, 9999, 19999))


# Input through trusted events ---------------------------------------------------


def test_click_dispatches_trusted_mouse_events_on_the_element(driver, site):
    url = site.page(
        "/click",
        "<button style='margin-top:1500px' onclick=\"document.querySelector('output')"
        ".textContent='trusted '+event.isTrusted+' '+event.detail\">Go</button>"
        "<output>none</output>",
    )
    page = act(driver, action="open", url=url)
    result = act(driver, action="click", node=node(page["snapshot"], 'button "Go"'))
    # Scrolled into view first, then pressed and released at its centre.
    assert "trusted true 1" in result["snapshot"]
    assert result["tab"]["loading"] is False


def test_type_replaces_clears_and_keeps_unicode(driver, site):
    url = site.page(
        "/type",
        "<input aria-label=name value='old value'>"
        "<textarea aria-label=notes>line one</textarea>"
        "<div contenteditable aria-label=rich role=textbox>former</div>"
        "<output></output>"
        "<script>document.querySelector('input').addEventListener('input',"
        " e => document.querySelector('output').textContent = 'input:' + e.target.value)"
        "</script>",
    )
    page = act(driver, action="open", url=url)
    name = node(page["snapshot"], 'textbox "name"')
    typed = act(driver, action="type", node=name, text="中文🙂 text")
    assert 'textbox "name" value="中文🙂 text"' in typed["snapshot"]
    assert "input:中文🙂 text" in typed["snapshot"]
    cleared = act(driver, action="type", node=name, text="")
    assert f'- textbox "name" focused [{name}]' in cleared["snapshot"]
    notes = act(driver, action="type", node=node(page["snapshot"], '"notes"'), text="新しい")
    assert 'textbox "notes" value="新しい" focused' in notes["snapshot"]
    rich = act(driver, action="type", node=node(page["snapshot"], '"rich"'), text="替換")
    assert 'textbox "rich" value="替換" focused' in rich["snapshot"]


def test_enter_submits_the_form_through_full_key_events(driver, site):
    # B3: Enter is keyDown with key/code/keyCode/text, so implicit submission runs.
    site.page("/submitted", "<title>sent</title><p>received</p>")
    url = site.page("/form", "<form action='/submitted'><input aria-label=query name=q></form>")
    page = act(driver, action="open", url=url)
    query = node(page["snapshot"], 'textbox "query"')
    act(driver, action="type", node=query, text="雪 yuki")
    result = act(driver, action="press", node=query, text="Enter")
    destination = site.url("/submitted?q=%E9%9B%AA+yuki")
    assert {"type": "navigated", "url": destination} in result["events"]
    assert result["tab"] == {
        "id": page["tab"]["id"],
        "url": destination,
        "title": "sent",
        "loading": False,
    }
    assert re.search(r"^- paragraph \[e\d+\]: received$", result["snapshot"], re.M)
    assert "/submitted?q=%E9%9B%AA+yuki" in site.requests


def test_select_chooses_by_label_or_value_and_fires_change(driver, site):
    url = site.page(
        "/select",
        "<select aria-label=fruit onchange=\"document.querySelector('output')"
        ".textContent='changed '+this.value\">"
        "<option value=a>Apple</option><option value=b>Banana</option>"
        "<option value=c>Cherry</option></select><output></output>",
    )
    page = act(driver, action="open", url=url)
    fruit = node(page["snapshot"], 'combobox "fruit"')
    by_label = act(driver, action="select", node=fruit, text="Banana")
    assert 'combobox "fruit" value="Banana"' in by_label["snapshot"]
    assert "changed b" in by_label["snapshot"]
    by_value = act(driver, action="select", node=fruit, text="c")
    assert "changed c" in by_value["snapshot"]
    missing = act(driver, action="select", node=fruit, text="Durian")
    assert missing["error"] == "option_not_found"
    assert missing["options"] == ["Apple", "Banana", "Cherry"]
    assert "status" not in missing


def test_hover_and_scroll_move_the_page_without_clicking(driver, site):
    url = site.page(
        "/scroll",
        "<button onmouseover=\"document.title='hovered'\" "
        "onclick=\"document.title='clicked'\">Hover</button>"
        "<div style='height:5000px'></div><a id=end href='#'>End</a>"
        "<script>addEventListener('scroll', () => document.querySelector('output')"
        ".textContent = 'at ' + Math.round(scrollY))</script><output></output>",
    )
    page = act(driver, action="open", url=url)
    hovered = act(driver, action="hover", node=node(page["snapshot"], 'button "Hover"'))
    assert hovered["tab"]["title"] == "hovered"
    down = act(driver, action="scroll", text="down")
    assert re.search(r"at [1-9]\d*", down["snapshot"])
    act(driver, action="scroll", text="-100000")
    assert "at 0" in act(driver, action="snapshot")["snapshot"]
    to_end = act(driver, action="scroll", node=node(page["snapshot"], 'link "End"'))
    assert int(re.search(r"at (\d+)", to_end["snapshot"]).group(1)) > 3000


# Downloads, file choosers and dialogs (B3, B4) ----------------------------------


def test_download_lands_in_the_downloads_directory_with_its_path(driver, site, tmp_path):
    payload = os.urandom(200_000)
    site.file(
        "/report.bin",
        payload,
        Content_Type="application/octet-stream",
        Content_Disposition='attachment; filename="report.bin"',
    )
    page = act(driver, action="open", url=site.page("/downloads", "<a href='/report.bin'>Get</a>"))
    clicked = act(driver, action="click", node=node(page["snapshot"], 'link "Get"'))
    begun = [event for event in clicked["events"] if event["type"] == "download"]
    assert len(begun) == 1
    download = begun[0]
    assert download["suggested_filename"] == "report.bin"
    assert download["path"] == str(tmp_path / "downloads" / download["guid"])
    events = clicked["events"]
    deadline = time.monotonic() + 15
    while not any(
        event["type"] == "download" and event["state"] == "completed" for event in events
    ):
        assert time.monotonic() < deadline
        driver.pump(0.1)
        events = act(driver, action="snapshot")["events"]
    assert Path(download["path"]).read_bytes() == payload
    # The page itself stayed where it was.
    assert clicked["tab"]["url"].endswith("/downloads")


def test_file_input_is_intercepted_and_filled_with_paths(driver, site, tmp_path):
    first, second = tmp_path / "a.txt", tmp_path / "第二.txt"
    first.write_text("a")
    second.write_text("b")
    url = site.page(
        "/upload",
        "<input type=file multiple onchange=\"document.querySelector('output')"
        ".textContent=[...this.files].map(f=>f.name+':'+f.size).join(',')\"><output></output>",
    )
    page = act(driver, action="open", url=url)
    chooser = node(page["snapshot"], "Choose Files")
    clicked = act(driver, action="click", node=chooser)
    # No native dialog can open; the chooser is reported instead.
    assert {"type": "file_chooser", "node": chooser, "multiple": True} in clicked["events"]
    filled = act(driver, action="type", node=chooser, text=f"{first}\n{second}\n")
    assert "a.txt:1,第二.txt:1" in filled["snapshot"]


def test_dialogs_never_hang_and_are_answered_through_the_protocol(driver, site):
    url = site.page(
        "/dialogs",
        "<button onclick=\"alert('注意');document.title='after alert'\">Alert</button>"
        "<button onclick=\"document.title='confirm '+confirm('Sure?')\">Confirm</button>"
        "<button onclick=\"document.title='prompt '+prompt('Name?','Yuki')\">Prompt</button>",
    )
    page = act(driver, action="open", url=url)
    assert act(driver, action="accept_dialog")["error"] == "no_dialog"
    started = time.monotonic()
    alerted = act(driver, action="click", node=node(page["snapshot"], 'button "Alert"'))
    assert time.monotonic() - started < 10
    dialog = {"type": "dialog", "dialog_type": "alert", "message": "注意"}
    assert alerted["events"] == [dialog]
    assert "snapshot" not in alerted
    blocked = act(driver, action="snapshot")
    assert blocked["error"] == "dialog_open" and blocked["dialog"] == dialog
    accepted = act(driver, action="accept_dialog")
    assert accepted["tab"]["title"] == "after alert" and "snapshot" in accepted
    act(driver, action="click", node=node(page["snapshot"], 'button "Confirm"'))
    assert act(driver, action="dismiss_dialog")["tab"]["title"] == "confirm false"
    prompted = act(driver, action="click", node=node(page["snapshot"], 'button "Prompt"'))
    assert prompted["events"][-1] == {
        "type": "dialog",
        "dialog_type": "prompt",
        "message": "Name?",
        "default_prompt": "Yuki",
    }
    assert act(driver, action="accept_dialog", text="答案🙂")["tab"]["title"] == "prompt 答案🙂"


# Frames, staleness and tabs (B1, B2, B5) ----------------------------------------


def test_cross_site_iframe_is_out_of_process_reachable_and_ids_stay_put(driver, site, chromium):
    site.page(
        "/frame",
        "<title>inner</title><input aria-label=inner>"
        "<button onclick=\"document.querySelector('output').textContent="
        "'got '+document.querySelector('input').value\">Send</button><output></output>",
    )
    cross = site.url("/frame", host="localhost")
    url = site.page(
        "/frames",
        f"<p>top</p><iframe id=keep src='{cross}' style='border:6px solid;padding:9px'></iframe>"
        "<button onclick=\"const f=document.createElement('iframe');f.id='extra';"
        f"f.src='{site.url('/frame', host='localhost')}?extra';"
        "document.body.insertBefore(f, document.getElementById('keep'))\">Add</button>"
        "<button onclick=\"document.getElementById('extra').remove()\">Remove</button>",
    )
    page = act(driver, action="open", url=url)
    inner = node(page["snapshot"], 'textbox "inner"')
    prefix = inner.partition("_")[0]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(chromium + "/json/list") as response:
        targets = json.load(response)
    frames = [target for target in targets if target["type"] == "iframe" and target["url"] == cross]
    assert [target["id"][:8].lower() for target in frames] == [prefix]
    act(driver, action="type", node=inner, text="跨站")
    sent = act(driver, action="click", node=node(page["snapshot"], 'button "Send"'))
    assert "got 跨站" in sent["snapshot"]
    added = act(driver, action="click", node=node(page["snapshot"], 'button "Add"'))
    deadline = time.monotonic() + 10
    while added["snapshot"].count('textbox "inner"') < 2:
        assert time.monotonic() < deadline
        added = act(driver, action="snapshot")
    extra = [
        node(line, 'textbox "inner"')
        for line in added["snapshot"].splitlines()
        if 'textbox "inner"' in line and inner not in line
    ]
    assert len(extra) == 1 and extra[0].partition("_")[0] != prefix
    # A frame inserted before it does not shift the original frame's node IDs.
    assert inner in added["snapshot"]
    act(driver, action="type", node=inner, text="仍然")
    act(driver, action="click", node=node(page["snapshot"], 'button "Remove"'))
    resent = act(driver, action="click", node=node(page["snapshot"], 'button "Send"'))
    assert "got 仍然" in resent["snapshot"]
    gone = act(driver, action="type", node=extra[0], text="x")
    assert gone["error"] == "node_stale" and "status" not in gone
    assert inner in gone["snapshot"] and extra[0] not in gone["snapshot"]


def test_old_nodes_are_stale_after_navigation_and_never_hit_another_element(driver, site):
    first = site.page("/first", "<button onclick=\"document.title='first'\">One</button>")
    second = site.page(
        "/second", "<title>second</title><button onclick=\"document.title='wrong'\">Two</button>"
    )
    page = act(driver, action="open", url=first)
    old = node(page["snapshot"], 'button "One"')
    moved = act(driver, action="open", tab=page["tab"]["id"], url=second)
    assert moved["tab"]["id"] == page["tab"]["id"] and moved["events"][-1]["url"] == second
    stale = act(driver, action="click", node=old)
    assert stale["error"] == "node_stale"
    assert stale["tab"]["title"] == "second"
    assert 'button "Two"' in stale["snapshot"] and "status" not in stale
    assert act(driver, action="snapshot")["tab"]["title"] == "second"


def test_a_slow_load_returns_loading_without_a_half_built_snapshot(driver, site):
    # B1: the wait ends at the request's budget; that is success with loading, not an error.
    site.page("/slow/page", "<title>late</title><p>arrived</p>")
    page = act(driver, action="open", url=site.page("/fast", "<p>fast</p>"))
    started = time.monotonic()
    pending = act(
        driver, action="open", tab=page["tab"]["id"], url=site.url("/slow/page"), wait_seconds=1
    )
    assert time.monotonic() - started < 5
    assert pending["tab"]["loading"] is True
    assert "snapshot" not in pending and "error" not in pending
    site.release.set()
    deadline = time.monotonic() + 15
    while True:
        later = act(driver, action="snapshot")
        if not later["tab"]["loading"] and "arrived" in later["snapshot"]:
            break
        assert time.monotonic() < deadline
        driver.pump(0.1)
    assert later["tab"]["title"] == "late"
    assert {"type": "navigated", "url": site.url("/slow/page")} in later["events"]


def test_new_tabs_are_reachable_and_owned_by_the_opener_work(driver, site):
    site.page("/opened", "<title>opened</title><p>child</p>")
    url = site.page(
        "/opener",
        "<a href='/opened' target=_blank>Blank</a>"
        "<button onclick=\"window.open('/opened?script')\">Script</button>",
    )
    page = act(driver, owner="work-tabs", action="open", url=url)
    linked = act(driver, owner="work-tabs", action="click", node=node(page["snapshot"], "Blank"))
    opened = [event["tab"] for event in linked["events"] if event["type"] == "new_tab"]
    assert len(opened) == 1
    child = act(driver, owner="work-tabs", action="snapshot", tab=opened[0]["id"])
    assert child["tab"]["title"] == "opened" and ": child" in child["snapshot"]
    scripted = act(
        driver,
        owner="work-tabs",
        action="click",
        tab=page["tab"]["id"],
        node=node(page["snapshot"], "Script"),
    )
    second = [event["tab"]["id"] for event in scripted["events"] if event["type"] == "new_tab"]
    owners = {tab["id"]: tab.get("work_id") for tab in driver.tabs()}
    assert owners[opened[0]["id"]] == owners[second[0]] == "work-tabs"
    # Omitted tab: the Work's most recently used tab.
    assert act(driver, owner="work-tabs", action="snapshot")["tab"]["id"] == page["tab"]["id"]
    missing = act(driver, owner="another-work", action="snapshot")
    assert missing["error"] == "tab_not_found"
    assert {tab["id"] for tab in missing["tabs"]} >= {page["tab"]["id"], second[0]}
    closed = act(driver, owner="another-work", action="close", tab=second[0])
    assert closed == {"tab": {"id": second[0], "closed": True}, "events": []}
    gone = act(driver, owner="work-tabs", action="snapshot", tab=second[0])
    assert gone["error"] == "tab_not_found"


# Lease check before every input (C8) --------------------------------------------


def test_input_stops_when_the_lease_is_gone_and_reports_dispatched_inputs(driver, site):
    url = site.page(
        "/lease",
        "<button onclick=\"document.title='clicked'\">Press</button>"
        "<input aria-label=field value=keep>",
    )
    page = act(driver, action="open", url=url)
    button, field_ = node(page["snapshot"], "Press"), node(page["snapshot"], '"field"')
    before = act(driver, action="click", node=button, before_input=held_after(0))
    assert before == {"error": "control_held", "message": "接管", "control": {"holder": "human"}}
    # Scrolled and moved, then the human took over before the press.
    midway = act(driver, action="click", node=button, before_input=held_after(2))
    assert midway["error"] == "control_held" and midway["status"] == "unknown"
    # Focused, then taken over before the existing text was selected and replaced.
    typing = act(driver, action="type", node=field_, text="lost", before_input=held_after(1))
    assert typing["status"] == "unknown"
    read = act(driver, action="snapshot", before_input=held_after(0))
    assert read["tab"]["title"] != "clicked" and 'value="keep"' in read["snapshot"]
    image = execute(driver, action="screenshot", before_input=held_after(0))[1]
    assert image is not None


def test_browser_screenshot_is_sized_at_the_source(driver, site):
    act(driver, action="open", url=site.page("/shot", "<h1 style='font-size:90px'>畫面</h1>"))
    full, image = execute(driver, action="screenshot", max_dimension=4000)
    assert image is not None and image.media_type == "image/jpeg"
    width, height = jpeg_size(image.data)
    assert (full["width"], full["height"]) == (image.width, image.height) == (width, height)
    assert "畫面" in full["snapshot"]
    small, reduced = execute(driver, action="screenshot", max_dimension=300)
    assert reduced is not None
    scale = cdp.image_scale(width, height, 300, 1_500_000)
    assert jpeg_size(reduced.data) == (round(width * scale), round(height * scale))
    assert max(small["width"], small["height"]) == 300


# The serve loop and its file protocol (M3, P1, P2, P4, P5, P7) -------------------


def write_request(root: Path, request: dict[str, Any]) -> None:
    requests = root / "driver/requests"
    requests.mkdir(parents=True, exist_ok=True)
    temporary = requests / (".pending-" + request["id"])
    temporary.write_text(json.dumps(request))
    os.rename(temporary, requests / (request["id"] + ".json"))


def exchange(
    root: Path,
    tool: str,
    args: dict[str, Any],
    *,
    lease: str | None = None,
    owner: str | None = "work-file",
    max_dimension: int | None = 1280,
    seconds: float = 60,
) -> dict[str, Any]:
    identity = str(uuid4())
    write_request(
        root,
        {
            "id": identity,
            "tool": tool,
            "args": args,
            "lease_id": lease,
            "owner": owner,
            "max_dimension": max_dimension,
            "max_pixels": 1_500_000,
            "wait_seconds": 15,
        },
    )
    result = root / "driver/results" / (identity + ".json")
    deadline = time.monotonic() + seconds
    while not result.exists():
        assert time.monotonic() < deadline, args
        time.sleep(0.02)
    response = json.loads(result.read_text())
    result.unlink()
    assert not (root / "driver/taken" / (identity + ".json")).exists()
    if "image" in response:
        image = root / "driver/results" / response["image"]["file"]
        response["image"]["data"] = image.read_bytes()
        image.unlink()
    return response


def control(root: Path, holder: str, lease: str | None = None) -> None:
    value = {"holder": holder, "since": "2026-11-09T00:00:00Z"}
    if lease:
        value["lease_id"] = lease
    (root / "control.json").write_text(json.dumps(value))


def environment(**extra: str) -> dict[str, str]:
    path = os.pathsep.join(filter(None, [str(CONNECTOR), os.environ.get("PYTHONPATH")]))
    return {**os.environ, "PYTHONPATH": path, **extra}


def test_serve_answers_requests_through_runtime_files(chromium, site, tmp_path):
    root = tmp_path / "runtime"
    leftover = root / "driver/taken" / (str(uuid4()) + ".json")
    leftover.parent.mkdir(parents=True)
    leftover.write_text(json.dumps({"id": leftover.stem, "tool": "browser", "args": {}}))
    lease = str(uuid4())
    control(root, "agent", lease)
    process = subprocess.Popen(
        [
            sys.executable,
            str(CLI),
            "serve",
            "--cdp",
            chromium,
            "--downloads",
            str(tmp_path / "workspace/downloads"),
        ],
        env=environment(YUKI_COMPUTER_RUNTIME=str(root)),
    )
    try:
        url = site.page("/served", "<title>served</title><button>OK</button>")
        opened = exchange(root, "browser", {"action": "open", "url": url}, lease=lease)["result"]
        # A request claimed by a dead driver is never executed again (P1).
        assert not leftover.exists()
        assert not (root / "driver/results" / (leftover.stem + ".json")).exists()
        assert opened["tab"]["title"] == "served" and 'button "OK"' in opened["snapshot"]
        deadline = time.monotonic() + 10
        while True:
            listed = json.loads((root / "driver/tabs.json").read_text())["tabs"]
            if any(tab.get("work_id") == "work-file" for tab in listed):
                break
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert {
            "target_id": opened["tab"]["id"],
            "title": "served",
            "url": url,
            "work_id": "work-file",
        } in listed
        status = subprocess.run(
            [sys.executable, str(CLI), "status", "--json"],
            env=environment(YUKI_COMPUTER_RUNTIME=str(root)),
            capture_output=True,
            check=True,
        )
        assert json.loads(status.stdout)["tabs"] == listed
        # Screenshots travel as files next to the result, never in a pipe (M3).
        shot = exchange(root, "browser", {"action": "screenshot"}, max_dimension=400)
        assert shot["image"]["media_type"] == "image/jpeg"
        assert jpeg_size(shot["image"]["data"]) == (
            shot["image"]["width"],
            shot["image"]["height"],
        )
        assert max(shot["image"]["width"], shot["image"]["height"]) == 400
        assert list((root / "driver/results").iterdir()) == []
        # An unforeseen failure answers that one request; the driver keeps serving (P9).
        broken = exchange(root, "browser", {"action": "screenshot"}, max_dimension=None)["result"]
        assert broken["error"] == "driver_failed" and "status" not in broken
        assert process.poll() is None
        # A human holds the computer: no input, but reads still work.
        control(root, "human")
        held = exchange(
            root,
            "browser",
            {"action": "click", "node": node(opened["snapshot"], "OK")},
            lease=lease,
        )["result"]
        assert held["error"] == "control_held" and held["control"]["holder"] == "human"
        assert "status" not in held
        assert "snapshot" in exchange(root, "browser", {"action": "snapshot"})["result"]
        # Headless: no display for the computer tool (P5).
        screen = exchange(root, "computer", {"action": "screenshot"})["result"]
        assert screen["error"] == "display_unavailable"
        assert (
            not (tmp_path / "workspace/downloads").samefile(root)
            and (tmp_path / "workspace/downloads").is_dir()
        )
    finally:
        process.terminate()
        process.wait(timeout=15)


# The headed screen (M6, B7, C8) --------------------------------------------------

HEADED = ("Xvnc", "openbox-session", "xdotool", "import", "xwininfo", "xprop", "chromium")


@pytest.fixture(scope="module")
def desktop(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    if not all(shutil.which(binary) for binary in HEADED):
        pytest.skip("Requires the actual headed environment packages")
    home = tmp_path_factory.mktemp("desktop")
    root = home / "runtime"
    process = subprocess.Popen(
        [sys.executable, str(SERVICE)],
        env=environment(
            HOME=str(home),
            YUKI_COMPUTER_RUNTIME=str(root),
            YUKI_DESKTOP_WORKSPACE=str(home / "workspace"),
            YUKI_JOB_ID=str(uuid4()),
        ),
    )
    try:
        deadline = time.monotonic() + 90
        while not (root / "desktop-status.json").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.1)
        yield root
    finally:
        process.terminate()
        process.wait(timeout=30)


def screen(root: Path, args: dict[str, Any], **options: Any) -> dict[str, Any]:
    return exchange(root, "computer", args, **options)


def xwininfo(window: str) -> dict[str, int]:
    output = subprocess.run(
        ["xwininfo", "-id", window],
        env={**os.environ, "DISPLAY": ":1"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    fields = {
        key.strip(): value.strip()
        for key, _, value in (line.partition(":") for line in output.splitlines())
    }
    return {
        "x": int(fields["Absolute upper-left X"]),
        "y": int(fields["Absolute upper-left Y"]),
        "width": int(fields["Width"]),
        "height": int(fields["Height"]),
    }


RECORDER = (
    "<title>recorder</title>"
    "<textarea aria-label=pad style='position:fixed;inset:0;width:100%;height:100%'></textarea>"
    "<div id=log role=log aria-label=events></div><output id=move></output>"
    "<script>"
    "const log = document.getElementById('log');"
    "const pad = document.querySelector('textarea');"
    "for (const type of ['mousedown','mouseup','click','dblclick','contextmenu'])"
    " pad.addEventListener(type, e => { if (type === 'contextmenu') e.preventDefault();"
    " log.textContent += `|${type} ${e.screenX} ${e.screenY} ${e.button}`; });"
    "pad.addEventListener('wheel', e => { log.textContent += `|wheel ${Math.sign(e.deltaX)}"
    " ${Math.sign(e.deltaY)}`; });"
    "pad.addEventListener('mousemove', e => {"
    " document.getElementById('move').textContent = `moved ${e.screenX} ${e.screenY}`; });"
    "</script>"
)


def events(root: Path, tab: str) -> list[list[str]]:
    snapshot = exchange(root, "browser", {"action": "snapshot", "tab": tab})["result"]["snapshot"]
    line = next(line for line in snapshot.splitlines() if 'log "events"' in line)
    return [entry.split() for entry in line.partition("]: ")[2].split("|") if entry.strip()]


def moved(root: Path, tab: str) -> list[int]:
    snapshot = exchange(root, "browser", {"action": "snapshot", "tab": tab})["result"]["snapshot"]
    match = re.search(r"moved (\d+) (\d+)", snapshot)
    assert match is not None
    return [int(match.group(1)), int(match.group(2))]


def test_screen_images_map_coordinates_through_the_current_scale(desktop, site):
    root = desktop
    lease = str(uuid4())
    control(root, "agent", lease)
    tab = exchange(
        root, "browser", {"action": "open", "url": site.page("/recorder", RECORDER)}, lease=lease
    )["result"]["tab"]["id"]
    shot = screen(root, {"action": "screenshot"}, max_dimension=720)
    full = shot["result"]
    assert (full["width"], full["height"]) == (720, 450)
    assert jpeg_size(shot["image"]["data"]) == (720, 450)
    assert full["display"] == ":1" and full["control"]["holder"] == "agent"
    browsers = [window for window in full["windows"] if window["app"].lower() == "chromium"]
    assert browsers, full["windows"]
    window = browsers[0]["id"]
    geometry = xwininfo(window)
    centre = (geometry["x"] + geometry["width"] // 2, geometry["y"] + geometry["height"] // 2)
    scale = 720 / 1440
    point = {"x": round(centre[0] * scale), "y": round(centre[1] * scale)}
    expected = [round(point["x"] / scale), round(point["y"] / scale)]
    clicked = screen(root, {"action": "click", **point}, lease=lease, max_dimension=720)
    assert clicked["result"]["submitted"] is True and "image" in clicked
    assert ["click", *map(str, expected), "0"] in events(root, tab)
    # Window images: coordinates are relative to the window's own image.
    inside = screen(root, {"action": "screenshot", "window": window}, max_dimension=400)
    window_scale = cdp.image_scale(geometry["width"], geometry["height"], 400, 1_500_000)
    assert (inside["result"]["width"], inside["result"]["height"]) == (
        round(geometry["width"] * window_scale),
        round(geometry["height"] * window_scale),
    )
    assert inside["result"]["window"] == window
    relative = {"x": inside["result"]["width"] // 2, "y": inside["result"]["height"] // 2}
    screen(
        root,
        {"action": "double_click", "window": window, **relative},
        lease=lease,
        max_dimension=400,
    )
    target = [
        geometry["x"] + round(relative["x"] / window_scale),
        geometry["y"] + round(relative["y"] / window_scale),
    ]
    assert ["dblclick", *map(str, target), "0"] in events(root, tab)
    screen(root, {"action": "right_click", **point}, lease=lease, max_dimension=720)
    assert ["contextmenu", *map(str, expected), "2"] in events(root, tab)
    away = {"x": point["x"] + 10, "y": point["y"] + 10}
    screen(root, {"action": "move", **away}, lease=lease, max_dimension=720)
    assert moved(root, tab) == [round(away["x"] / scale), round(away["y"] / scale)]
    screen(
        root,
        {"action": "drag", **point, "x2": away["x"], "y2": away["y"]},
        lease=lease,
        max_dimension=720,
    )
    recorded = events(root, tab)
    assert ["mousedown", *map(str, expected), "0"] in recorded
    assert ["mouseup", str(round(away["x"] / scale)), str(round(away["y"] / scale)), "0"] in (
        recorded
    )
    screen(root, {"action": "scroll", **point, "dy": 2, "dx": -1}, lease=lease, max_dimension=720)
    recorded = events(root, tab)
    assert ["wheel", "0", "1"] in recorded and ["wheel", "-1", "0"] in recorded


def test_screen_types_unicode_and_keys_and_stops_without_the_lease(desktop, site):
    root = desktop
    lease = str(uuid4())
    control(root, "agent", lease)
    opened = exchange(
        root, "browser", {"action": "open", "url": site.page("/pad", RECORDER)}, lease=lease
    )["result"]
    pad = node(opened["snapshot"], 'textbox "pad"')
    exchange(root, "browser", {"action": "click", "node": pad}, lease=lease)
    typed = screen(root, {"action": "type", "text": "中文🙂 abc ÄÖÜ"}, lease=lease)
    assert typed["result"]["submitted"] is True
    snapshot = exchange(root, "browser", {"action": "snapshot"})["result"]["snapshot"]
    assert 'textbox "pad" value="中文🙂 abc ÄÖÜ"' in snapshot
    # More distinct characters than the display has spare keycodes.
    long = "".join(chr(0x4E00 + index * 7) for index in range(60)) + " 終わり"
    screen(root, {"action": "type", "text": "\n" + long}, lease=lease)
    snapshot = exchange(root, "browser", {"action": "snapshot"})["result"]["snapshot"]
    assert json.dumps("中文🙂 abc ÄÖÜ\n" + long, ensure_ascii=False) in snapshot
    screen(root, {"action": "key", "text": "ctrl+a"}, lease=lease)
    screen(root, {"action": "key", "text": "BackSpace"}, lease=lease)
    snapshot = exchange(root, "browser", {"action": "snapshot"})["result"]["snapshot"]
    assert re.search(r'textbox "pad" focused \[e\d+\]', snapshot)
    control(root, "human")
    held = screen(root, {"action": "type", "text": "never"}, lease=lease)["result"]
    assert held["error"] == "control_held" and "status" not in held
    snapshot = exchange(root, "browser", {"action": "snapshot"})["result"]["snapshot"]
    assert "never" not in snapshot
    # Reads keep working while a human holds control; resume reads like screenshot.
    resumed = screen(root, {"action": "resume"})
    assert resumed["result"]["control"]["holder"] == "human" and "windows" in resumed["result"]
    assert screen(root, {"action": "screenshot", "display": ":9"})["result"]["error"] == (
        "display_unavailable"
    )


def test_screen_reports_missing_and_minimized_windows(desktop):
    root = desktop
    for unknown in ("0x7ffffff", "Chromium", "0"):
        # Never handed to xwininfo as something it would wait on a click for.
        missing = screen(root, {"action": "screenshot", "window": unknown})["result"]
        assert missing["error"] == "window_not_found"
    windows = screen(root, {"action": "screenshot"})["result"]["windows"]
    window = next(window["id"] for window in windows if window["app"].lower() == "chromium")
    display = {**os.environ, "DISPLAY": ":1"}
    subprocess.run(["xdotool", "windowminimize", window], env=display, check=True)
    try:
        # The window manager iconifies asynchronously; wait for the fact itself.
        deadline = time.monotonic() + 10
        while (
            "IsUnMapped"
            not in subprocess.run(
                ["xwininfo", "-id", window], env=display, capture_output=True, text=True
            ).stdout
        ):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        hidden = screen(root, {"action": "screenshot", "window": window})["result"]
        assert hidden["error"] == "window_unavailable"
        assert "window" not in screen(root, {"action": "screenshot"})["result"]
        listed = screen(root, {"action": "screenshot"})["result"]["windows"]
        assert window not in [entry["id"] for entry in listed]
    finally:
        subprocess.run(["xdotool", "windowactivate", "--sync", window], env=display, check=True)


def test_closing_the_browser_keeps_the_screen_and_opens_the_browser_again(desktop, site):
    """The person closes Chromium's window on the shared desktop.

    Failure cases, written before the service changed:
    - the service ends with the browser and takes Xvnc down, so every window the person
      opened on the screen dies and the desktop comes back empty;
    - the screen stays but no browser comes back, leaving the browser tool nothing to drive;
    - a browser comes back but the driver still holds the closed CDP connection and never
      answers another request.
    """
    root = desktop
    display = {**os.environ, "DISPLAY": ":1"}
    lease = str(uuid4())
    control(root, "agent", lease)
    # A window the person started on the desktop themselves, outside the service.
    own = subprocess.Popen(["xmessage", "-name", "own-window", "kept"], env=display)

    def windows(*query: str) -> list[str]:
        found = subprocess.run(
            ["xdotool", "search", "--onlyvisible", *query],
            env=display,
            capture_output=True,
            text=True,
        )
        return found.stdout.split()

    def browser() -> str | None:
        # Each Chromium launch answers with its own browser ID; X reuses window IDs.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open("http://127.0.0.1:9222/json/version", timeout=1) as response:
                return str(json.load(response)["webSocketDebuggerUrl"])
        except OSError:
            return None

    try:
        deadline = time.monotonic() + 30
        while not windows("--classname", "own-window"):
            assert own.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        closed = browser()
        assert closed and windows("--class", "chromium")
        # As with its close button: each browser window is closed until none is left.
        for window in windows("--class", "chromium"):
            subprocess.run(["xdotool", "windowactivate", "--sync", window], env=display)
            subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+shift+w"], env=display)
        deadline = time.monotonic() + 60
        while browser() in (None, closed) or not windows("--class", "chromium"):
            assert own.poll() is None, "the person's own window went with the browser"
            assert time.monotonic() < deadline
            time.sleep(0.1)
        assert own.poll() is None and windows("--classname", "own-window")
        url = site.page("/reopened", "<title>reopened</title><button>Again</button>")
        result = exchange(root, "browser", {"action": "open", "url": url}, lease=lease)["result"]
        assert result["tab"]["title"] == "reopened" and 'button "Again"' in result["snapshot"]
    finally:
        own.terminate()
        own.wait(timeout=10)
