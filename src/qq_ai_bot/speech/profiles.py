"""Strict manifest validation and atomic local voice-profile management."""

from __future__ import annotations

import hashlib
import json
import shutil
import tomllib
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.services.plugin_events import LifecycleEventPublisher, publish_notification
from qq_ai_bot.speech.models import (
    VoiceManifestReference,
    VoiceProfile,
    VoiceProfileManifest,
    VoiceReference,
)
from qq_ai_bot.speech.paths import SpeechPathError, SpeechPathPolicy
from qq_ai_bot.speech.repository import VoiceProfileRepository
from yuki_plugin_sdk.events import EventName


class ProfileLoader(Protocol):
    async def load_profile(self, profile: VoiceProfile, *, reload: bool = False) -> None: ...

    async def unload_profile(self, profile_id: str) -> None: ...


class VoiceProfileService:
    def __init__(
        self,
        *,
        repository: VoiceProfileRepository,
        paths: SpeechPathPolicy,
        loader: ProfileLoader | None = None,
        event_publisher: LifecycleEventPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._paths = paths
        self._loader = loader
        self._event_publisher = event_publisher

    def set_event_publisher(self, publisher: LifecycleEventPublisher) -> None:
        self._event_publisher = publisher

    def discover_profiles(self) -> tuple[str, ...]:
        self._paths.ensure_layout()
        voices = self._paths.resolve("voices")
        return tuple(
            item.name
            for item in sorted(voices.iterdir())
            if item.is_dir() and (item / "profile.toml").is_file()
        )

    def validate_profile(self, profile_directory: Path) -> VoiceProfileManifest:
        manifest_path = profile_directory / "profile.toml"
        if not manifest_path.is_file():
            raise FileNotFoundError("profile.toml is missing")
        try:
            payload = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = VoiceProfileManifest.model_validate_json(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
        except (OSError, tomllib.TOMLDecodeError, UnicodeError, ValidationError) as exc:
            raise ValueError("invalid voice profile manifest") from exc
        model = self._inside_directory(profile_directory, manifest.model.path)
        if not model.is_dir() or not any(model.iterdir()):
            raise ValueError("voice model directory is missing or empty")
        for reference in manifest.references:
            audio = self._inside_directory(profile_directory, reference.audio)
            if not audio.is_file():
                raise ValueError(f"voice reference is missing: {reference.id}")
        return manifest

    async def import_profile(self, source_directory: Path) -> VoiceProfile:
        manifest = self.validate_profile(source_directory)
        self._paths.ensure_layout()
        staging, destination = self._paths.stage_import(source_directory, manifest.id)
        try:
            staged_manifest = self.validate_profile(staging)
            self._paths.commit_import(staging, destination)
            profile = await self._persist_manifest(staged_manifest, destination)
            await self._load(profile)
            return profile
        except (OSError, ValueError, LookupError):
            if staging.exists():
                shutil.rmtree(staging)
            if destination.exists():
                shutil.rmtree(destination)
            raise

    async def reload_profile(self, profile_id: str) -> VoiceProfile:
        directory = self._paths.profile_root(profile_id, must_exist=True)
        manifest = self.validate_profile(directory)
        if manifest.id != profile_id:
            raise ValueError("profile id does not match its directory")
        profile = await self._persist_manifest(manifest, directory)
        await self._load(profile, reload=True)
        return profile

    async def sync_profile_metadata(self, profile_id: str) -> VoiceProfile:
        """Refresh persisted manifest metadata without warming the synthesis model."""

        directory = self._paths.profile_root(profile_id, must_exist=True)
        manifest = self.validate_profile(directory)
        if manifest.id != profile_id:
            raise ValueError("profile id does not match its directory")
        return await self._persist_manifest(manifest, directory)

    async def activate_profile(self, profile_id: str) -> VoiceProfile:
        profile = await self._repository.activate(profile_id)
        await self._load(profile)
        return profile

    async def disable_profile(self, profile_id: str) -> VoiceProfile:
        profile = await self._repository.set_enabled(profile_id, enabled=False)
        if self._loader is not None:
            await self._loader.unload_profile(profile_id)
        return profile

    async def enable_profile(self, profile_id: str) -> VoiceProfile:
        profile = await self._repository.set_enabled(profile_id, enabled=True)
        await self._load(profile)
        return profile

    async def list_profiles(self, *, enabled_only: bool = False) -> tuple[VoiceProfile, ...]:
        return await self._repository.list_profiles(enabled_only=enabled_only)

    async def get_profile(
        self, profile_id: str, *, session: AsyncSession | None = None
    ) -> VoiceProfile | None:
        return await self._repository.get_profile(profile_id, session=session)

    async def list_styles(self, profile_id: str) -> tuple[str, ...]:
        profile = await self._required(profile_id)
        return tuple(dict.fromkeys(item.style for item in profile.references if item.enabled))

    async def disable_reference(self, profile_id: str, reference_key: str) -> VoiceReference:
        profile = await self._required(profile_id)
        target = next(
            (item for item in profile.references if item.reference_key == reference_key), None
        )
        if target is None:
            raise LookupError("voice reference not found")
        if target.style == profile.default_style:
            raise ValueError("default voice reference cannot be disabled")
        return await self._repository.set_reference_enabled(
            profile_id, reference_key, enabled=False
        )

    async def add_reference(self, profile_id: str, source: Path) -> VoiceReference:
        """Import a local reference plus strict sidecar metadata and reload the profile."""

        profile_directory = self._paths.profile_root(profile_id, must_exist=True)
        manifest = self.validate_profile(profile_directory)
        metadata_path, audio_source = self._reference_source(source)
        try:
            payload = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeError) as exc:
            raise ValueError("invalid reference metadata") from exc
        if audio_source is None:
            audio_value = str(payload.get("audio") or "").strip()
            if not audio_value:
                raise ValueError("reference.toml must specify audio")
            audio_source = self._inside_directory(source, audio_value)
        if audio_source is None or not audio_source.is_file():
            raise FileNotFoundError("reference audio is missing")
        payload["audio"] = f"references/{payload.get('id', '')}{audio_source.suffix.lower()}"
        try:
            reference = VoiceManifestReference.model_validate_json(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            updated = manifest.model_copy(update={"references": (*manifest.references, reference)})
            updated = VoiceProfileManifest.model_validate(updated.model_dump())
        except ValidationError as exc:
            raise ValueError("invalid reference metadata") from exc
        destination = self._inside_directory(profile_directory, reference.audio)
        if destination.exists():
            raise FileExistsError("voice reference already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        manifest_path = profile_directory / "profile.toml"
        previous_manifest = manifest_path.read_bytes()
        audio_staging = destination.with_suffix(destination.suffix + ".tmp")
        manifest_staging = manifest_path.with_suffix(".toml.tmp")
        try:
            shutil.copy2(audio_source, audio_staging)
            audio_staging.replace(destination)
            manifest_staging.write_text(_manifest_toml(updated), encoding="utf-8")
            manifest_staging.replace(manifest_path)
            loaded = await self.reload_profile(profile_id)
        except (OSError, ValueError, LookupError):
            manifest_path.write_bytes(previous_manifest)
            destination.unlink(missing_ok=True)
            audio_staging.unlink(missing_ok=True)
            manifest_staging.unlink(missing_ok=True)
            raise
        return next(item for item in loaded.references if item.reference_key == reference.id)

    async def set_default_style(self, profile_id: str, style: str) -> VoiceProfile:
        return await self._repository.set_default_style(profile_id, style)

    async def doctor(self) -> dict[str, object]:
        self._paths.ensure_layout()
        discovered = self.discover_profiles()
        valid: list[str] = []
        invalid: list[str] = []
        for profile_id in discovered:
            try:
                self.validate_profile(self._paths.profile_root(profile_id, must_exist=True))
            except (OSError, ValueError):
                invalid.append(profile_id)
            else:
                valid.append(profile_id)
        return {
            "speech_root_ready": self._paths.root.is_dir(),
            "genie_data_present": any(self._paths.resolve("genie_data").iterdir()),
            "valid_profiles": tuple(valid),
            "invalid_profiles": tuple(invalid),
        }

    async def _persist_manifest(
        self, manifest: VoiceProfileManifest, profile_directory: Path
    ) -> VoiceProfile:
        model = self._inside_directory(profile_directory, manifest.model.path)
        references: list[dict[str, object]] = []
        for reference in manifest.references:
            audio = self._inside_directory(profile_directory, reference.audio)
            references.append(
                {
                    "reference_key": reference.id,
                    "style": reference.style,
                    "aliases": reference.aliases,
                    "audio_relative_path": self._paths.relative(audio),
                    "audio_checksum": self._file_checksum(audio),
                    "transcript": reference.text,
                    "language": reference.language,
                    "enabled": reference.enabled,
                    "priority": reference.priority,
                }
            )
        return await self._repository.save_profile(
            profile_id=manifest.id,
            display_name=manifest.display_name,
            provider=manifest.provider,
            engine_model_version=manifest.engine_model_version.value,
            language=manifest.language,
            supported_languages=manifest.supported_languages,
            model_relative_path=self._paths.relative(model),
            model_checksum=self._directory_checksum(model),
            default_style=manifest.default_style,
            enabled=manifest.enabled,
            source=manifest.source,
            source_note=manifest.source_note,
            license_note=manifest.license_note,
            manifest_hash=self._file_checksum(profile_directory / "profile.toml"),
            references=tuple(references),
        )

    async def _required(self, profile_id: str) -> VoiceProfile:
        profile = await self._repository.get_profile(profile_id)
        if profile is None:
            raise LookupError("voice profile not found")
        return profile

    async def _load(self, profile: VoiceProfile, *, reload: bool = False) -> None:
        if self._loader is None:
            return
        try:
            await self._loader.load_profile(profile, reload=reload)
        except (OSError, RuntimeError, ValueError):
            await publish_notification(
                self._event_publisher,
                EventName.SPEECH_PROFILE_FAILED,
                {"profile_id": profile.profile_id},
            )
            raise
        await publish_notification(
            self._event_publisher,
            EventName.SPEECH_PROFILE_LOADED,
            {"profile_id": profile.profile_id},
        )

    @staticmethod
    def _inside_directory(root: Path, relative_path: str) -> Path:
        path = (root / relative_path).resolve()
        if not path.is_relative_to(root.resolve()):
            raise SpeechPathError("manifest path escapes profile directory")
        return path

    @staticmethod
    def _file_checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _directory_checksum(cls, directory: Path) -> str:
        digest = hashlib.sha256()
        files = sorted(path for path in directory.rglob("*") if path.is_file())
        for path in files:
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(cls._file_checksum(path).encode())
        return digest.hexdigest()

    @staticmethod
    def _reference_source(source: Path) -> tuple[Path, Path | None]:
        if source.is_dir():
            return source / "reference.toml", None
        if source.is_file():
            return source.with_suffix(".toml"), source
        raise FileNotFoundError("reference source does not exist")


def _manifest_toml(manifest: VoiceProfileManifest) -> str:
    lines = [
        f"id = {_toml_quote(manifest.id)}",
        f"display_name = {_toml_quote(manifest.display_name)}",
        f"provider = {_toml_quote(manifest.provider)}",
        f"engine_model_version = {_toml_quote(manifest.engine_model_version.value)}",
        f"language = {_toml_quote(manifest.language)}",
        "supported_languages = ["
        + ", ".join(_toml_quote(item) for item in manifest.supported_languages)
        + "]",
        f"default_style = {_toml_quote(manifest.default_style)}",
        f"enabled = {str(manifest.enabled).lower()}",
        f"source = {_toml_quote(manifest.source)}",
        f"source_note = {_toml_quote(manifest.source_note)}",
        f"license_note = {_toml_quote(manifest.license_note)}",
        "",
        "[model]",
        f"path = {_toml_quote(manifest.model.path)}",
    ]
    for reference in manifest.references:
        aliases = ", ".join(_toml_quote(item) for item in reference.aliases)
        lines.extend(
            (
                "",
                "[[references]]",
                f"id = {_toml_quote(reference.id)}",
                f"style = {_toml_quote(reference.style)}",
                f"aliases = [{aliases}]",
                f"audio = {_toml_quote(reference.audio)}",
                f"text = {_toml_quote(reference.text)}",
                f"language = {_toml_quote(reference.language)}",
                f"enabled = {str(reference.enabled).lower()}",
                f"priority = {reference.priority}",
            )
        )
    return "\n".join(lines) + "\n"


def _toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
