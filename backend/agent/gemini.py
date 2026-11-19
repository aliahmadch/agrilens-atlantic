"""Google Gemini (generateContent REST API) for the field agent and the farmer chat.

Used when GEMINI_API_KEY is set and no ANTHROPIC_API_KEY is. Talks to the REST API with
httpx so no extra SDK is needed. Tool schemas are the same JSON schemas the Claude path uses.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Tried in order when the configured model is retired, overloaded, rate limited or too slow.
FALLBACK_MODELS = ("gemini-3-flash-preview", "gemini-flash-latest", "gemini-3.1-flash-lite")
RETRY_NEXT_MODEL = (404, 429, 500, 503, 504)
_working_model: Optional[str] = None
status: dict[str, Optional[str]] = {"last_error": None}  # shown in /api/agent/status to diagnose setup problems
_use_json_schema = True  # parametersJsonSchema (full JSON Schema); falls back to the OpenAPI-subset "parameters"


class GeminiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status
        status_store = globals()["status"]
        status_store["last_error"] = message


def configured() -> bool:
    return bool(settings.GEMINI_API_KEY)


def current_model() -> str:
    return _working_model or settings.GEMINI_MODEL


def _sanitize(schema: Any) -> Any:
    """JSON Schema -> Gemini OpenAPI subset: drop additionalProperties, turn ["x","null"] into nullable."""
    if isinstance(schema, list):
        return [_sanitize(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("additionalProperties", "$schema"):
            continue
        if key == "type" and isinstance(value, list):
            non_null = [item for item in value if item != "null"]
            out["type"] = non_null[0] if non_null else "string"
            if "null" in value:
                out["nullable"] = True
            continue
        out[key] = _sanitize(value)
    return out


def function_declarations(tool_definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declarations = []
    for tool in tool_definitions:
        declaration: dict[str, Any] = {"name": tool["name"], "description": tool.get("description", "")}
        schema = tool.get("input_schema") or {"type": "object", "properties": {}}
        if schema.get("properties"):
            if _use_json_schema:
                declaration["parametersJsonSchema"] = schema
            else:
                declaration["parameters"] = _sanitize(schema)
        declarations.append(declaration)
    return declarations


async def generate(
    contents: list[dict[str, Any]],
    system: str,
    tool_definitions: Optional[list[dict[str, Any]]] = None,
    max_output_tokens: int = 2048,
    timeout: float = 90,
) -> dict[str, Any]:
    """One generateContent call. Returns the first candidate plus usage; raises GeminiError."""
    global _working_model, _use_json_schema
    models = [current_model()] + [m for m in FALLBACK_MODELS if m != current_model()]
    last_error: Optional[GeminiError] = None
    for model in models:
        for _attempt in range(2):  # second attempt only after switching the schema format
            body: dict[str, Any] = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": contents,
                "generationConfig": {"maxOutputTokens": max_output_tokens},
            }
            if tool_definitions:
                body["tools"] = [{"functionDeclarations": function_declarations(tool_definitions)}]
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        f"{BASE_URL}/models/{model}:generateContent",
                        headers={"x-goog-api-key": settings.GEMINI_API_KEY or ""},
                        json=body,
                    )
            except httpx.TimeoutException:
                last_error = GeminiError(f"Gemini {model} timed out", 504)
                break
            except httpx.HTTPError as exc:
                raise GeminiError(f"Gemini unreachable: {type(exc).__name__}") from exc
            data = response.json() if response.content else {}
            if response.status_code < 400:
                _working_model = model
                status["last_error"] = None
                candidates = data.get("candidates") or []
                if not candidates:
                    reason = (data.get("promptFeedback") or {}).get("blockReason") or "no candidates"
                    raise GeminiError(f"Gemini returned no answer ({reason})", 200)
                return {"candidate": candidates[0], "usage": data.get("usageMetadata") or {}, "model": data.get("modelVersion") or model}
            message = ((data.get("error") or {}).get("message") or response.text or "")[:300]
            last_error = GeminiError(f"Gemini {response.status_code}: {message}", response.status_code)
            schema_rejected = any(token in message for token in ("parameters_json_schema", "parametersJsonSchema", "Unknown name"))
            if response.status_code == 400 and _use_json_schema and tool_definitions and schema_rejected:
                _use_json_schema = False
                continue
            break
        if last_error and last_error.status in RETRY_NEXT_MODEL:
            logger.warning("Gemini model %s unavailable, trying next: %s", model, last_error)
            continue
        raise last_error  # auth, quota, bad request: no point trying other models
    raise last_error or GeminiError("Gemini request failed")


def parts_of(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    return ((candidate.get("content") or {}).get("parts")) or []


def text_of(candidate: dict[str, Any]) -> str:
    return "".join(part.get("text", "") for part in parts_of(candidate) if not part.get("thought")).strip()


def function_calls(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    return [part["functionCall"] for part in parts_of(candidate) if part.get("functionCall")]


def model_turn(candidate: dict[str, Any]) -> dict[str, Any]:
    """Echo the model's turn back unchanged (keeps thought signatures intact for the next request)."""
    return {"role": "model", "parts": parts_of(candidate)}


def function_response_turn(responses: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    parts = []
    for call, result in responses:
        payload: dict[str, Any] = {"name": call.get("name"), "response": result}
        if call.get("id"):
            payload["id"] = call["id"]
        parts.append({"functionResponse": payload})
    return {"role": "user", "parts": parts}
