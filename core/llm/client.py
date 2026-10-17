"""OpenAI-compatible chat client against the local llama-server (PLAN.md §8.4).

The core service speaks ``/v1/chat/completions`` and nothing model-specific, so §4.1's
"swapping models is a config change" holds literally: LM Studio and Ollama work as
user-supplied endpoints by changing ``base_url`` alone (§4.1 accepts them as equals for
that reason, while shipping ``llama-server``).

Every call is authenticated with the per-launch token (§9).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from core.config import LLAMA_HOST, LLAMA_PORT, get_settings
from core.llm.server import LlamaServerError, LlamaServerSupervisor, get_supervisor


class LLMError(RuntimeError):
    """Raised when the local LLM cannot be reached or returns an error."""


@dataclass
class ChatResult:
    """A completed chat response plus the timing data §14.2 measures."""

    text: str
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Wall time to first streamed token, seconds (§14.2 "time-to-first-token").
    ttft_s: float | None = None
    total_s: float = 0.0

    @property
    def tokens_per_second(self) -> float | None:
        """Generation throughput, excluding the prefill."""
        if not self.completion_tokens or not self.ttft_s or self.total_s <= self.ttft_s:
            return None
        return self.completion_tokens / (self.total_s - self.ttft_s)


@dataclass
class StreamChunk:
    """One incremental piece of a streamed answer."""

    text: str
    ttft_s: float | None = None


class LLMClient:
    """Chat completions against ``127.0.0.1:8081``."""

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        supervisor: LlamaServerSupervisor | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or f"http://{LLAMA_HOST}:{LLAMA_PORT}").rstrip("/")
        self.api_token = api_token or settings.api_token
        self.supervisor = supervisor
        self.timeout_s = timeout_s

    # -- plumbing ----------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # §9: the per-launch token guards the endpoint from other local processes.
        return {"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}

    def _ensure_ready(self) -> None:
        """Make sure the server is up, starting it lazily (§8.4)."""
        if self.supervisor is not None:
            try:
                self.supervisor.ensure_running()
                return
            except LlamaServerError as exc:
                raise LLMError(str(exc)) from exc
        if not httpx.get(f"{self.base_url}/health", timeout=2.0).is_success:
            raise LLMError(
                f"no LLM endpoint at {self.base_url}. Run the supervisor "
                "(core.llm.server) or point base_url at LM Studio / Ollama (§4.1)."
            )

    def _payload(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        stream: bool,
        stop: list[str] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": "local",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if stop:
            payload["stop"] = stop
        # Qwen3 enables reasoning by default, which spends the whole max_tokens budget on
        # `reasoning_content` and truncates the answer before any citation appears. The
        # §8.1 task is extractive summarisation, so reasoning is off by default; see
        # core.config.Settings.llm_enable_thinking.
        if not get_settings().llm_enable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            resp = httpx.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"LLM returned {resp.status_code}: {resp.text[:500]}")
        return resp

    # -- non-streaming -----------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 512,
        stop: list[str] | None = None,
    ) -> ChatResult:
        """One-shot completion. Convenient for scripts and tests."""
        payload = self._payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            stop=stop,
        )

        start = time.perf_counter()
        data = self._post(payload).json()
        elapsed = time.perf_counter() - start

        if self.supervisor is not None:
            self.supervisor.touch()

        usage = data.get("usage") or {}
        return ChatResult(
            text=_first_choice_text(data),
            model=data.get("model"),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            # Without a stream there is no TTFT; report None rather than faking it.
            ttft_s=None,
            total_s=elapsed,
        )

    # -- streaming ---------------------------------------------------------------

    def stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 512,
        stop: list[str] | None = None,
    ) -> Iterator[StreamChunk]:
        """Yield answer text as it is produced.

        Synchronous generator because the consumer is the §8.2 prompt builder and the
        Phase 4 SSE endpoint, both of which run in a worker thread. §14.2 records TTFT
        from the first yielded chunk.
        """
        payload = self._payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            stop=stop,
        )

        start = time.perf_counter()
        ttft: float | None = None
        text_parts: list[str] = []

        try:
            with httpx.stream(
                "POST",
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout_s,
            ) as resp:
                if resp.status_code != 200:
                    resp.read()
                    raise LLMError(f"LLM returned {resp.status_code}: {resp.text[:500]}")
                for delta in _iter_sse_deltas(resp):
                    if ttft is None:
                        ttft = time.perf_counter() - start
                    if delta:
                        text_parts.append(delta)
                        yield StreamChunk(text=delta, ttft_s=ttft)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM stream failed: {exc}") from exc
        finally:
            if self.supervisor is not None:
                self.supervisor.touch()

    # -- diagnostics -------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Endpoint health, for ``GET /status``."""
        out: dict[str, Any] = {"base_url": self.base_url, "reachable": False}
        try:
            resp = httpx.get(f"{self.base_url}/health", timeout=2.0)
            out["reachable"] = resp.status_code == 200
        except httpx.HTTPError as exc:
            out["error"] = str(exc)
        return out


def _first_choice_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content") or ""


def _iter_sse_deltas(resp: httpx.Response) -> Iterator[str]:
    """Yield content deltas from an OpenAI-style SSE response."""
    for line in resp.iter_lines():
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        choices = data.get("choices") or []
        if not choices:
            continue
        # Non-streaming chunks carry a final `usage` block; streaming carries deltas.
        content = (choices[0].get("delta") or {}).get("content")
        if content:
            yield content


_client: LLMClient | None = None


def get_client(api_token: str | None = None) -> LLMClient:
    """Shared chat client, wired to the shared supervisor."""
    global _client
    settings = get_settings()
    token = api_token or settings.api_token
    if _client is None or _client.api_token != token:
        _client = LLMClient(api_token=token, supervisor=get_supervisor(token))
    return _client
