"""OpenAI-compatible chat-completions client over httpx. The provider is config only: LLM_API_KEY, LLM_BASE_URL,
DRYDOCK_MODEL, optional LLM_FALLBACKS (see .env.example). A key is never sent to a default host: a key with no base
URL refuses to run. The older VULTR_INFERENCE_* names still work.

No cache, no replay. Outcomes besides a full Reply:
- ModelUnavailable: no key, timeout, transport error, 5xx, 402/408/429 after retries, run deadline or token budget. The
  eval aborts on it; the app shows the card.
- ModelRequestError: any other 4xx (bad schema, wrong model id, bad key): our bug, so the eval scores the ticket as a
  fail and keeps going. The app shows the same card.
- ModelTruncated: finish_reason == "length", a cut-off reply: a budget problem, never scored as a model answer.
str(e) is always a fixed public text (safe for the UI); the HTTP status/body live in e.detail (logs, ledger only).
Keys live only in this process's env; sandbox containers never get them.
"""
import logging
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

TEMPERATURE = 0.2                # plan Global Constraints
# First live run (2026-11-07, glm-5.3-flash on Vultr) hit finish_reason=length at 2048: the model reasons
# before it answers. 8192 is a measured-failure fix, not a tuned value; override with DRYDOCK_MAX_TOKENS.
# (ModelTruncated still makes a too-small cap visible instead of a silent bad contract)
MAX_TOKENS = int(__import__("os").environ.get("DRYDOCK_MAX_TOKENS", "8192"))
# Live E8 (2026-11-07): glm-5.3-flash spent all 8192 tokens on hidden reasoning in the revise contract call, content
# empty. Vultr honours reasoning_effort: "medium" kept every E8 call under 1.8k tokens and still wrote the golden SQL;
# "low" thinks ~0 tokens and wrote worse SQL. DRYDOCK_REASONING_EFFORT overrides; blank = don't send the field.
REASONING_EFFORT_DEFAULT = "medium"
PUBLIC = "Model unavailable, try later"
TIME_UP = "This run hit its time limit. Nothing was applied."
TOKENS_UP = "This run hit its token budget. Nothing was applied."
UNAVAILABLE_4XX = {402, 408, 429}  # payment/daily cap, timeout, rate limit: the endpoint, not our request
RETRYABLE = {408, 429, 500, 502, 503, 504}
FAILOVER_4XX = {401, 403, 404}     # this provider's key or model is wrong: another provider may still work
MAX_RETRIES = 3
_sleep = time.sleep                # test seams
_now = time.monotonic


class ModelError(RuntimeError):
    public = PUBLIC

    def __init__(self, public=PUBLIC, detail="", status=None):
        super().__init__(public)
        self.public, self.detail, self.status = public, detail, status


class ModelUnavailable(ModelError):
    pass


class ModelRequestError(ModelError):
    pass


class ModelTruncated(ModelError):
    def __init__(self, reply):
        super().__init__(PUBLIC, f"finish_reason=length after {reply.tokens_out} tokens")
        self.reply = reply


class ConfigError(ValueError):
    """Settings that must stop the process at start, not on a visitor's first click."""


@dataclass
class Reply:
    message: dict   # assistant message as returned (content and/or tool_calls)
    model_id: str   # the id the provider says served the call
    tokens_in: int
    tokens_out: int
    ms: int
    finish_reason: str | None = None
    host: str = ""  # the host that really answered, for honest labels

    @property
    def text(self) -> str:
        return self.message.get("content") or ""


@dataclass(frozen=True)
class Provider:
    base: str
    key: str
    model: str
    tpm: int = 0    # tokens per minute to pace to; 0 = no pacing


def env_int(name: str, default: int) -> int:
    """Blank or unset = default, so `NAME=` in .env never crashes an import."""
    return int(os.environ.get(name) or default)


def load_env(path=None) -> None:
    """KEY=value lines from .env into os.environ; real env vars win. Prints nothing (values are secrets)."""
    path = Path(path or os.environ.get("DRYDOCK_ENV_FILE") or Path(__file__).resolve().parent.parent / ".env")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.partition("=")
        k = k.strip()
        if sep and k and not k.startswith("#") and " " not in k:
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def api_key() -> str:
    return os.environ.get("LLM_API_KEY") or os.environ.get("VULTR_INFERENCE_API_KEY") or ""


def base_url() -> str:
    return os.environ.get("LLM_BASE_URL") or os.environ.get("VULTR_INFERENCE_BASE_URL") or ""


def _fallbacks() -> list:
    """LLM_FALLBACKS: comma-separated base_url|KEY_ENV_NAME|model[|tpm]. The list names a key's env var, never the key.
    An entry whose key env var is unset is skipped, so one list can sit in .env.example."""
    out = []
    for entry in filter(None, (e.strip() for e in (os.environ.get("LLM_FALLBACKS") or "").split(","))):
        parts = entry.split("|")
        if len(parts) not in (3, 4) or not all(parts):
            raise ConfigError(f"LLM_FALLBACKS entry must be base_url|KEY_ENV_NAME|model[|tpm], got {len(parts)} fields")
        if key := os.environ.get(parts[1]):
            out.append(Provider(parts[0], key, parts[2], int(parts[3]) if len(parts) == 4 else 0))
    return out


def validate_env() -> None:
    """Called at start. No key at all is fine (the app shows the card). A key missing its host or model is not."""
    fallbacks = _fallbacks()
    if api_key():
        if not base_url():
            raise ConfigError("LLM_BASE_URL is required when a key is set: a key is never sent to a default host")
        if not os.environ.get("DRYDOCK_MODEL"):
            raise ConfigError("DRYDOCK_MODEL is required when a key is set")
    elif fallbacks:
        raise ConfigError("LLM_FALLBACKS is set but LLM_API_KEY is not")


def providers(model=None) -> list:
    """Primary first, then fallbacks. Raises ModelUnavailable when nothing may be called."""
    try:
        validate_env()
    except ConfigError as e:
        raise ModelUnavailable(detail=str(e)) from None
    key = api_key()
    if not key:
        raise ModelUnavailable(detail="LLM_API_KEY not set")
    return [Provider(base_url(), key, model or os.environ["DRYDOCK_MODEL"], env_int("LLM_TPM_LIMIT", 0)), *_fallbacks()]


def host_of(base: str) -> str:
    return urlparse(base).hostname or "unknown host"


# ---- budget for one run ----
class RunBudget:
    """One run's wall clock, retry wait and tokens. Defaults: DRYDOCK_RUN_DEADLINE_S, LLM_RETRY_BUDGET_S,
    DRYDOCK_RUN_TOKENS. ponytail: no source fixes these three values; set them from the account's measured limits."""

    def __init__(self, deadline_s=None, retry_s=None, tokens=None):
        d = env_int("DRYDOCK_RUN_DEADLINE_S", 240) if deadline_s is None else deadline_s
        self.ends = _now() + d
        self.retry_left = env_int("LLM_RETRY_BUDGET_S", 90) if retry_s is None else retry_s
        self.tokens_left = env_int("DRYDOCK_RUN_TOKENS", 60000) if tokens is None else tokens

    def left(self) -> float:
        return self.ends - _now()

    def check(self) -> None:
        if self.left() <= 0:
            raise ModelUnavailable(TIME_UP, "run deadline passed")
        if self.tokens_left <= 0:
            raise ModelUnavailable(TOKENS_UP, "run token budget spent")

    def may_wait(self, seconds: float) -> bool:
        return seconds <= self.retry_left and seconds < self.left()


# ---- process-wide meters: the quota belongs to the key, not to a run ----
_lock = threading.Lock()
_windows: dict = {}      # base url -> deque of [monotonic time, tokens]
_day = {"day": None, "tokens": 0}


def reset_meters() -> None:
    with _lock:
        _windows.clear()
        _day.update(day=None, tokens=0)


def tokens_today() -> int:
    today = datetime.now(timezone.utc).date()
    with _lock:
        return _day["tokens"] if _day["day"] == today else 0


def restore_tokens_today(tokens: int) -> None:
    with _lock:
        _day.update(day=datetime.now(timezone.utc).date(), tokens=tokens)


def _add_today(tokens: int) -> None:
    today = datetime.now(timezone.utc).date()
    with _lock:
        if _day["day"] != today:
            _day.update(day=today, tokens=0)
        _day["tokens"] += tokens


def _pace(p: Provider, estimate: int, budget: RunBudget) -> list | None:
    """Waits until `estimate` tokens fit this minute's limit, reserves them, returns the entry to correct afterwards.
    An estimate above the whole limit waits for an empty window, then goes: the provider decides."""
    if not p.tpm:
        return None
    while True:
        with _lock:
            q = _windows.setdefault(p.base, deque())
            now = _now()
            while q and now - q[0][0] >= 60:
                q.popleft()
            if not q or sum(e[1] for e in q) + estimate <= p.tpm:
                entry = [now, estimate]
                q.append(entry)
                return entry
            wait = q[0][0] + 60 - now
        if wait >= budget.left():
            raise ModelUnavailable(TIME_UP, f"pacing to {p.tpm} tokens/min needs {wait:.0f}s, past the run deadline")
        _sleep(wait + 0.01)


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after")
    if not v:
        return None
    try:
        return max(float(v), 0.0)
    except ValueError:
        pass
    try:
        return max((parsedate_to_datetime(v) - datetime.now(timezone.utc)).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return None


def _call(p: Provider, body: dict, budget: RunBudget, transport, timeout) -> Reply:
    body = {**body, "model": body.get("model") or p.model}
    estimate = sum(len(str(v)) for v in body.values()) // 4
    detail = ""
    for attempt in range(MAX_RETRIES + 1):
        budget.check()
        entry = _pace(p, estimate, budget)
        t0 = time.perf_counter()
        r = None
        try:
            with httpx.Client(transport=transport, timeout=min(timeout, max(budget.left(), 1))) as c:
                r = c.post(p.base.rstrip("/") + "/chat/completions", json=body,
                           headers={"Authorization": f"Bearer {p.key}"})
        except httpx.HTTPError as e:
            detail = type(e).__name__
        ms = round((time.perf_counter() - t0) * 1000)
        if r is not None and r.status_code == 200:
            return _parse(r, body, p, ms, entry, budget)
        if r is not None:
            detail = f"HTTP {r.status_code}: {r.text[:200].replace(p.key, '***')}"
            if r.status_code not in RETRYABLE:
                bad_request = 400 <= r.status_code < 500 and r.status_code not in UNAVAILABLE_4XX
                raise (ModelRequestError if bad_request else ModelUnavailable)(detail=detail, status=r.status_code)
        wait = (r is not None and _retry_after(r)) or None
        wait = wait if wait is not None else 2 ** attempt + random.uniform(0, 0.5)
        if attempt == MAX_RETRIES or not budget.may_wait(wait):
            break
        log.warning("%s: %s; retrying in %.1fs", host_of(p.base), detail, wait)
        budget.retry_left -= wait
        _sleep(wait)
    raise ModelUnavailable(detail=detail, status=r.status_code if r is not None else None)


def _parse(r, body, p, ms, entry, budget) -> Reply:
    try:
        data = r.json()
        usage = data.get("usage") or {}
        choice = data["choices"][0]
        reply = Reply(choice["message"], data.get("model", body["model"]), usage.get("prompt_tokens", 0),
                      usage.get("completion_tokens", 0), ms, choice.get("finish_reason"), host_of(p.base))
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ModelUnavailable(detail=f"unreadable response: {r.text[:200].replace(p.key, '***')}") from None
    spent = reply.tokens_in + reply.tokens_out
    if entry is not None:
        entry[1] = spent or entry[1]
    budget.tokens_left -= spent
    _add_today(spent)
    if reply.finish_reason == "length":
        raise ModelTruncated(reply)
    return reply


def chat(messages, *, model=None, temperature=TEMPERATURE, max_tokens=MAX_TOKENS, tools=None,
         response_format=None, transport=None, timeout=None, budget=None) -> Reply:
    """One logical call: paced, retried on 429/5xx inside the run's budget, then tried on the next provider."""
    budget = budget or RunBudget()
    budget.check()
    chain = providers(model)
    body = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
    if response_format:
        body["response_format"] = response_format
    if effort := os.environ.get("DRYDOCK_REASONING_EFFORT", REASONING_EFFORT_DEFAULT):
        body["reasoning_effort"] = effort
    timeout = timeout or env_int("LLM_TIMEOUT_S", 30)
    for i, p in enumerate(chain):
        last = i == len(chain) - 1
        try:
            return _call(p, body, budget, transport, timeout)
        except ModelUnavailable as e:
            if last or e.public != PUBLIC:  # the deadline and token budget are the run's, not this provider's
                raise
            log.warning("%s unavailable, trying the next provider: %s", host_of(p.base), e.detail)
        except ModelRequestError as e:
            if last or e.status not in FAILOVER_4XX:
                raise
            log.warning("%s refused the key or model, trying the next provider: %s", host_of(p.base), e.detail)
    raise AssertionError("unreachable: providers() is never empty")
