from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Generator, List, Optional


class OpenAICompatHTTPClient:
    def __init__(self, *, base_url: str, api_key: str, timeout_sec: int = 60):
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key
        self._timeout_sec = int(timeout_sec or 60)

    def create_chat_completion(
        self,
        *,
        model: str,
        messages: List[Dict[str, Any]],
        stream: bool = False,
        extra_body: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        url = urllib.parse.urljoin(self._base_url + "/", "chat/completions")
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": bool(stream),
        }
        if max_tokens is not None:
            body["max_tokens"] = int(max_tokens)
        if extra_body:
            body.update(extra_body)

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            method="POST",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout_sec) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            raise RuntimeError(f"http_error:{e.code}:{detail}") from e
        except Exception as e:
            raise RuntimeError(f"request_failed:{e}") from e

    def stream_chat_completion(
        self,
        *,
        model: str,
        messages: List[Dict[str, Any]],
        extra_body: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        url = urllib.parse.urljoin(self._base_url + "/", "chat/completions")
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if max_tokens is not None:
            body["max_tokens"] = int(max_tokens)
        if extra_body:
            body.update(extra_body)

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            method="POST",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "text/event-stream",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout_sec) as resp:
                for raw_line in resp:
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        continue
                    yield obj
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            raise RuntimeError(f"http_error:{e.code}:{detail}") from e
        except socket.timeout as e:
            raise RuntimeError("request_timeout") from e
        except Exception as e:
            raise RuntimeError(f"request_failed:{e}") from e

    def list_models(self) -> Dict[str, Any]:
        url = urllib.parse.urljoin(self._base_url + "/", "models")
        req = urllib.request.Request(
            url=url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_sec) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            raise RuntimeError(f"http_error:{e.code}:{detail}") from e
        except Exception as e:
            raise RuntimeError(f"request_failed:{e}") from e
