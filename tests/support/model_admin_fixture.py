"""Local-only upstream for Desktop model administration; records no credential values."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MODEL_UPSTREAM_KEY = "synthetic-desktop-model-key-never-export"


def attach_model_admin_fixture(app: FastAPI) -> None:
    calls: list[dict] = []

    def record(request: Request, *, model: str | None = None) -> bool:
        authorization = request.headers.get("authorization", "")
        authorized = authorization == f"Bearer {MODEL_UPSTREAM_KEY}"
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "model": model,
                "has_authorization": bool(authorization),
                "authorized": authorized,
            }
        )
        return authorized

    @app.get("/__test/models/v1/models", include_in_schema=False)
    async def models(request: Request):
        if not record(request):
            return JSONResponse({"error": {"code": "invalid_api_key"}}, status_code=401)
        return {
            "data": [
                {"id": "fixture-chat-a", "display_name": "Fixture A"},
                {"id": "fixture-chat-b", "display_name": "Fixture B"},
            ]
        }

    @app.post("/__test/models/v1/chat/completions", include_in_schema=False)
    async def complete(request: Request):
        body = await request.json()
        if not record(request, model=body.get("model")):
            return JSONResponse({"error": {"code": "invalid_api_key"}}, status_code=401)
        return {
            "id": "fixture-probe-response",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }

    @app.get("/__test/model-upstream-calls", include_in_schema=False)
    async def requests():
        return calls
