"""Controlled index selection with real immutable workspace snapshots."""

import asyncio
import hashlib
from io import BytesIO
from tempfile import TemporaryFile
from uuid import uuid4

from PIL import Image

from yuki.emoji.delivery import EmojiDeliveryService
from yuki.emoji.library import snapshot_identity
from yuki.emoji.models import EmojiAsset
from yuki.emoji.sticker_index import StickerIndex
from yuki.services.media_resolver import _media_io


class DirectoryBridge:
    """Real independent file index and snapshots, replacing only the RPC transport."""

    def __init__(self, root, store):
        self.root, self.store = root, store
        root.mkdir(parents=True)

    async def execute(self, method, args, *, request_id):
        if method == "sticker_index":
            return await _media_io(StickerIndex(self.root).execute, args)
        if method == "workspace_checkout":
            metadata, content = self.store.read_bytes(args["artifact_id"])
            target = self.root / args["destination"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            return {"path": args["destination"], "version": metadata["sha256"]}
        if method == "workspace_publish":
            with (self.root / args["path"]).open("rb") as stream:
                result = self.store.snapshot(
                    stream.fileno(),
                    args["name"],
                    artifact_id=args["artifact_id"],
                    source_key=args["source_key"],
                    expected_version=args["expected_version"],
                )
            return {**result, "version": result["sha256"]}
        raise AssertionError(method)


class StickerLibrary:
    def __init__(self, store):
        content = BytesIO()
        Image.new("RGB", (4, 4), "yellow").save(content, format="PNG")
        self.content = content.getvalue()
        self.store = store
        self.asset = EmojiAsset(
            id=str(uuid4()),
            platform="qq",
            sha256=hashlib.sha256(self.content).hexdigest(),
            relative_path="stickers/qq/files/smile.png",
            mime_type="image/png",
            image_format="png",
            byte_size=len(self.content),
            description="开心的笑脸",
            tags=("开心",),
            is_sticker=True,
        )
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.select_calls = 0
        self.selected_platforms = []
        self.publish_calls = 0

    async def select(self, goal, emotion="", *, platform):
        self.select_calls += 1
        self.selected_platforms.append(platform)
        self.entered.set()
        await self.release.wait()
        return self.asset if self.asset is not None and self.asset.platform == platform else None

    async def publish(self, asset, *, request_id):
        self.publish_calls += 1
        with TemporaryFile() as stream:
            stream.write(self.content)
            stream.flush()
            return self.store.snapshot(
                stream.fileno(),
                f"smile.{asset.image_format}",
                artifact_id=snapshot_identity(asset.platform, asset.sha256),
                source_key=f"sticker:{asset.platform}:{asset.sha256}",
            )


def make_delivery(env):
    library = StickerLibrary(env.store)
    delivery = EmojiDeliveryService(env.service, library)
    env.service.emoji_delivery = delivery
    return delivery, library


async def settle(delivery, context):
    async with asyncio.timeout(3):
        while True:
            intent = await delivery.intents.find(context.turn_id, context.call_id)
            if intent is not None and intent.closed:
                return await delivery.project(context)
            await asyncio.sleep(0.01)
