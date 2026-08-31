from __future__ import annotations

import json
import http.client
import math
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from .ai_provider import ModelInvocation


OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterTransportError(RuntimeError):
    """A sanitized OpenRouter transport failure with no credential content."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "OPENROUTER_TRANSPORT_ERROR",
        status_code: int | None = None,
        elapsed_seconds: float | None = None,
        retry_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code
        self.elapsed_seconds = elapsed_seconds
        self.retry_count = retry_count


class OpenRouterTransport:
    """Minimal OpenRouter chat-completions transport with one transient retry."""

    MAX_BUSINESS_OUTPUT_TOKENS = 4096
    RESPONSE_CHUNK_BYTES = 64 * 1024

    def __init__(
        self,
        *,
        api_key_env: str = "OPENROUTER_API_KEY",
        endpoint: str = OPENROUTER_CHAT_COMPLETIONS_URL,
        timeout_seconds: float = 120.0,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        user_environment_reader: Callable[[str], str] | None = None,
    ) -> None:
        self.api_key_env = api_key_env
        self.endpoint = endpoint
        self.timeout_seconds = float(timeout_seconds)
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("invalid_openrouter_wall_clock_timeout")
        self._urlopen = urlopen
        self._user_environment_reader = user_environment_reader or _windows_user_environment_value

    def invoke(self, *, model: str, prompt: str, parameters: Mapping[str, Any]) -> ModelInvocation:
        body = dict(parameters)
        body.update(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
        )
        try:
            requested_max_tokens = int(
                body.get("max_tokens", self.MAX_BUSINESS_OUTPUT_TOKENS)
            )
        except (TypeError, ValueError):
            requested_max_tokens = self.MAX_BUSINESS_OUTPUT_TOKENS
        body["max_tokens"] = min(
            max(requested_max_tokens, 1), self.MAX_BUSINESS_OUTPUT_TOKENS
        )
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=encoded,
            method="POST",
            headers=self.authorization_headers(content_type_json=True),
        )

        started = time.perf_counter()
        deadline = started + self.timeout_seconds
        retry_count = 0
        while True:
            try:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise _ResponseDeadlineExceeded
                with self._urlopen(request, timeout=remaining) as response:
                    response_bytes = _read_response_with_deadline(
                        response,
                        deadline,
                        chunk_bytes=self.RESPONSE_CHUNK_BYTES,
                    )
            except urllib.error.HTTPError as exc:
                elapsed = time.perf_counter() - started
                raise OpenRouterTransportError(
                    f"OpenRouter HTTP error: {exc.code}",
                    error_code=f"OPENROUTER_HTTP_{exc.code}",
                    status_code=exc.code,
                    elapsed_seconds=elapsed,
                    retry_count=retry_count,
                ) from exc
            except Exception as exc:
                if isinstance(exc, _ResponseDeadlineExceeded) or time.perf_counter() >= deadline:
                    elapsed = time.perf_counter() - started
                    raise OpenRouterTransportError(
                        "OpenRouter request exceeded the shared wall-clock budget",
                        error_code="OPENROUTER_WALL_CLOCK_TIMEOUT",
                        elapsed_seconds=elapsed,
                        retry_count=retry_count,
                    ) from exc
                if not _is_transient_transport_error(exc):
                    raise
                if retry_count >= 1:
                    elapsed = time.perf_counter() - started
                    raise OpenRouterTransportError(
                        "OpenRouter transient transport failed after one retry",
                        error_code="OPENROUTER_TRANSIENT_RETRY_EXHAUSTED",
                        elapsed_seconds=elapsed,
                        retry_count=retry_count,
                    ) from exc
                retry_count += 1
                continue

            envelope_error: Exception | None = None
            try:
                payload = json.loads(response_bytes.decode("utf-8"))
                choice = payload["choices"][0]
                message = choice["message"]
                raw_text = _message_text(message.get("content"))
            except (KeyError, IndexError, TypeError, AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                envelope_error = exc
                failure_code = "OPENROUTER_INVALID_ENVELOPE_RETRY_EXHAUSTED"
                failure_message = "OpenRouter returned an invalid chat-completions envelope after one retry"
            else:
                if raw_text.strip():
                    break
                failure_code = "OPENROUTER_EMPTY_CONTENT_RETRY_EXHAUSTED"
                failure_message = "OpenRouter returned empty model content after one retry"

            if retry_count >= 1:
                elapsed = time.perf_counter() - started
                raise OpenRouterTransportError(
                    failure_message,
                    error_code=failure_code,
                    elapsed_seconds=elapsed,
                    retry_count=retry_count,
                ) from envelope_error
            retry_count += 1

        elapsed = time.perf_counter() - started

        usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        completion_details = (
            usage.get("completion_tokens_details")
            if isinstance(usage.get("completion_tokens_details"), Mapping)
            else {}
        )
        prompt_details = (
            usage.get("prompt_tokens_details")
            if isinstance(usage.get("prompt_tokens_details"), Mapping)
            else {}
        )
        return ModelInvocation(
            raw_text=raw_text,
            model=str(payload.get("model") or model),
            elapsed_seconds=elapsed,
            input_tokens=_optional_int(usage.get("prompt_tokens")),
            output_tokens=_optional_int(usage.get("completion_tokens")),
            cost=_optional_float(usage.get("cost")),
            cost_currency="USD" if usage.get("cost") is not None else "",
            request_id=str(payload.get("id") or ""),
            retry_count=retry_count,
            metadata={
                "api_provider": "OpenRouter",
                "upstream_provider": str(payload.get("provider") or ""),
                "finish_reason": str(choice.get("finish_reason") or ""),
                "total_tokens": _optional_int(usage.get("total_tokens")),
                "reasoning_tokens": _optional_int(completion_details.get("reasoning_tokens")),
                "cached_tokens": _optional_int(prompt_details.get("cached_tokens")),
                "endpoint": self.endpoint,
            },
        )

    def authorization_headers(self, *, content_type_json: bool = False) -> dict[str, str]:
        """Build the one shared bearer header set without logging or persisting the credential."""

        api_key = os.environ.get(self.api_key_env, "").strip()
        if not api_key:
            raise OpenRouterTransportError(f"Required credential is unavailable in {self.api_key_env}")
        user_api_key = self._user_environment_reader(self.api_key_env).strip()
        if user_api_key and user_api_key != api_key:
            raise OpenRouterTransportError(
                "Process credential differs from the current user environment; restart the host process",
                error_code="OPENROUTER_CREDENTIAL_SCOPE_MISMATCH",
            )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        if content_type_json:
            headers["Content-Type"] = "application/json"
        return headers


def _is_transient_transport_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            urllib.error.URLError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
            TimeoutError,
            socket.timeout,
            ConnectionResetError,
        ),
    )


class _ResponseDeadlineExceeded(TimeoutError):
    """Internal signal raised after the absolute response deadline expires."""


def _read_response_with_deadline(
    response: Any,
    deadline: float,
    *,
    chunk_bytes: int,
) -> bytes:
    """Read incrementally while applying the remaining absolute budget to the socket."""

    chunks: list[bytes] = []
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            _close_response(response)
            raise _ResponseDeadlineExceeded
        _set_response_socket_timeout(response, remaining)
        try:
            chunk = response.read(chunk_bytes)
        except TypeError:
            # Small test doubles and non-HTTP compatible readers may expose only read().
            chunk = response.read()
            if time.perf_counter() >= deadline:
                _close_response(response)
                raise _ResponseDeadlineExceeded
            return bytes(chunk or b"")
        if not chunk:
            return b"".join(chunks)
        chunks.append(bytes(chunk))


def _set_response_socket_timeout(response: Any, remaining: float) -> None:
    candidates = [response]
    for _index in range(3):
        nested = getattr(candidates[-1], "fp", None)
        if nested is None:
            break
        candidates.append(nested)
    for candidate in candidates:
        raw = getattr(candidate, "raw", None)
        sock = getattr(raw, "_sock", None) or getattr(candidate, "_sock", None)
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(max(remaining, 0.001))
            return


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(item.get("text") or "") for item in content if isinstance(item, Mapping)]
        return "".join(parts)
    return ""


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _windows_user_environment_value(name: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _value_type = winreg.QueryValueEx(key, name)
        return str(value)
    except (FileNotFoundError, OSError):
        return ""
