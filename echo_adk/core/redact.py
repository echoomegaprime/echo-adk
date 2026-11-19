"""Secret redaction (lifted from Prime ``workspace.ts`` ``redact()`` and extended).

Applied to every model reply and every capability result before it is logged, stored or shown.
``redact`` returns the cleaned text and how many substitutions were made; ``redact_value`` walks
dicts and lists.
"""

from __future__ import annotations

import re
from typing import Any

_KEYWORD = (
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|[a-z0-9_-]*_key|[a-z0-9_-]*_token"
    r"|key|token|secret|password|passwd|credential|credentials|authorization|x-echo-api-key)"
)
_ASSIGNMENT = re.compile(r"(\b" + _KEYWORD + r"\b[\"']?\s*[=:]\s*[\"']?)([^\s\"',;}\]]+)", re.IGNORECASE)
_BEARER = re.compile(r"\b([Bb]earer)\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKENS = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----")
_SECRET_FIELD = re.compile(
    r"^(?:api[_-]?key|apikey|[a-z0-9_-]*token|[a-z0-9_-]*secret|password|passwd|credential|credentials|authorization"
    r"|bearer|private[_-]?key|[a-z0-9_-]*[_-]key|key)$",
    re.IGNORECASE,
)
_AUTH_SCHEMES = ("bearer", "basic", "token", "digest")

REDACTED = "[REDACTED]"


def is_secret_field(name: str) -> bool:
    """True for configuration keys that look like credentials (``api_key``, ``token`` ...)."""
    return bool(_SECRET_FIELD.match(name.strip()))


def _assignment(match: re.Match[str]) -> str:
    # "Authorization: Bearer <token>" keeps the scheme word; the Bearer pass redacts the token itself.
    if match.group(2).lower() in _AUTH_SCHEMES:
        return match.group(0)
    return match.group(1) + REDACTED


def redact(text: str) -> tuple[str, int]:
    """Return ``(clean_text, substitutions)``."""
    if not text:
        return text, 0
    count = 0
    out, n = _PRIVATE_KEY.subn("[REDACTED PRIVATE KEY]", text)
    count += n
    out, n = _ASSIGNMENT.subn(_assignment, out)
    count += n - sum(1 for m in _ASSIGNMENT.finditer(text) if m.group(2).lower() in _AUTH_SCHEMES)
    out, n = _BEARER.subn(lambda m: m.group(1) + " " + REDACTED, out)
    count += n
    out, n = _TOKENS.subn(REDACTED, out)
    count += n
    return out, count


def redact_value(value: Any, *, _depth: int = 0) -> tuple[Any, int]:
    """Redact strings anywhere inside a JSON-like value; secret-named fields are wiped outright."""
    if _depth > 64:
        return REDACTED, 1
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        total = 0
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if is_secret_field(name) and item not in (None, "", [], {}):
                out[name] = REDACTED
                total += 1
                continue
            cleaned, n = redact_value(item, _depth=_depth + 1)
            out[name] = cleaned
            total += n
        return out, total
    if isinstance(value, (list, tuple)):
        total = 0
        items: list[Any] = []
        for item in value:
            cleaned, n = redact_value(item, _depth=_depth + 1)
            items.append(cleaned)
            total += n
        return items, total
    return value, 0
