"""Shared types + aggregation hooks for the per-category check modules.

``SettingField`` lives here (a neutral module with no heavy imports) so both
``audit_settings.py`` and each category module can use it without a circular
import. ``collect_category_setting_fields()`` lazily gathers every category
module's ``SETTING_FIELDS`` so ``settings_schema()`` renders them too.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SettingField:
    key: str          # must match an AuditSettings field name
    group: str        # must match a rules_registry group name
    check: str        # must match a rules_registry rule key
    label: str
    type: str         # bool | int | amount | multiple | percent | list | select
    help: str
    unit: Optional[str] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    options: Optional[tuple[str, ...]] = None   # allowed values for type "select"


# Category modules whose SETTING_FIELDS feed the settings schema. Add a module
# name here when you move a category's settings into app/checks/<name>.py.
_CATEGORY_MODULES: tuple[str, ...] = (
    "fixed_assets",
    "documents",
    "tax",
    "coding",
    "duplicates",
    "bank",
    "dates",
    "prepayments",
    "approval",
    "contacts",
)


def collect_category_setting_fields() -> tuple[SettingField, ...]:
    """Lazily import each category module and gather its SETTING_FIELDS.

    Imported lazily (at call time, not module load) so ``audit_settings`` never
    imports a category module at import time — that would cycle, since category
    modules import ``DEFAULT_SETTINGS`` lazily from ``audit_settings``.
    """
    import importlib

    out: list[SettingField] = []
    for name in _CATEGORY_MODULES:
        mod = importlib.import_module(f"app.modules.healthcheck.checks.{name}")
        out.extend(getattr(mod, "SETTING_FIELDS", ()))
    return tuple(out)


def collect_issue_labels() -> dict[str, str]:
    """issue_type → human label, gathered from every category module's ``META``
    (``(issue_type, label, enabled)``). Lets a report show a readable "Check"
    name instead of the raw issue_type. Lazy import, same reason as above.
    """
    import importlib

    labels: dict[str, str] = {}
    for name in _CATEGORY_MODULES:
        mod = importlib.import_module(f"app.modules.healthcheck.checks.{name}")
        for entry in getattr(mod, "META", ()):
            if len(entry) >= 2:
                labels[str(entry[0])] = str(entry[1])
    return labels
