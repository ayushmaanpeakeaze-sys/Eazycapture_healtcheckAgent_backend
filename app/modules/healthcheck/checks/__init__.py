"""Per-category check modules.

Each file groups one category of checks (mirroring the ``rules_registry``
groups) and keeps that group's **detection logic + tunable settings + registry
metadata together** — so a developer finds everything for, say, the Fixed-Asset
checks in ``fixed_assets.py`` instead of hunting across ``deterministic.py``,
``audit_settings.py`` and ``rules_registry.py``.

The shared engine (orchestrator, trapped feed, BatchTransaction, actions, AI
enrichment) stays shared; only each check's *distinct* pieces live here. The
central files import from these modules so nothing else has to change.
"""
