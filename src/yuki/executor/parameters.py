"""Current capabilities validate their own parameters; new capability names stay open."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from yuki.client_api.errors import ClientError


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFolder(Parameters):
    path: str = Field(min_length=1)
    mode: Literal["list", "read"]


class WriteFolder(Parameters):
    path: str = Field(min_length=1)
    content_base64: str | None = None
    artifact_id: str | None = None
    version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    if_absent: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def one_content_source(self) -> "WriteFolder":
        inline = self.content_base64 is not None
        artifact = self.artifact_id is not None and self.version is not None
        if inline == artifact or ((self.artifact_id is None) != (self.version is None)):
            raise ValueError("one_content_source_required")
        return self


class OpenURL(Parameters):
    url: str = Field(min_length=1)


class OpenFile(Parameters):
    path: str = Field(min_length=1)


class Notify(Parameters):
    title: str
    body: str


def validate_parameters(capability: str, params: dict[str, Any]) -> None:
    models: dict[str, type[Parameters]] = {
        "folder.read": ReadFolder,
        "folder.write": WriteFolder,
        "open.url": OpenURL,
        "open.file": OpenFile,
        "notify": Notify,
    }
    model = models.get(capability)
    if model is not None:
        try:
            model.model_validate(params)
        except ValidationError:
            raise ClientError("validation_error") from None


def validate_result(
    capability: str | None, params: dict[str, Any], state: str, result: dict[str, Any]
) -> None:
    if state != "succeeded":
        return
    import base64
    import binascii
    import hashlib

    if capability in {"open.url", "open.file", "notify"}:
        if result.get("submitted") is not True:
            raise ClientError("validation_error")
    elif capability == "folder.write" or (
        capability == "folder.read" and params.get("mode") == "read"
    ):
        size, digest = result.get("size"), result.get("sha256")
        if type(size) is not int or size < 0 or not isinstance(digest, str) or len(digest) != 64:
            raise ClientError("validation_error")
        try:
            bytes.fromhex(digest)
        except ValueError:
            raise ClientError("validation_error") from None
        if capability == "folder.write":
            if type(result.get("created")) is not bool:
                raise ClientError("validation_error")
            if "artifact_id" in params:
                if digest != params.get("version"):
                    raise ClientError("validation_error")
                return
            content = params.get("content_base64")
        else:
            content = result.get("content_base64")
        if not isinstance(content, str):
            raise ClientError("validation_error")
        try:
            data = base64.b64decode(content, validate=True)
        except (TypeError, ValueError, binascii.Error):
            raise ClientError("validation_error") from None
        if hashlib.sha256(data).hexdigest() != digest or len(data) != size:
            raise ClientError("validation_error")
    elif capability == "folder.read" and not isinstance(result.get("entries"), list):
        raise ClientError("validation_error")
