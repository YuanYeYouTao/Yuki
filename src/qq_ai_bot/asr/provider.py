"""Bounded Qwen ASR calls; audio and provider errors never enter logs."""

from __future__ import annotations

import base64
from typing import Protocol

import httpx


class ASRError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ASRProvider(Protocol):
    async def transcribe(self, audio: bytes) -> str: ...

    async def close(self) -> None: ...


class QwenASRProvider:
    """Qwen3-ASR uses input_audio in chat/completions, not audio/transcriptions."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "qwen3-asr-flash",
        timeout_seconds: float = 60,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)

    async def transcribe(self, audio: bytes) -> str:
        if not audio or len(audio) > 7_000_000:
            raise ASRError("audio_limit")
        body = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": "data:audio/mpeg;base64,"
                                + base64.b64encode(audio).decode("ascii")
                            },
                        }
                    ],
                }
            ],
            "stream": False,
            "asr_options": {"enable_itn": False},
        }
        # No automatic retry: a lost response may already have incurred a bill.
        try:
            async with self._client.stream(
                "POST",
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            ) as response:
                if response.status_code in {401, 403}:
                    raise ASRError("not_configured")
                if response.status_code == 429:
                    raise ASRError("rate_limited")
                if response.status_code != 200:
                    raise ASRError("provider_failed")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16384):
                    data.extend(chunk)
                    if len(data) > 131072:
                        raise ASRError("invalid_response")
            import json

            payload = json.loads(data)
            choice = payload["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ASRError("incomplete_transcript")
            result = choice["message"]["content"]
            if not isinstance(result, str) or len(result) > 12000:
                raise ASRError("invalid_response")
            result = "".join(c for c in result if c.isprintable() or c in "\n\t").strip()
            if not result:
                raise ASRError("no_speech")
            return result
        except httpx.TimeoutException as exc:
            raise ASRError("timeout") from exc
        except httpx.HTTPError as exc:
            raise ASRError("provider_failed") from exc
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ASRError("invalid_response") from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
