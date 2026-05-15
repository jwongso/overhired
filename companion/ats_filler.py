"""ATS form filler using cached JSON field operations."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from tool_server import _safe_domain

if TYPE_CHECKING:
    from ai_client import AIClient

FILLERS_DIR = Path("~/.grapply/fillers").expanduser()
_log = logging.getLogger(__name__)
_LAST_CACHE_HIT = False
_BASE_ALLOWED_VALUE_KEYS = {
    "name", "email", "phone", "cover_letter",
    "linkedin", "github", "website",
    "location", "work_authorization", "availability", "salary_expectation",
}

def _make_system_prompt(available_keys: set[str]) -> str:
    keys_str = "|".join(sorted(available_keys))
    return (
        "You are a JSON generator. Output ONLY a valid JSON array — no explanation, no markdown.\n"
        f'Each element: {{"selector": "CSS selector string", "value_key": "{keys_str}"}}\n'
        "Pick the value_key that best matches each field's purpose.\n"
        "Only include fields you can confidently match — skip optional/unknown fields."
    )


def get_filler(domain: str, form_snapshot: list[dict], ai: "AIClient",
               fill_data: dict | None = None) -> list[dict] | None:
    """Return cached or freshly-generated fill operations."""
    global _LAST_CACHE_HIT

    try:
        cached = _try_cached(domain, form_snapshot)
    except Exception as exc:
        _log.warning("[ats_cache] stale/invalid for %s — deleting (%s)", domain, exc)
        _delete_cached(domain)
        cached = None

    if cached:
        _LAST_CACHE_HIT = True
        return cached

    _LAST_CACHE_HIT = False
    try:
        operations = _one_shot_fill(domain, form_snapshot, ai, fill_data or {})
    except Exception as exc:
        _log.warning("[fill] generation failed for %s: %s", domain, exc)
        return None

    if not operations or not _looks_valid_filler(operations, form_snapshot):
        return None

    _save_filler(domain, operations)
    return _try_cached(domain, form_snapshot)


def last_cache_hit() -> bool:
    return _LAST_CACHE_HIT


def _try_cached(domain: str, form_snapshot: list[dict]) -> list[dict] | None:
    """Return cached operations if they still match the current form."""
    path = FILLERS_DIR / f"{_safe_domain(domain)}.json"
    if not path.exists():
        return None

    try:
        operations = json.loads(path.read_text(encoding="utf-8"))
        if not _looks_valid_filler(operations, form_snapshot):
            _log.warning("[ats_cache] stale/invalid for %s — deleting", domain)
            path.unlink(missing_ok=True)
            return None
        _log.info("[ats_cache] hit for %s", domain)
        return operations
    except Exception:
        _log.warning("[ats_cache] stale/invalid for %s — deleting", domain)
        path.unlink(missing_ok=True)
        return None


def _one_shot_fill(domain: str, form_snapshot: list[dict], ai: "AIClient",
                   fill_data: dict) -> list[dict] | None:
    """Ask the LLM for a JSON array of field operations."""
    available_keys = _BASE_ALLOWED_VALUE_KEYS | set(fill_data.keys())
    system_prompt = _make_system_prompt(available_keys)

    # Show LLM what data is actually available to fill with
    data_hints = "\n".join(
        f"- {k}: {repr(v[:80]) if isinstance(v, str) and len(v) > 80 else repr(v)}"
        for k, v in fill_data.items() if v
    )
    useful = [field for field in form_snapshot if field.get("id") or field.get("name") or field.get("label")][:20]
    fields_summary = "\n".join(
        f"- id={field.get('id')!r} name={field.get('name')!r} label={field.get('label')!r} "
        f"placeholder={field.get('placeholder')!r} aria_label={field.get('aria_label')!r} type={field.get('type')!r}"
        for field in useful
    ) or "- (no usable fields provided)"
    user_prompt = (
        f"Domain: {domain}\n\n"
        f"Available data to fill with:\n{data_hints or '(none provided)'}\n\n"
        f"Form fields (use only real fields from this list):\n{fields_summary}\n\n"
        "Return the operations array that fills the form. "
        "Each item must include selector and value_key."
    )
    try:
        raw = ai.generate(system_prompt, user_prompt, timeout=ai.tool_timeout).strip()
        operations = _extract_json_array(raw)
        if operations:
            validation = _validate_candidate(operations, form_snapshot, available_keys)
            _log.info("[fill] one-shot validation for %s: %s", domain, validation)
            if validation.get("valid"):
                return operations
    except Exception as exc:
        _log.warning("[fill] one-shot failed for %s: %s", domain, exc)
    return None


def _looks_valid_filler(ops: object, form_snapshot: list[dict]) -> bool:
    """Basic sanity check for generated fill operations."""
    return bool(_validate_candidate(ops, form_snapshot, _BASE_ALLOWED_VALUE_KEYS).get("valid"))


def _validate_candidate(ops: object, form_snapshot: list[dict],
                        allowed_keys: set[str] | None = None) -> dict:
    if allowed_keys is None:
        allowed_keys = _BASE_ALLOWED_VALUE_KEYS
    if not isinstance(ops, list) or not ops:
        return {"valid": False, "error": "Operations must be a non-empty list."}

    for idx, op in enumerate(ops):
        if not isinstance(op, dict):
            return {"valid": False, "error": f"Operation {idx} must be an object."}
        selector = op.get("selector")
        value_key = op.get("value_key")
        if not isinstance(selector, str) or not selector.strip():
            return {"valid": False, "error": f"Operation {idx} is missing a selector."}
        if value_key not in allowed_keys:
            return {"valid": False, "error": f"Operation {idx} has invalid value_key {value_key!r}."}

    if not _has_field_reference(ops, form_snapshot):
        return {"valid": False, "error": "Operations do not reference any current form fields."}

    return {"valid": True, "error": ""}


def _has_field_reference(ops: object, form_snapshot: list[dict]) -> bool:
    if not isinstance(ops, list):
        return False

    tokens = _reference_tokens(form_snapshot)
    if not tokens:
        return False

    for op in ops:
        selector = str((op or {}).get("selector", "")).lower()
        if selector and any(token in selector for token in tokens):
            return True
    return False


def _reference_tokens(form_snapshot: list[dict]) -> set[str]:
    tokens: set[str] = set()
    for field in form_snapshot or []:
        for key in ("id", "name", "label", "placeholder", "aria_label"):
            value = str(field.get(key, "")).strip().lower()
            if not value:
                continue
            tokens.add(value)
            for part in re.split(r"[^a-z0-9_]+", value):
                if len(part) >= 3:
                    tokens.add(part)
    return tokens


def _extract_json_array(text: str) -> list[dict] | None:
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)

    for fence in re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE):
        candidate = fence.strip()
        if candidate:
            candidates.append(candidate)

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start:end + 1].strip())

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return parsed
    return None


def _save_filler(domain: str, operations: list[dict]) -> None:
    FILLERS_DIR.mkdir(parents=True, exist_ok=True)
    path = FILLERS_DIR / f"{_safe_domain(domain)}.json"
    path.write_text(json.dumps(operations, indent=2), encoding="utf-8")


def _delete_cached(domain: str) -> None:
    (FILLERS_DIR / f"{_safe_domain(domain)}.json").unlink(missing_ok=True)
