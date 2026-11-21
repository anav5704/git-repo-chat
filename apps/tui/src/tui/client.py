import json
import os
from collections.abc import Callable, Iterator

import httpx

from tui.env import load_env

EventHandler = Callable[[str, str], None]


def api_url() -> str:
    load_env()
    return os.environ.get("API_URL", "http://127.0.0.1:8000").rstrip("/")


def list_repos() -> list[dict[str, object]]:
    response = httpx.get(f"{api_url()}/repos", timeout=10)
    _raise(response)
    body = response.json()
    if not isinstance(body, list):
        raise TypeError("repo list was not a list")
    return body


def create_repo(source: str) -> dict[str, object]:
    response = httpx.post(
        f"{api_url()}/repos",
        json={"source": source},
        timeout=30,
    )
    _raise(response)
    body = response.json()
    if not isinstance(body, dict):
        raise TypeError("repo was not stored")
    return body


def stream_chat(
    repo_id: str,
    message: str,
    conversation_id: str | None,
    on_event: EventHandler,
) -> None:
    payload: dict[str, str] = {"message": message}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    with httpx.stream(
        "POST",
        f"{api_url()}/repos/{repo_id}/chat",
        json=payload,
        timeout=None,
    ) as response:
        _raise(response)
        for name, data in iter_sse(response.iter_lines()):
            on_event(name, data)


def iter_sse(lines: Iterator[str]) -> Iterator[tuple[str, str]]:
    event = "message"
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event = "message"
            data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].lstrip()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        yield event, "\n".join(data)


def _raise(response: httpx.Response) -> None:
    if response.is_success:
        return
    detail = response.text
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict) and body.get("detail"):
        detail = str(body["detail"])
    raise RuntimeError(detail)
