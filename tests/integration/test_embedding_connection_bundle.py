"""Bundle configuration reaches an actual local embedding HTTP endpoint."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from tests.support.model_profiles import write_fake_profiles

from yuki.admin.config_files import ConfigFileService
from yuki.config import Settings
from yuki.memory.embedding.openai_compatible import OpenAICompatibleEmbeddingProvider
from yuki.memory.embedding.provider import EmbeddingProviderError
from yuki.memory.embedding.runtime import MemoryEmbeddingRuntime
from yuki.model_runtime.profiles import load_model_profile_catalog
from yuki.model_runtime.secrets import read_model_secrets


@pytest.fixture
def embedding_http():
    state = {"status": 200, "response": None, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(
                {
                    "path": self.path,
                    "body": body,
                    "authorized": bool(self.headers.get("Authorization")),
                }
            )
            response = state["response"] or {
                "data": [
                    {"index": i, "embedding": [float(i + 1), 0.5]}
                    for i in reversed(range(len(body["input"])))
                ],
                "usage": {"prompt_tokens": 7},
            }
            encoded = json.dumps(response).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def evidence(name: str, value: object) -> None:
    path = Path("test-results/model-connections") / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


async def test_bundle_sidecar_and_compatible_embedding_real_http(tmp_path, embedding_http):
    url, state = embedding_http
    path = write_fake_profiles(tmp_path / "model_profiles.toml")
    settings = Settings(
        _env_file=None,
        model_profiles_file=path,
        web_mode="disabled",
        memory_embedding_enabled=False,
    )
    files = ConfigFileService(settings)
    before = await files.read()
    document = before["document"]
    document["embedding"] = {
        "provider": "openai_compatible",
        "base_url": url,
        "api_key_env": "EMBED_TEST_KEY",
        "model": "fixture-embedding",
        "dimensions": 2,
    }
    await files.save(
        before["revision"],
        {"document": document, "api_keys": {"EMBED_TEST_KEY": "fixture-private-key"}},
    )
    after = await files.read()
    assert after["valid"]
    assert document["embedding"].items() <= after["document"]["embedding"].items()
    assert after["document"]["embedding"]["output_type"] == "dense"
    assert after["document"]["embedding"]["document_template_version"] == 1
    assert after["apply_mode"] == "restart"
    assert "fixture-private-key" not in json.dumps(after)
    assert "fixture-private-key" not in path.read_text()
    catalog = load_model_profile_catalog(path, web_mode=settings.web.mode)
    connection = catalog.embedding
    assert connection is not None
    keys = read_model_secrets(path)[1]
    provider = OpenAICompatibleEmbeddingProvider(
        base_url=connection.base_url,
        api_key=keys[connection.api_key_env],
        model=connection.model,
        dimensions=connection.dimensions,
    )
    try:
        assert not settings.memory_embedding_enabled
        selected = MemoryEmbeddingRuntime._build_provider(
            settings, connection, keys[connection.api_key_env]
        )
        assert isinstance(selected, OpenAICompatibleEmbeddingProvider)
        assert selected.profile.dimensions == 2
        assert selected.profile.provider_id == "openai_compatible"
        await selected.close()
        assert (
            MemoryEmbeddingRuntime._build_provider(
                settings,
                connection.model_copy(update={"enabled": False}),
                keys[connection.api_key_env],
            )
            is None
        )
        result = await provider.embed_documents(("first", "second"))
        assert tuple(vector.values[0] for vector in result.vectors) == (1, 2)
        assert result.usage.input_tokens == 7
        assert state["requests"][0]["path"] == "/v1/embeddings"
        assert len(state["requests"]) == 1
        assert state["requests"][0]["authorized"]
        evidence(
            "compatible-success",
            {
                "public_bundle": after,
                "requests": state["requests"],
                "vectors": [v.model_dump() for v in result.vectors],
            },
        )
    finally:
        await provider.close()


async def test_malformed_connection_cannot_echo_a_plaintext_key(tmp_path):
    path = write_fake_profiles(tmp_path / "model_profiles.toml")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            '\n[embedding]\nprovider="openai_compatible"\nbase_url="https://fixture.invalid/v1"\nmodel="fixture"\ndimensions=2\napi_key="fixture-private-key"\n'
        )
    files = ConfigFileService(
        Settings(_env_file=None, model_profiles_file=path, web_mode="disabled")
    )
    result = await files.read()
    assert not result["valid"]
    assert "fixture-private-key" not in json.dumps(result)
    evidence("malformed-connection", result)


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "embedding_authentication_failed"),
        (429, "embedding_rate_limited"),
        (500, "embedding_provider_unavailable"),
    ],
)
async def test_compatible_failure_has_one_request_no_key_echo(embedding_http, status, expected):
    url, state = embedding_http
    state.update(status=status, response={"error": {"message": "fixture-private-key"}})
    provider = OpenAICompatibleEmbeddingProvider(
        base_url=url, api_key="fixture-private-key", model="fixture", dimensions=2
    )
    try:
        with pytest.raises(EmbeddingProviderError) as caught:
            await provider.embed_query("query")
        assert caught.value.code == expected
        assert "fixture-private-key" not in str(caught.value)
        assert len(state["requests"]) == 1
        evidence(
            f"compatible-{status}", {"error": caught.value.code, "requests": state["requests"]}
        )
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "rows",
    [
        [{"index": 0, "embedding": [1, 2]}, {"index": 0, "embedding": [2, 3]}],
        [{"index": 0, "embedding": [1]}],
    ],
)
async def test_compatible_invalid_vectors_not_retried(embedding_http, rows):
    url, state = embedding_http
    state["response"] = {"data": rows}
    provider = OpenAICompatibleEmbeddingProvider(
        base_url=url, api_key="", model="fixture", dimensions=2
    )
    try:
        with pytest.raises(EmbeddingProviderError) as caught:
            await provider.embed_documents(("first", "second"))
        assert caught.value.code == "embedding_invalid_response"
        assert len(state["requests"]) == 1
        assert not state["requests"][0]["authorized"]
        evidence(
            f"compatible-invalid-{len(rows)}",
            {"error": caught.value.code, "requests": state["requests"]},
        )
    finally:
        await provider.close()
