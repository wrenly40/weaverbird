"""The brain — model calls, bring-your-own-key (BYOK).

Weaverbird ships no keys and calls no model with its own account. The
user configures a model ladder in the global settings (cheap, mid,
frontier); each tier names a provider, a model, an optional base URL,
and the NAME of an environment variable that holds the user's key
(``api_key_env``). The key is read from the environment at the moment
a call is made, used for that call's Authorization header, and never
written anywhere — not to the ledger, not to a log, not to a report.

Two provider kinds, both over the standard library only:

- ``openai`` — OpenAI-compatible chat completions. Works for OpenAI
  itself, OpenRouter, Together, and local servers (Ollama, LM Studio)
  via ``base_url``.
- ``anthropic`` — the Anthropic Messages API.

Every completed call is written to the project's ledger BEFORE the
result is returned, with the real token counts from the provider's
response. Calls are refused when the project is already at or over
the global per-game token cap, and the result carries a loud warning
when a call crosses the cap. When no key, no configuration or no
network is available, callers get a plain BrainUnavailable-style
result — never a crash.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import ledger as ledger_module
from . import scope as scope_module
from . import settings as settings_module

PROVIDERS = ("openai", "anthropic")
TIERS = ledger_module.TIERS

_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}
_DEFAULT_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")


class BrainError(Exception):
    """Raised for brain problems the caller must surface (e.g. budget)."""


class BrainBudgetError(BrainError):
    """Raised when a call is refused because the budget cap is reached."""


@dataclass
class BrainResult:
    """The outcome of one model call attempt."""

    ok: bool
    tier: str = ""
    stage: str = ""
    text: str = ""
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    warning: str = ""
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ---------------------------------------------------------------------------
# Settings and tier configuration

def resolve_settings(settings_path=None, project_dir=None):
    """Find the global settings file. Returns (path, data) or (None, None).

    Order: the explicit path, $WEAVERBIRD_SETTINGS, the project's own
    directory, the workspace above the project, the current directory,
    and ~/.weaverbird/.
    """
    candidates: list[Path] = []
    if settings_path:
        candidates.append(Path(settings_path))
    env_value = os.environ.get("WEAVERBIRD_SETTINGS")
    if env_value:
        candidates.append(Path(env_value))
    if project_dir:
        project = Path(project_dir)
        candidates.append(project / "global-settings.yaml")
        candidates.append(project.parent / "global-settings.yaml")
    candidates.append(Path.cwd() / "global-settings.yaml")
    candidates.append(Path.home() / ".weaverbird" / "global-settings.yaml")
    for candidate in candidates:
        if candidate.is_file():
            try:
                return candidate, settings_module.load_settings(candidate)
            except settings_module.SettingsError:
                return candidate, {}
    return None, None


def tier_config(data: dict | None, tier: str) -> dict:
    """The normalised config for one ladder tier ({} when unset)."""
    if not isinstance(data, dict):
        return {}
    models = data.get("models")
    if not isinstance(models, dict):
        return {}
    raw = models.get(tier)
    if not isinstance(raw, dict):
        return {}
    provider = str(raw.get("provider") or "").strip().lower()
    model = str(raw.get("model") or "").strip()
    base_url = str(raw.get("base_url") or "").strip().rstrip("/") or None
    key_env = str(raw.get("api_key_env") or "").strip() or None
    price = raw.get("cost_per_1k_tokens")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key_env": key_env,
        "cost_per_1k_tokens": price,
    }


def _key_env_for(config: dict) -> str | None:
    if config.get("api_key_env"):
        return config["api_key_env"]
    return _DEFAULT_KEY_ENVS.get(config.get("provider", ""))


def _is_local(base_url: str | None) -> bool:
    if not base_url:
        return False
    return any(host in base_url for host in _LOCAL_HOSTS)


def tier_status(data: dict | None, tier: str) -> dict:
    """Whether a tier is usable: provider + model set, key present.

    Only ever reports whether the key environment variable is set —
    never its value.
    """
    config = tier_config(data, tier)
    provider = config.get("provider", "")
    model = config.get("model", "")
    key_env = _key_env_for(config) if provider else config.get("api_key_env")
    key_present = bool(key_env and os.environ.get(key_env))
    base_url = config.get("base_url") or _DEFAULT_BASE_URLS.get(provider)
    configured = bool(provider in PROVIDERS and model)
    usable = configured and (key_present or _is_local(base_url))
    reason = ""
    if not data:
        reason = "No global settings file found."
    elif provider and provider not in PROVIDERS:
        reason = f"Unknown provider {provider!r} (supported: {', '.join(PROVIDERS)})."
    elif not configured:
        reason = "No provider/model set for this tier in the global settings."
    elif not usable:
        reason = f"The key environment variable {key_env} is not set."
    return {
        "tier": tier,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key_env": key_env,
        "key_present": key_present,
        "configured": configured,
        "usable": usable,
        "reason": reason,
    }


def brain_status(data: dict | None) -> dict:
    """Status of every tier in the ladder."""
    return {tier: tier_status(data, tier) for tier in TIERS}


# ---------------------------------------------------------------------------
# The HTTP transport (one place; tests replace it)

def _send(url: str, headers: dict, body: bytes, timeout: int = 60):
    """POST body to url. Returns (status, response text).

    The key travels in the headers of this one request and nowhere
    else. HTTP errors are returned, not raised, so callers can turn
    them into plain unavailable results.
    """
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _usage_openai(payload: dict) -> tuple[int, int]:
    usage = payload.get("usage") or {}
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
    if not prompt and not completion and usage.get("total_tokens"):
        prompt = int(usage["total_tokens"])
    return int(prompt or 0), int(completion or 0)


def _usage_anthropic(payload: dict) -> tuple[int, int]:
    usage = payload.get("usage") or {}
    return int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)


def _text_openai(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # some providers return content parts
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return choices[0].get("text", "") or ""


def _text_anthropic(payload: dict) -> str:
    parts = payload.get("content") or []
    return "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    )


def _image_block_openai(image_bytes: bytes, media_type: str) -> dict:
    import base64
    data_uri = "data:%s;base64,%s" % (
        media_type, base64.b64encode(image_bytes).decode("ascii"))
    return {"type": "image_url", "image_url": {"url": data_uri}}


def _image_block_anthropic(image_bytes: bytes, media_type: str) -> dict:
    import base64
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(image_bytes).decode("ascii"),
        },
    }


def _with_image(messages: list, image_bytes: bytes, media_type: str,
                provider: str) -> list:
    """Return a copy of messages with the image attached to the last
    user turn, shaped for the provider: OpenAI-compatible calls take
    a content-parts list with an image_url data URI; Anthropic takes
    content blocks with a base64 image source."""
    block = (_image_block_anthropic(image_bytes, media_type)
             if provider == "anthropic"
             else _image_block_openai(image_bytes, media_type))
    out = [dict(m) for m in messages]
    for index in range(len(out) - 1, -1, -1):
        if out[index].get("role") == "user":
            text = out[index].get("content", "")
            out[index] = {"role": "user",
                          "content": [{"type": "text", "text": text}, block]}
            return out
    out.append({"role": "user", "content": [block]})
    return out


_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp",
                ".gif": "image/gif"}


def _call_provider(config: dict, key: str | None, messages: list,
                   max_tokens: int | None) -> tuple[int, int, str]:
    """Make the raw provider call. Returns (prompt, completion, text)."""
    provider = config["provider"]
    base = config.get("base_url") or _DEFAULT_BASE_URLS[provider]
    limit = max_tokens or 1024
    if provider == "openai":
        url = base + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {"model": config["model"], "messages": messages,
                "max_tokens": limit}
        status, text = _send(url, headers, json.dumps(body).encode("utf-8"))
        if status != 200:
            raise BrainError(f"The provider answered HTTP {status}.")
        payload = json.loads(text)
        prompt, completion = _usage_openai(payload)
        return prompt, completion, _text_openai(payload)
    # anthropic
    url = base + "/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if key:
        headers["x-api-key"] = key
    system = "\n".join(
        m.get("content", "") for m in messages if m.get("role") == "system"
    )
    turns = [
        {"role": m.get("role", "user"), "content": m.get("content", "")}
        for m in messages
        if m.get("role") != "system"
    ] or [{"role": "user", "content": ""}]
    body = {"model": config["model"], "max_tokens": limit, "messages": turns}
    if system:
        body["system"] = system
    status, text = _send(url, headers, json.dumps(body).encode("utf-8"))
    if status != 200:
        raise BrainError(f"The provider answered HTTP {status}.")
    payload = json.loads(text)
    prompt, completion = _usage_anthropic(payload)
    return prompt, completion, _text_anthropic(payload)


# ---------------------------------------------------------------------------
# The public call

def complete(project_dir, tier: str, messages: list, stage: str,
             settings_path=None, max_tokens: int | None = None) -> BrainResult:
    """Run one model call on a ladder tier, ledger-first.

    Returns a BrainResult. Unavailable brain (no settings, tier not
    configured, key missing, network failure, bad response) comes
    back as ``ok=False`` with a plain ``error`` — it never raises.
    The one refusal that raises is the budget: when the project's
    ledger is already at or over the global per-game token cap,
    BrainBudgetError is raised before any call is made.
    """
    return _complete_core(project_dir, tier, messages, stage,
                          settings_path=settings_path,
                          max_tokens=max_tokens)


def complete_with_image(project_dir, tier: str, text: str, image_path,
                        stage: str, settings_path=None,
                        max_tokens: int | None = None) -> BrainResult:
    """One vision call: a text prompt plus one image file.

    The image is attached to the user turn in the provider's own
    shape (OpenAI-compatible: an image_url part with a base64 data
    URI; Anthropic: a base64 image content block). Everything else —
    tier resolution, the budget refusal, the ledger entry written
    before the result is returned — is the same as `complete`.
    An unreadable image comes back as ok=False, never a raise.
    """
    path = Path(image_path)
    media_type = _MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None or not path.is_file():
        result = BrainResult(ok=False, tier=tier, stage=stage)
        result.error = (f"The image {path} is missing or not a "
                        "supported image file (png/jpg/webp/gif).")
        return result
    try:
        image_bytes = path.read_bytes()
    except OSError as exc:
        result = BrainResult(ok=False, tier=tier, stage=stage)
        result.error = f"The image {path} could not be read: {exc}"
        return result

    def shape(messages, provider):
        return _with_image(messages, image_bytes, media_type, provider)

    return _complete_core(
        project_dir, tier, [{"role": "user", "content": text}], stage,
        settings_path=settings_path, max_tokens=max_tokens, shape=shape)


def _complete_core(project_dir, tier: str, messages: list, stage: str,
                   settings_path=None, max_tokens: int | None = None,
                   shape=None) -> BrainResult:
    result = BrainResult(ok=False, tier=tier, stage=stage)
    if tier not in TIERS:
        result.error = f"Unknown tier {tier!r}. Known tiers: {', '.join(TIERS)}."
        return result
    settings_file, data = resolve_settings(settings_path, project_dir)
    if not settings_file:
        result.error = (
            "No global settings file found, so no model is configured. "
            "Create one from the global-settings template and fill in the "
            "models ladder — Weaverbird is bring-your-own-key."
        )
        return result
    status = tier_status(data, tier)
    result.provider = status["provider"]
    result.model = status["model"]
    if not status["usable"]:
        result.error = (
            f"The {tier} tier is not usable: {status['reason']} "
            "No call was made and nothing was spent."
        )
        return result

    config = tier_config(data, tier)
    budgets = data.get("budgets", {}) if isinstance(data, dict) else {}
    cap = budgets.get("tokens_per_game")
    used = ledger_module.summarise(project_dir)["tokens"]
    if isinstance(cap, (int, float)) and cap and used >= cap:
        raise BrainBudgetError(
            f"Budget reached: this project has already used {used} tokens, "
            f"at or over the global per-game cap of {int(cap)}. The call was "
            "refused. Raise the cap in the global settings if you want to go on."
        )

    if shape is not None:
        messages = shape(messages, config["provider"])
    key_env = _key_env_for(config)
    key = os.environ.get(key_env) if key_env else None
    try:
        prompt_tokens, completion_tokens, text = _call_provider(
            config, key, messages, max_tokens
        )
    except BrainError as exc:
        result.error = f"The {tier} model call failed: {exc} Nothing was spent."
        return result
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result.error = (
            f"The {tier} model could not be reached ({type(exc).__name__}). "
            "Nothing was spent."
        )
        return result

    total = prompt_tokens + completion_tokens
    price = config.get("cost_per_1k_tokens")
    cost = round(total / 1000.0 * price, 6) if price else 0.0
    # The ledger entry lands before the caller sees the text.
    ledger_module.add_entry(project_dir, stage, tier, total, cost)
    result.ok = True
    result.text = text
    result.prompt_tokens = prompt_tokens
    result.completion_tokens = completion_tokens
    result.cost = cost
    if isinstance(cap, (int, float)) and cap and used + total >= cap:
        result.warning = (
            f"WARNING: this call crossed the global per-game token cap "
            f"({used + total} of {int(cap)} tokens). Further calls will be refused."
        )
    return result


# ---------------------------------------------------------------------------
# Scope interview — the first real brain job

_EXTRACTION_SYSTEM = (
    "You extract answers for a game scope file from the maker's prompt. "
    "Answer with one JSON object only — no prose, no code fences. Keys are "
    "the exact field names given; values are short plain answers taken "
    "from, or directly implied by, the prompt. If the prompt does not say "
    "enough about a field, use an empty string for it. Never invent "
    "details the maker did not give."
)


def extract_json(text: str) -> dict:
    """Parse a JSON object out of a model reply, tolerating stray
    prose and code fences. Returns {} when nothing parses. Values
    keep their JSON types (nested objects stay objects) — use this
    for structured replies; `_parse_extraction` flattens to strings."""
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned,
                     flags=re.MULTILINE).strip()
    candidates = [cleaned]
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _parse_extraction(text: str) -> dict:
    """Parse the model's JSON answer, tolerating stray prose/fences."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned,
                     flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int, float))}


def interview_from_prompt(project_dir, prompt_text: str,
                          settings_path=None) -> dict:
    """Store a prompt in the scope file and extract what it answers.

    Always stores the prompt. With a usable cheap tier, one model call
    extracts answers for the still-empty REQUIRED fields and applies
    them to fields that are empty (a filled field is never
    overwritten). Without a brain it degrades gracefully: nothing is
    extracted and every question comes back. Returns a dict with the
    prompt stored, the answers applied, whether a model was used, a
    plain note, and the remaining grouped interview questions.
    """
    project = Path(project_dir)
    scope_file = scope_module.scope_path_for(project)
    if not scope_file.exists():
        raise scope_module.ScopeError(f"No scope file found at {scope_file}.")
    parsed = scope_module.set_original_prompt(scope_file, prompt_text)

    outcome = {
        "prompt_stored": True,
        "applied": {},
        "brain_used": False,
        "note": "",
        "brain_result": None,
    }

    missing = parsed.missing_fields
    if not missing:
        outcome["note"] = "The scope is already complete — nothing to extract."
        outcome["questions"] = []
        outcome["missing_count"] = 0
        return outcome

    settings_file, data = resolve_settings(settings_path, project)
    status = tier_status(data, "cheap") if settings_file else None
    if status is None or not status["usable"]:
        reason = status["reason"] if status else "No global settings file found."
        outcome["note"] = (
            f"No model is configured, so nothing was extracted ({reason}) "
            "The prompt is stored; please answer the questions below."
        )
        outcome["questions"] = scope_module.interview_questions(parsed)
        outcome["missing_count"] = len(missing)
        return outcome

    wanted = {f.name: scope_module.question_for(f) for f in missing}
    messages = [
        {"role": "system", "content": _EXTRACTION_SYSTEM},
        {"role": "user", "content": (
            "The maker's prompt:\n\n" + prompt_text + "\n\n"
            "Fields to fill (name: what it asks):\n"
            + "\n".join(f"- {name}: {q}" for name, q in wanted.items())
            + "\n\nReturn the JSON object now."
        )},
    ]
    try:
        result = complete(project, "cheap", messages, stage="scope",
                          settings_path=settings_file, max_tokens=900)
    except BrainBudgetError as exc:
        outcome["note"] = f"{exc} The prompt is stored; nothing was extracted."
        outcome["questions"] = scope_module.interview_questions(parsed)
        outcome["missing_count"] = len(missing)
        return outcome
    outcome["brain_result"] = result
    if not result.ok:
        outcome["note"] = (
            f"The model call did not work ({result.error}) "
            "The prompt is stored; please answer the questions below."
        )
        outcome["questions"] = scope_module.interview_questions(parsed)
        outcome["missing_count"] = len(missing)
        return outcome

    outcome["brain_used"] = True
    answers = _parse_extraction(result.text)
    applied: dict = {}
    for fld in missing:
        raw = answers.get(fld.name, "")
        value = scope_module._clean_value(str(raw)) if raw else ""
        if value:
            scope_module.set_field_value(scope_file, fld.name, value)
            applied[fld.name] = value
    outcome["applied"] = applied
    if result.warning:
        outcome["note"] = result.warning
    reparsed = scope_module.load_scope(scope_file)
    outcome["questions"] = scope_module.interview_questions(reparsed)
    outcome["missing_count"] = len(reparsed.missing_fields)
    return outcome
