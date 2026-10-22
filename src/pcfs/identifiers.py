"""Canonical serialization and stable identity helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel
from pydantic_core import to_jsonable_python


def _json_value(value: Any, *, model_json: bool = False) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="python", exclude_none=True), model_json=True)
    if isinstance(value, datetime):
        return to_jsonable_python(value) if model_json else value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item, model_json=model_json) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item, model_json=model_json) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_json_value(item, model_json=model_json) for item in value), key=canonical_json
        )
    return to_jsonable_python(value)


def canonical_json(value: Any) -> str:
    """Serialize a supported value with stable ordering and no incidental whitespace."""
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_digest(value: Any) -> str:
    """Return a SHA-256 digest of canonical content."""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def stable_id(prefix: str, value: Any, *, length: int = 24) -> str:
    """Create a readable stable identifier from canonical content."""
    if not prefix or not prefix.replace("-", "").isalnum():
        raise ValueError("prefix must contain only alphanumeric characters and hyphens")
    return f"{prefix}_{content_digest(value)[:length]}"
