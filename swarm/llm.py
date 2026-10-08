#!/usr/bin/env python3
"""llama.cpp router client (OpenAI-compatible API, no pi involved).

The router (llama-server --models-autoload on 127.0.0.1:8080) serves the
GGUFs. Everything here is plain HTTP: load a model, wait for it, chat.

VRAM reality (this is why runs used to freeze): the swarm needs two
~2GB models, the GPU has 4GB. Asking the router for the second model
while the first is resident fails inside the spawned model server
(ggml ErrorOutOfDeviceMemory) — yet `/models/load` already answered
{"success": true} and the model silently drops back to "unloaded".
Waiting for "loaded" after that never terminates. So warming a model
here means: evict the other residents first, request the load, then
watch the status until it is REALLY "loaded" — with progress output and
a fast retry when the router drops the model instead of loading it.
"""

import json
import os
import time
import urllib.error
import urllib.request

ROUTER_URL = os.environ.get(
    "LLAMA_BASE_URL", "http://127.0.0.1:8080"
).rstrip("/")

COMPLETIONS_URL = f"{ROUTER_URL}/v1/chat/completions"

# Per-attempt budget for a model to reach "loaded". A 2GB model takes a
# few seconds; anything longer means the load failed, not that it is slow.
LOAD_TIMEOUT = 120
LOAD_ATTEMPTS = 3

# A model that has been "unloaded" this long after we asked for it is a
# failed load (the router drops it after the model server exits). Keep
# this short: waiting longer is exactly the old 10-minute freeze.
FAILED_LOAD_GRACE = 20

# Generation budget for one completion. On this GPU a full 2K-token
# report takes well under two minutes; longer means the router stalled.
REQUEST_TIMEOUT = 300


def _log(message: str):
    """Progress output — a silent wait looks like a freeze."""
    print(message, flush=True)


def _request(url: str, payload: dict = None, timeout: int = 15) -> bytes:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )

    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def model_catalog() -> list:
    """All router models as [{"id": …, "status": …}]."""
    catalog = json.loads(_request(f"{ROUTER_URL}/models", timeout=10) or b"{}")

    models = []

    for model in catalog.get("data", []):
        models.append({
            "id": model.get("id"),
            "status": (model.get("status") or {}).get("value") or "unknown",
        })

    return models


def router_status(model_id: str) -> str:
    """Return the router's status value for a model ('loaded', ...)."""
    for model in model_catalog():
        if model["id"] == model_id:
            return model["status"]

    raise RuntimeError(f"Model not in router catalog: {model_id}")


def _safe_status(model_id: str) -> str:
    """Status for polling: a network hiccup is 'unknown', a wrong model
    id is a configuration error and must not be swallowed."""
    try:
        return router_status(model_id)
    except RuntimeError:
        raise
    except Exception:
        return "unknown"


def _post(path: str, payload: dict) -> bool:
    """POST to a router control endpoint. False when the router refuses."""
    try:
        _request(f"{ROUTER_URL}{path}", payload, timeout=30)
        return True
    except Exception:
        return False


def evict_other_models(model_id: str):
    """Unload every other resident model so `model_id` fits in VRAM.

    Only one ~2GB model fits on this GPU. The router will not free the
    VRAM on its own (it answers {"success": true} to the load and then
    fails to allocate), so the client has to make room.
    """
    for model in model_catalog():
        if model["id"] == model_id or model["status"] in ("unloaded", "unknown"):
            continue

        _log(f"[llm] unloading {model['id']} to free VRAM")

        if not _post("/models/unload", {"model": model["id"]}):
            # Older routers call this eject; either way, best effort.
            _post("/models/eject", {"model": model["id"]})


def ensure_model_loaded(model_id: str, timeout: int = LOAD_TIMEOUT):
    """Wait until the model is loaded in the router.

    Requests that hit the router mid-swap get a 500 "failed to load" and
    come back empty, so every agent warms its model before it starts.
    A load that the router drops (no VRAM) is retried instead of being
    waited on forever.
    """
    for attempt in range(1, LOAD_ATTEMPTS + 1):
        status = _safe_status(model_id)

        if status == "loaded":
            return

        if status != "loading":
            # "unloaded", "failed", or a stray "unknown": make room and
            # ask for the load. The response is not trustworthy, so the
            # poll below is the only real answer.
            evict_other_models(model_id)
            _post("/models/load", {"model": model_id})

        _log(f"[llm] loading {model_id} (attempt {attempt}/{LOAD_ATTEMPTS})")

        started = time.time()
        seen_loading = False

        while time.time() - started < timeout:
            status = _safe_status(model_id)

            if status == "loaded":
                _log(f"[llm] {model_id} ready in {time.time() - started:.0f}s")
                return

            seen_loading = seen_loading or status == "loading"

            if status == "unloaded" and (
                seen_loading or time.time() - started > FAILED_LOAD_GRACE
            ):
                # The router started (or was asked) and dropped the model
                # again: the load failed, typically VRAM. Waiting longer
                # is what used to freeze the run for ten minutes.
                _log(f"[llm] {model_id} load was dropped by the router")
                break

            if status == "sleeping":
                # Some builds park models instead of unloading them.
                # Ask for a wake, but do not block on it: a request on a
                # sleeping model still works (autoload), and chat()
                # retries a 500 from a mid-wake request.
                _post("/models/load", {"model": model_id})
                if time.time() - started > 30:
                    return

            time.sleep(1)

    raise RuntimeError(
        f"Router never loaded {model_id}. Check the router's own log — on "
        f"a small GPU the load fails with 'ErrorOutOfDeviceMemory' unless "
        f"only one model is resident (this client unloads the others "
        f"first), and note that -ngl 999 disables llama.cpp's automatic "
        f"'fit layers to free VRAM' fallback."
    )


def chat(
    model: str,
    messages: list,
    max_tokens: int = 2048,
    temperature: float = 0.2,
) -> str:
    """One chat completion. Returns visible text (never reasoning).

    Retries a dead turn — no text at all means the request never reached
    the model (typically a 500 while the router was between models) —
    and retries a stalled request instead of crashing the run.
    """
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    body = json.dumps(payload).encode("utf-8")

    text = ""

    for attempt in range(3):
        if attempt:
            _log(f"[llm] no usable reply from {model}, retrying")
            ensure_model_loaded(model)
            time.sleep(2)

        req = urllib.request.Request(
            COMPLETIONS_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                reply = json.loads(r.read())
        except urllib.error.HTTPError as error:
            if attempt == 2:
                detail = error.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(
                    f"llama.cpp refused the completion: {error.code} {detail}"
                )
            # Most likely a 500 mid-swap: re-warm and try again.
            continue
        except Exception as error:
            # Socket timeout, reset connection, bad JSON. The router may
            # have died between models — retry instead of taking the run
            # down with a traceback.
            if attempt == 2:
                raise RuntimeError(
                    f"llama.cpp completion failed: {error}"
                )
            _log(f"[llm] request error: {error}")
            continue

        choice = (reply.get("choices") or [{}])[0]
        message = choice.get("message") or {}

        text = (message.get("content") or "").strip()

        if not text:
            # Reasoning models may answer inside the reasoning channel.
            text = (message.get("reasoning_content") or "").strip()

        if text:
            break

    return text
