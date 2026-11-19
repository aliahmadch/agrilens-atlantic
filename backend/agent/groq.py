"""Groq (OpenAI-compatible chat completions API) for the field agent and the farmer chat.

First choice in AI_PROVIDER_ORDER when GROQ_API_KEY is set. Uses httpx, no extra SDK.
Tool schemas are the same JSON schemas the other providers use.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.groq.com/openai/v1"
# Tried in order when the configured model is unavailable, rate limited, slow, or emits a malformed tool call.
FALLBACK_MODELS = ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b")
RETRY_NEXT_MODEL = (400, 404, 413, 429, 500, 502, 503, 504)
_working_model: Optional[str] = None
status: dict[str, Any] = {"last_error": None, "model_errors": {}}  # shown in /api/agent/status


class GroqError(Exception):
    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        status["last_error"] = message


def configured() -> bool:
    return bool(settings.GROQ_API_KEY)


def current_model() -> str:
    return _working_model or settings.GROQ_MODEL


def tools_payload(tool_definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for tool in tool_definitions
    ]


async def generate(
    messages: list[dict[str, Any]],
    tool_definitions: Optional[list[dict[str, Any]]] = None,
    max_tokens: int = 2048,
    timeout: float = 60,
) -> dict[str, Any]:
    """One chat completion. Returns {message, finish_reason, usage, model}; raises GroqError."""
    global _working_model
    models = [current_model()] + [m for m in FALLBACK_MODELS if m != current_model()]
    last_error: Optional[GroqError] = None
    for model in models:
        body: dict[str, Any] = {"model": model, "messages": messages, "max_completion_tokens": max_tokens}
        if tool_definitions:
            body["tools"] = tools_payload(tool_definitions)
            body["tool_choice"] = "auto"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
                    json=body,
                )
        except httpx.TimeoutException:
            last_error = GroqError(f"Groq {model} timed out", 504)
            continue
        except httpx.HTTPError as exc:
            raise GroqError(f"Groq unreachable: {type(exc).__name__}") from exc
        data = response.json() if response.content else {}
        if response.status_code < 400:
            choice = (data.get("choices") or [{}])[0]
            _working_model = model
            status["last_error"] = None
            return {
                "message": choice.get("message") or {},
                "finish_reason": choice.get("finish_reason"),
                "usage": data.get("usage") or {},
                "model": data.get("model") or model,
            }
        error = data.get("error") or {}
        last_error = GroqError(f"Groq {response.status_code} ({model}): {(error.get('message') or response.text or '')[:300]}", response.status_code)
        status["model_errors"][model] = str(last_error)[:300]
        if response.status_code == 401:
            raise last_error
        if response.status_code in RETRY_NEXT_MODEL:
            logger.warning("Groq model %s failed, trying next: %s", model, last_error)
            continue
        raise last_error
    raise last_error or GroqError("Groq request failed")


def assistant_turn(message: dict[str, Any]) -> dict[str, Any]:
    """Echo the assistant turn with only the fields the API accepts back (drops reasoning text)."""
    turn: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        turn["tool_calls"] = [
            {"id": call["id"], "type": "function", "function": {"name": call["function"]["name"], "arguments": call["function"].get("arguments") or "{}"}}
            for call in message["tool_calls"]
        ]
    return turn


def tool_calls(message: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """[(call_id, name, args)] with arguments parsed from their JSON string (bad JSON -> {})."""
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            args = json.loads(function.get("arguments") or "{}")
        except ValueError:
            args = {}
        calls.append((call.get("id") or "", function.get("name") or "", args if isinstance(args, dict) else {}))
    return calls


def tool_result_turn(call_id: str, result: Any) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, default=str)}
