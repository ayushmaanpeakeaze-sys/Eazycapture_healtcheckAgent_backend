"""Shared JSON parsing for LLM responses."""
from __future__ import annotations

import json
import re

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_object(raw: str) -> dict:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(raw)
        if not match:
            raise
        return json.loads(match.group(0))
