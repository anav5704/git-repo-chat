import json
import os
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel

_BASE = "https://generativelanguage.googleapis.com/v1beta"
_THINKING = "LOW"
_ROLES = {"assistant": "model", "model": "model", "user": "user"}


@dataclass(frozen=True, slots=True)
class Turn:
    role: str
    content: str


def complete(turns: Sequence[Turn], *, system: str | None = None) -> str:
    text = _text(_post(turns, system, None))
    if not text:
        raise RuntimeError("chat response has no text")
    return text


def stream(turns: Sequence[Turn], *, system: str | None = None) -> Iterator[str]:
    key, model = _settings()
    body = _body(turns, system, None)
    for attempt in range(4):
        yielded = False
        with (
            httpx.Client(timeout=60) as client,
            client.stream(
                "POST",
                f"{_BASE}/models/{model}:streamGenerateContent",
                headers={"x-goog-api-key": key},
                params={"alt": "sse"},
                json=body,
            ) as response,
        ):
            if response.status_code == 429 and attempt < 3:
                response.read()
                time.sleep(20)
                continue
            if response.status_code != 200:
                raw = response.read().decode("utf-8", "replace")
                raise RuntimeError(
                    f"chat request failed: {response.status_code} {raw[:500]}"
                )
            for line in response.iter_lines():
                payload = _event(line)
                if payload is None:
                    continue
                text = _text(payload)
                if text:
                    yielded = True
                    yield text
        if yielded:
            return
        raise RuntimeError("chat response has no text")


def structured[T: BaseModel](
    turns: Sequence[Turn], shape: type[T], *, system: str | None = None
) -> T:
    text = _text(_post(turns, system, shape.model_json_schema()))
    if not text:
        raise RuntimeError("chat response has no text")
    return shape.model_validate_json(text)


def _post(
    turns: Sequence[Turn], system: str | None, schema: dict[str, Any] | None
) -> Any:
    key, model = _settings()
    body = _body(turns, system, schema)
    response = httpx.post(
        f"{_BASE}/models/{model}:generateContent",
        headers={"x-goog-api-key": key},
        json=body,
        timeout=60,
    )
    for attempt in range(3):
        if response.status_code != 429:
            break
        time.sleep(20)
        response = httpx.post(
            f"{_BASE}/models/{model}:generateContent",
            headers={"x-goog-api-key": key},
            json=body,
            timeout=60,
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"chat request failed: {response.status_code} {response.text[:500]}"
        )
    return response.json()


def _body(
    turns: Sequence[Turn], system: str | None, schema: dict[str, Any] | None
) -> dict[str, Any]:
    if not turns:
        raise ValueError("turns are empty")
    contents = []
    for turn in turns:
        role = _ROLES.get(turn.role)
        if role is None:
            raise ValueError(f"unknown role {turn.role}")
        if not turn.content.strip():
            raise ValueError("text is empty")
        contents.append({"role": role, "parts": [{"text": turn.content}]})
    config: dict[str, Any] = {"thinkingConfig": {"thinkingLevel": _THINKING}}
    if schema is not None:
        config["responseMimeType"] = "application/json"
        config["responseJsonSchema"] = schema
    body: dict[str, Any] = {"contents": contents, "generationConfig": config}
    if system is not None and system.strip():
        body["systemInstruction"] = {"parts": [{"text": system}]}
    return body


def _settings() -> tuple[str, str]:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("CHAT_MODEL", "gemini-3-flash-preview").strip()
    for prefix in ("models/", "google/"):
        if model.startswith(prefix):
            model = model.removeprefix(prefix)
    if not key or not model:
        raise RuntimeError("GEMINI_API_KEY and CHAT_MODEL are required")
    return key, model


def _event(line: str) -> Any | None:
    if not line.startswith("data:"):
        return None
    data = line.removeprefix("data:").strip()
    if not data or data == "[DONE]":
        return None
    return json.loads(data)


def _text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    message = error.get("message", error) if isinstance(error, dict) else None
    if message:
        raise RuntimeError(f"chat request failed: {message}")
    feedback = payload.get("promptFeedback")
    reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
    if reason:
        raise RuntimeError(f"chat blocked: {reason}")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("thought") is True:
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "".join(chunks)
