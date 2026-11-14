"""Whole-computer snapshots: the rootFS image and HOME copied as one point in time.

Only the Manager creates, rotates and restores them: the snapshot volume is mounted
into neither the environment nor the Bot, and every request arrives through the
Manager socket (Yuki's tool call, the person's Desktop command or the daily check).
Records live in the Manager state database and never roll back with the computer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from yuki.sandbox.persistent import PersistentManager

LOGGER = logging.getLogger(__name__)
IMAGE = "yuki-environment-snapshot"
LABEL = "io.yuki.snapshot"
CHROMIUM = "/.local/share/yuki-computer/chromium/**/"
# Read-only media cache mount point and Chromium caches that rebuild themselves.
EXCLUDES = (
    "/inputs/media-cache/",
    CHROMIUM + "Cache/",
    CHROMIUM + "Code Cache/",
    CHROMIUM + "GPUCache/",
)
# Inside the environment: the first rootFS entry changed after a time, skipping every
# mount point (HOME, /tmp, runtime and code files) and other file systems.
ROOTFS_CHANGED = """
import os, sys
since = float(sys.argv[1])
mounts = {line.split()[4] for line in open("/proc/self/mountinfo")} - {"/"}
root = os.lstat("/")
if root.st_ctime > since:
    print("/")
    raise SystemExit(0)
stack = ["/"]
while stack:
    directory = stack.pop()
    try:
        entries = list(os.scandir(directory))
    except OSError:
        continue
    for entry in entries:
        if entry.path in mounts:
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if info.st_dev != root.st_dev:
            continue
        if info.st_ctime > since:
            print(entry.path)
            raise SystemExit(0)
        if entry.is_dir(follow_symlinks=False):
            stack.append(entry.path)
"""


def _excluded(relative: str) -> bool:
    parts = relative.strip("/").split("/")
    if parts[:2] == ["inputs", "media-cache"]:
        return True
    chromium = [".local", "share", "yuki-computer", "chromium"]
    return parts[:4] == chromium and parts[-1] in {"Cache", "Code Cache", "GPUCache"}


def home_changed(home: Path, since: float) -> str | None:
    """The first HOME entry whose ctime is after ``since`` (creation, change, deletion)."""
    stack = [home]
    if home.lstat().st_ctime > since:
        return "/"
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            relative = os.path.relpath(entry.path, home)
            if _excluded(relative):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if info.st_ctime > since:
                return relative
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
    return None


def unique_bytes(root: Path) -> int:
    """Blocks used only by this copy: hard links shared with another snapshot have nlink > 1."""
    total = 0
    for directory, _names, files in os.walk(root):
        total += os.lstat(directory).st_blocks * 512
        for name in files:
            info = os.lstat(os.path.join(directory, name))
            if info.st_nlink == 1:
                total += info.st_blocks * 512
    return total


def volume_bytes(root: Path) -> int:
    seen: set[tuple[int, int]] = set()
    total = 0
    for directory, names, files in os.walk(root):
        for name in (*names, *files):
            info = os.lstat(os.path.join(directory, name))
            if (info.st_dev, info.st_ino) not in seen:
                seen.add((info.st_dev, info.st_ino))
                total += info.st_blocks * 512
    return total


class SnapshotError(RuntimeError):
    pass


class EnvironmentSnapshots:
    def __init__(
        self, manager: PersistentManager, root: Path, *, keep: int, interval_hours: float
    ) -> None:
        self.manager, self.root = manager, root
        self.keep, self.interval_hours = keep, interval_hours
        self._finished: dict[str, set[asyncio.Event]] = {}
        # When a restore that could not finish is tried again: scheduling, held in memory.
        self._restore_retry = 0.0
        manager.db.executescript(
            "CREATE TABLE IF NOT EXISTS environment_snapshots (id TEXT PRIMARY KEY,"
            "request_id TEXT UNIQUE NOT NULL, source TEXT NOT NULL, actor TEXT NOT NULL,"
            "note TEXT, state TEXT NOT NULL, image TEXT, point_at REAL, container_id TEXT,"
            "sizes TEXT, pause_seconds REAL, rotated TEXT, error TEXT, created REAL NOT NULL);"
            "CREATE TABLE IF NOT EXISTS environment_restores (request_id TEXT PRIMARY KEY,"
            "snapshot_id TEXT NOT NULL, actor TEXT NOT NULL, state TEXT NOT NULL,"
            "started REAL NOT NULL, finished REAL, home_restored REAL, generation TEXT,"
            "error TEXT);"
            # Image tags the retired Docker checkpoint path pointed at no longer exist.
            "DELETE FROM environment_state WHERE key LIKE 'checkpoint:%' "
            "OR key='previous_checkpoint_image';"
        )
        manager.db.commit()

    # ---- records -------------------------------------------------------------------

    def view(self, row: Any) -> dict[str, Any]:
        sizes = json.loads(row["sizes"]) if row["sizes"] else {}
        return {
            "id": row["id"],
            "request_id": row["request_id"],
            "state": row["state"],
            "source": row["source"],
            "by": json.loads(row["actor"]),
            "note": row["note"],
            "created_at": row["created"],
            "point_at": row["point_at"],
            "image": row["image"],
            "size_bytes": sum(sizes.values()) if sizes else None,
            "sizes": sizes or None,
            "pause_seconds": row["pause_seconds"],
            "rotated": json.loads(row["rotated"]) if row["rotated"] else [],
            "error": row["error"],
        }

    def restore_view(self, row: Any) -> dict[str, Any]:
        return {
            "request_id": row["request_id"],
            "snapshot_id": row["snapshot_id"],
            "state": row["state"],
            "by": json.loads(row["actor"]),
            "started_at": row["started"],
            "finished_at": row["finished"],
            "generation": row["generation"],
            "error": row["error"],
        }

    def _snapshot(self, **where: str) -> Any:
        ((column, value),) = where.items()
        return self.manager.db.execute(
            f"SELECT * FROM environment_snapshots WHERE {column}=?", (value,)
        ).fetchone()

    def _restore(self, request_id: str) -> Any:
        return self.manager.db.execute(
            "SELECT * FROM environment_restores WHERE request_id=?", (request_id,)
        ).fetchone()

    def find(self, request_id: str) -> dict[str, Any] | None:
        """The original snapshot or restore of a request key, whichever it was."""
        if row := self._snapshot(request_id=request_id):
            return {"snapshot": self.view(row)}
        if row := self._restore(request_id):
            return {"restore": self.restore_view(row)}
        return None

    def ready(self) -> list[Any]:
        return self.manager.db.execute(
            "SELECT * FROM environment_snapshots WHERE state='ready' ORDER BY point_at DESC"
        ).fetchall()

    def busy(self) -> dict[str, Any] | None:
        db = self.manager.db
        if row := db.execute(
            "SELECT request_id FROM environment_snapshots WHERE state='creating'"
        ).fetchone():
            return {"error": "snapshot_in_progress", "request_id": row[0]}
        if row := db.execute(
            "SELECT request_id FROM environment_restores WHERE state='accepted'"
        ).fetchone():
            return {"error": "restore_in_progress", "request_id": row[0]}
        return None

    def status(self) -> dict[str, Any]:
        ready = self.ready()
        newest = ready[0]["point_at"] if ready else 0
        listed = self.manager.db.execute(
            "SELECT * FROM environment_snapshots WHERE state IN ('creating','ready') "
            "OR (state='failed' AND created>?) ORDER BY created DESC",
            (newest,),
        ).fetchall()
        last = self.manager.db.execute(
            "SELECT * FROM environment_restores ORDER BY started DESC LIMIT 1"
        ).fetchone()
        recreated = self.manager.setting("recreated_from")
        storage = self.manager.setting("snapshot_storage_bytes")
        return {
            "snapshots": [self.view(row) for row in listed],
            "snapshot_keep": self.keep,
            "snapshot_interval_hours": self.interval_hours,
            "snapshot_storage_bytes": int(storage) if storage else None,
            "last_restore": self.restore_view(last) if last else None,
            "recreated_from": json.loads(recreated) if recreated else None,
        }

    # ---- requests ------------------------------------------------------------------

    def request_snapshot(
        self, request_id: str, source: str, actor: object, note: object
    ) -> dict[str, Any]:
        """Accept one snapshot; the same request key always answers its original record."""
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
            raise ValueError("invalid_request_id")
        if source not in {"daily", "yuki", "person"}:
            raise ValueError("invalid_snapshot_source")
        if note is not None and not isinstance(note, str):
            raise ValueError("invalid_snapshot_note")
        if not isinstance(actor, dict):
            raise ValueError("invalid_snapshot_actor")
        if existing := self.find(request_id):
            return existing if "snapshot" in existing else {"error": "idempotency_conflict"}
        if busy := self.busy():
            return busy
        if not self.manager.ready:
            return {"error": "environment_unavailable"}
        identity = str(uuid4())
        with self.manager.db:
            self.manager.db.execute(
                "INSERT INTO environment_snapshots (id,request_id,source,actor,note,state,created)"
                " VALUES (?,?,?,?,?,'creating',?)",
                (identity, request_id, source, json.dumps(actor), note, time.time()),
            )
            self.manager.computer._bump()
        self.manager.computer.notify()
        return {"snapshot": self.view(self._snapshot(id=identity))}

    def request_restore(
        self, request_id: str, snapshot_id: object, actor: object
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
            raise ValueError("invalid_request_id")
        if not isinstance(actor, dict):
            raise ValueError("invalid_snapshot_actor")
        if existing := self.find(request_id):
            if "restore" not in existing or existing["restore"]["snapshot_id"] != snapshot_id:
                return {"error": "idempotency_conflict"}
            return existing
        if busy := self.busy():
            return busy
        target = self._snapshot(id=snapshot_id) if isinstance(snapshot_id, str) else None
        if target is None or target["state"] != "ready":
            return {"error": "snapshot_not_found"}
        with self.manager.db:
            self.manager.db.execute(
                "INSERT INTO environment_restores (request_id,snapshot_id,actor,state,started)"
                " VALUES (?,?,?,'accepted',?)",
                (request_id, snapshot_id, json.dumps(actor), time.time()),
            )
            self.manager.computer._bump()
        self.manager.computer.notify()
        return {"restore": self.restore_view(self._restore(request_id))}

    async def wait(self, request_id: str) -> dict[str, Any]:
        """Observe one accepted request until it ends; cancelling the wait never cancels it."""
        changed = asyncio.Event()
        waiters = self._finished.setdefault(request_id, set())
        waiters.add(changed)
        try:
            while True:
                changed.clear()
                current = self.find(request_id)
                assert current is not None
                record = current.get("snapshot") or current["restore"]
                # A restore attempt that could not finish reports its error while the
                # Manager keeps converging on the same snapshot.
                if record["state"] not in {"creating", "accepted"} or record["error"]:
                    return current
                await changed.wait()
        finally:
            waiters.discard(changed)
            if not waiters:
                self._finished.pop(request_id, None)

    def _changed(self, request_id: str) -> None:
        with self.manager.db:
            self.manager.computer._bump()
        self.manager.computer.notify()
        for waiter in self._finished.get(request_id, ()):
            waiter.set()

    # ---- worker --------------------------------------------------------------------

    async def step(self) -> None:
        """Run accepted work in the Manager worker; no new terminal run is dispatched meanwhile.

        Screen and browser actions hold the runtime lock while they may wait for this
        worker to start the desktop service, so the step waits for the next round
        instead of for that lock.
        """
        lock = self.manager.desktop.lock
        if lock.locked():
            return
        db = self.manager.db
        if restore := db.execute(
            "SELECT * FROM environment_restores WHERE state='accepted'"
        ).fetchone():
            if time.monotonic() < self._restore_retry:
                return
            async with lock:
                self.manager.ready = False
                try:
                    await self.manager.recover()
                except Exception as exc:
                    # It converges on the same snapshot; the next try follows the
                    # worker's own recovery cadence.
                    self._restore_retry = time.monotonic() + 15
                    LOGGER.warning(
                        "environment_restore_pending request_id=%s error=%s", restore[0], exc
                    )
                    with db:
                        db.execute(
                            "UPDATE environment_restores SET error=? WHERE request_id=? "
                            "AND state='accepted'",
                            (str(exc)[-2048:] or type(exc).__name__, restore[0]),
                        )
                    self._changed(restore[0])
            return
        if pending := db.execute(
            "SELECT * FROM environment_snapshots WHERE state='creating' ORDER BY created"
        ).fetchone():
            async with lock:
                await self._finish(pending)
            return
        await self._daily()

    async def _daily(self) -> None:
        if not self.manager.ready or self.interval_hours <= 0:
            return
        now, interval = time.time(), self.interval_hours * 3600
        ready = self.ready()
        newest = ready[0]["point_at"] if ready else None
        checked = float(self.manager.setting("snapshot_daily_checked_at", "0"))
        if (newest is not None and now - newest < interval) or now - checked < interval:
            return
        # Not while the person operates the screen: a pause would freeze it. This only
        # moves the check to after the control is returned.
        if self.manager.computer.control()["holder"] == "human":
            return
        changed = newest is None or await self.changed_since(newest)
        self.manager.set_setting("snapshot_daily_checked_at", str(now))
        if changed:
            accepted = self.request_snapshot(f"daily:{uuid4()}", "daily", {"system": "daily"}, None)
            if "snapshot" in accepted:
                async with self.manager.desktop.lock:
                    await self._finish(self._snapshot(id=accepted["snapshot"]["id"]))

    async def changed_since(self, since: float) -> bool:
        if await asyncio.to_thread(home_changed, self.manager.home, since):
            return True
        code, output = await self.manager.command(
            "docker",
            "exec",
            self.manager.name,
            "/usr/local/bin/python",
            "-I",
            "-c",
            ROOTFS_CHANGED,
            repr(since),
            deadline_seconds=600,
        )
        if code:
            raise SnapshotError("rootfs_scan_failed: " + output.decode(errors="replace")[-512:])
        return bool(output.strip())

    async def _docker(self, *args: str, deadline: float = 1800) -> bytes:
        code, output = await self.manager.command("docker", *args, deadline_seconds=deadline)
        if code:
            raise SnapshotError(" ".join(args[:2]) + ": " + output.decode(errors="replace")[-1024:])
        return output

    async def _rsync(self, source: Path, target: Path, *extra: str) -> None:
        excludes = [f"--exclude={pattern}" for pattern in EXCLUDES]
        code, output = await self.manager.command(
            "rsync",
            "-aHAX",
            "--numeric-ids",
            "--delete",
            *excludes,
            *extra,
            f"{source}/",
            f"{target}/",
            deadline_seconds=86400,
        )
        if code:
            raise SnapshotError("rsync: " + output.decode(errors="replace")[-1024:])

    async def _finish(self, row: Any) -> None:
        """Create one accepted snapshot; whatever happens, its waiters see the outcome."""
        try:
            await self._create(row)
        except Exception as exc:
            with self.manager.db:
                self.manager.db.execute(
                    "UPDATE environment_snapshots SET state='failed',error=? "
                    "WHERE id=? AND state='creating'",
                    (str(exc)[-2048:] or type(exc).__name__, row["id"]),
                )
            raise
        finally:
            self._changed(row["request_id"])

    async def _create(self, row: Any) -> None:
        identity, request_id = row["id"], row["request_id"]
        image = f"{IMAGE}:{identity}"
        partial, final = self.root / f"{identity}.partial", self.root / identity
        ready = self.ready()
        previous = ready[0]["id"] if ready else None
        generation = self.manager.container_id
        committed = paused = False
        started = 0.0
        try:
            await self._docker("exec", self.manager.name, "sync", deadline=600)
            await self._docker("pause", self.manager.name)
            paused, started = True, time.monotonic()
            point_at = time.time()
            try:
                await self._docker(
                    "commit",
                    "--pause=false",
                    "-c",
                    f"LABEL {LABEL}={identity}",
                    "-c",
                    f"LABEL io.yuki.sandbox.project={self.manager.project}",
                    self.manager.name,
                    image,
                )
                committed = True
                await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)
                link = (f"--link-dest=../{previous}/",) if previous else ()
                await self._rsync(self.manager.home, partial, *link)
                await asyncio.to_thread(partial.rename, final)
            finally:
                paused = False
                pause_seconds = time.monotonic() - started
                await self._docker("unpause", self.manager.name)
            current = await self.manager.inspect_container(size=True)
            if (
                current is None
                or not current["State"]["Running"]
                or current["Id"] + ":" + current["State"]["StartedAt"] != generation
            ):
                # Under runsc an OOM during the pause ends the whole environment.
                raise SnapshotError("environment_stopped_during_snapshot")
            sizes = {
                "rootfs_bytes": int(current.get("SizeRw") or 0),
                "home_bytes": await asyncio.to_thread(unique_bytes, final),
            }
        except Exception as exc:
            if paused:
                await self.manager.command("docker", "unpause", self.manager.name)
            await asyncio.to_thread(shutil.rmtree, partial, ignore_errors=True)
            await asyncio.to_thread(shutil.rmtree, final, ignore_errors=True)
            if committed:
                await self.manager.command("docker", "image", "rm", image)
            with self.manager.db:
                self.manager.db.execute(
                    "UPDATE environment_snapshots SET state='failed',error=? WHERE id=?",
                    (str(exc)[-2048:] or type(exc).__name__, identity),
                )
            self._changed(request_id)
            return
        with self.manager.db:
            self.manager.db.execute(
                "UPDATE environment_snapshots SET state='ready',image=?,point_at=?,"
                "container_id=?,sizes=?,pause_seconds=? WHERE id=?",
                (image, point_at, generation, json.dumps(sizes), pause_seconds, identity),
            )
            self.manager.db.execute(
                "INSERT OR REPLACE INTO environment_state VALUES ('checkpoint_image',?)", (image,)
            )
        rotated = await self._rotate()
        with self.manager.db:
            self.manager.db.execute(
                "UPDATE environment_snapshots SET rotated=? WHERE id=?",
                (json.dumps(rotated), identity),
            )
        self.manager.set_setting(
            "snapshot_storage_bytes", str(await asyncio.to_thread(volume_bytes, self.root))
        )
        self._changed(request_id)

    async def _rotate(self) -> list[str]:
        """Only after a new snapshot is ready: drop the oldest beyond the kept count."""
        rotated = []
        for row in self.ready()[self.keep :]:
            await asyncio.to_thread(shutil.rmtree, self.root / row["id"], ignore_errors=True)
            # Refused while the running container is based on it; recover() removes
            # the tag once no container uses it.
            await self.manager.command("docker", "image", "rm", row["image"])
            with self.manager.db:
                self.manager.db.execute(
                    "UPDATE environment_snapshots SET state='rotated' WHERE id=?", (row["id"],)
                )
            rotated.append(row["id"])
        return rotated

    # ---- recovery ------------------------------------------------------------------

    async def before_recover(self, current: dict[str, Any] | None) -> dict[str, Any] | None:
        """Finish what an ended Manager left: a paused container, a snapshot, a restore."""
        if current is not None and current["State"].get("Paused"):
            await self._docker("unpause", self.manager.name)
            current = await self.manager.inspect_container()
        for row in self.manager.db.execute(
            "SELECT * FROM environment_snapshots WHERE state='creating'"
        ).fetchall():
            await asyncio.to_thread(
                shutil.rmtree, self.root / f"{row['id']}.partial", ignore_errors=True
            )
            await asyncio.to_thread(shutil.rmtree, self.root / row["id"], ignore_errors=True)
            await self.manager.command("docker", "image", "rm", f"{IMAGE}:{row['id']}")
            with self.manager.db:
                self.manager.db.execute(
                    "UPDATE environment_snapshots SET state='failed',error=? WHERE id=?",
                    ("interrupted", row["id"]),
                )
            self._changed(row["request_id"])
        for partial in await asyncio.to_thread(lambda: list(self.root.glob("*.partial"))):
            await asyncio.to_thread(shutil.rmtree, partial, ignore_errors=True)
        restore = self.manager.db.execute(
            "SELECT * FROM environment_restores WHERE state='accepted'"
        ).fetchone()
        if restore is None or restore["home_restored"]:
            return current
        snapshot = self._snapshot(id=restore["snapshot_id"])
        code, _ = await self.manager.command("docker", "image", "inspect", snapshot["image"])
        if code:
            # Never fall back to another image, and leave the current computer as it is.
            with self.manager.db:
                self.manager.db.execute(
                    "UPDATE environment_restores SET state='failed',finished=?,error=? "
                    "WHERE request_id=?",
                    (time.time(), "snapshot_image_missing", restore["request_id"]),
                )
            self._changed(restore["request_id"])
            return current
        for job in self.manager.active():
            if job["dispatched"]:
                self.manager.finish(
                    job["id"],
                    "failed",
                    {
                        "error": "environment_restored",
                        "interrupted": True,
                        "snapshot_id": snapshot["id"],
                    },
                )
        if current is not None:
            await self._docker("stop", "--time", "10", self.manager.name, deadline=120)
            await self._docker("rm", self.manager.name, deadline=120)
            current = None
        # The environment is gone: HOME is only ever restored while nothing runs in it.
        await self._rsync(self.root / snapshot["id"], self.manager.home)
        with self.manager.db:
            self.manager.db.execute(
                "INSERT OR REPLACE INTO environment_state VALUES ('checkpoint_image',?)",
                (snapshot["image"],),
            )
            self.manager.db.execute(
                "UPDATE environment_restores SET home_restored=? WHERE request_id=?",
                (time.time(), restore["request_id"]),
            )
        return current

    def created_from(self, image: str) -> None:
        """Record which snapshot a newly created container came from."""
        row = self._snapshot(image=image)
        value = (
            json.dumps({"snapshot_id": row["id"], "point_at": row["point_at"], "at": time.time()})
            if row
            else None
        )
        with self.manager.db:
            if value:
                self.manager.db.execute(
                    "INSERT OR REPLACE INTO environment_state VALUES ('recreated_from',?)",
                    (value,),
                )
            else:
                self.manager.db.execute("DELETE FROM environment_state WHERE key='recreated_from'")

    async def after_recover(self, *, created: bool) -> None:
        restore = self.manager.db.execute(
            "SELECT * FROM environment_restores WHERE state='accepted' AND home_restored "
            "IS NOT NULL"
        ).fetchone()
        if restore is not None:
            with self.manager.db:
                self.manager.db.execute(
                    "UPDATE environment_restores SET state='done',finished=?,generation=?,"
                    "error=NULL WHERE request_id=?",
                    (time.time(), self.manager.container_id, restore["request_id"]),
                )
            self._changed(restore["request_id"])
        if not created:
            # The same container still uses the image it was created from.
            return
        kept = {row["image"] for row in self.ready()}
        output = await self.manager.command(
            "docker",
            "image",
            "ls",
            "--filter",
            f"label={LABEL}",
            "--format",
            "{{.Repository}}:{{.Tag}}",
        )
        for tag in output[1].decode(errors="replace").split() if not output[0] else ():
            if tag in kept or not tag.startswith(IMAGE + ":"):
                continue
            code, project = await self.manager.command(
                "docker",
                "image",
                "inspect",
                "-f",
                '{{index .Config.Labels "io.yuki.sandbox.project"}}',
                tag,
            )
            if not code and project.decode().strip() == self.manager.project:
                # Docker refuses while a container still uses it; the next recovery retries.
                await self.manager.command("docker", "image", "rm", tag)
