# path: agent/llm.py
import json
import logging
import os
import random
import re
import sys
import threading
import time
from collections import deque

import openai
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

_CALL_DEADLINE_S = 90.0
_BACKOFF_BASE_S = 2.0
_BACKOFF_CAP_S = 30.0
_JSON_TEMPERATURE = 0.2
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(Exception):
    pass


class _RateLimiter:
    def __init__(self, max_rpm: int) -> None:
        self._max = max_rpm
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, deadline: float) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= 60.0:
                    self._stamps.popleft()
                if len(self._stamps) < self._max:
                    self._stamps.append(now)
                    return
                wait = 60.0 - (now - self._stamps[0])
            if time.monotonic() + wait >= deadline:
                raise LLMError(
                    f"LLM_MAX_RPM limiter wait ({wait:.0f}s) would exceed the "
                    f"{_CALL_DEADLINE_S:.0f}s call deadline"
                )
            time.sleep(wait)


_lock = threading.Lock()
_runtime: tuple[openai.OpenAI, _RateLimiter, str] | None = None
_json_mode_supported = True


def _get_runtime() -> tuple[openai.OpenAI, _RateLimiter, str]:
    global _runtime
    with _lock:
        if _runtime is None:
            base_url = os.environ.get("LLM_BASE_URL", "").strip()
            model = os.environ.get("LLM_MODEL", "").strip()
            if not base_url or not model:
                raise LLMError("LLM_BASE_URL and LLM_MODEL must be set")
            try:
                max_rpm = int(os.environ.get("LLM_MAX_RPM") or "20")
            except ValueError as exc:
                raise LLMError("LLM_MAX_RPM must be an integer") from exc
            if max_rpm < 1:
                raise LLMError("LLM_MAX_RPM must be >= 1")
            # Local servers such as Ollama ignore the key but the SDK requires a non-empty one.
            api_key = os.environ.get("LLM_API_KEY", "").strip() or "unused"
            client = openai.OpenAI(base_url=base_url, api_key=api_key, max_retries=0)
            _runtime = (client, _RateLimiter(max_rpm), model)
        return _runtime


def _retry_after(exc: openai.APIStatusError) -> float | None:
    value = exc.response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _chat(system: str, user: str, temperature: float, *, json_mode: bool = False) -> str:
    global _json_mode_supported
    client, limiter, model = _get_runtime()
    deadline = time.monotonic() + _CALL_DEADLINE_S
    use_json_mode = json_mode and _json_mode_supported
    attempt = 0
    while True:
        limiter.acquire(deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LLMError(f"LLM call exceeded the {_CALL_DEADLINE_S:.0f}s deadline")
        kwargs = {"response_format": {"type": "json_object"}} if use_json_mode else {}
        started = time.monotonic()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                timeout=remaining,
                **kwargs,
            )
        except openai.APIStatusError as exc:
            status = exc.status_code
            if use_json_mode and status in (400, 404, 422):
                logger.debug("provider rejected response_format (HTTP %s); falling back", status)
                _json_mode_supported = False
                use_json_mode = False
                continue
            if status != 429 and status < 500:
                raise LLMError(f"LLM request failed with HTTP {status}: {str(exc)[:300]}") from exc
            reason = f"HTTP {status}"
            retry_after = _retry_after(exc)
            delay = None if retry_after is None else retry_after + random.uniform(0.0, 1.0)
        except openai.APIConnectionError as exc:
            reason = type(exc).__name__
            delay = None
        except openai.OpenAIError as exc:
            raise LLMError(f"LLM request failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
        else:
            usage = getattr(resp, "usage", None)
            logger.debug(
                "LLM call ok: model=%s latency=%.2fs prompt_tokens=%s completion_tokens=%s",
                model,
                time.monotonic() - started,
                getattr(usage, "prompt_tokens", None),
                getattr(usage, "completion_tokens", None),
            )
            if not resp.choices:
                return ""
            return resp.choices[0].message.content or ""

        if delay is None:
            ceiling = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2**attempt)
            delay = ceiling / 2 + random.uniform(0.0, ceiling / 2)
        attempt += 1
        if time.monotonic() + delay >= deadline:
            raise LLMError(
                f"LLM request failed ({reason}); retrying in {delay:.0f}s would exceed "
                f"the {_CALL_DEADLINE_S:.0f}s call deadline"
            )
        logger.info("LLM request failed (%s); retrying in %.1fs", reason, delay)
        time.sleep(delay)


def _balanced_end(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _extract_json_object(text: str) -> str | None:
    fallback = None
    pos = text.find("{")
    while pos != -1:
        end = _balanced_end(text, pos)
        if end is None:
            break
        candidate = text[pos:end]
        if fallback is None:
            fallback = candidate
        try:
            if isinstance(json.loads(candidate), dict):
                return candidate
        except ValueError:
            pass
        pos = text.find("{", end)
    return fallback


def _describe(exc: ValueError) -> str:
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        ]
        return "; ".join(parts)[:1500]
    return str(exc)[:1500]


def complete(system: str, user: str, *, temperature: float = 0.2) -> str:
    text = _chat(system, user, temperature)
    if not text.strip():
        raise LLMError("model returned an empty completion")
    return text


def complete_json(
    system: str,
    user: str,
    model_cls: type[BaseModel],
    *,
    max_retries: int = 3,
) -> BaseModel:
    json_system = (
        f"{system}\n\n"
        "Respond with exactly one JSON object that conforms to the JSON schema below. "
        "Output the JSON object only: no prose, no markdown, no code fences.\n"
        f"JSON schema:\n{json.dumps(model_cls.model_json_schema())}"
    )
    attempts = max(0, max_retries) + 1
    user_turn = user
    last_error = ""
    for n in range(attempts):
        raw = _chat(json_system, user_turn, _JSON_TEMPERATURE, json_mode=True)
        try:
            # Reasoning models may emit <think> blocks whose contents include stray braces.
            candidate = _extract_json_object(_THINK_BLOCK.sub("", raw))
            if candidate is None:
                raise ValueError("no complete JSON object found in the reply")
            return model_cls.model_validate_json(candidate)
        except ValueError as exc:
            last_error = _describe(exc)
            logger.debug("complete_json attempt %d/%d rejected: %s", n + 1, attempts, last_error)
            user_turn = (
                f"{user}\n\nYour previous reply was rejected: {last_error}\n"
                "Reply again with only the corrected JSON object."
            )
    raise LLMError(
        f"model did not return valid {model_cls.__name__} JSON after {attempts} attempts: {last_error}"
    )


if __name__ == "__main__":

    class _Ping(BaseModel):
        ok: bool
        message: str

    logging.basicConfig(level=logging.INFO)
    logger.setLevel(logging.DEBUG)
    try:
        print("complete:", complete("You are a terse assistant.", "Reply with the single word: pong").strip())
        result = complete_json(
            "You are a terse assistant.",
            'Return ok=true and message="pong".',
            _Ping,
        )
        print("complete_json:", result.model_dump_json())
    except LLMError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    print("OK")
