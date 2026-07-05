"""Health-check engine — deterministic rules + LLM detection, split by concern.

* shared.py            — constants + context/COA helpers
* deterministic.py     — non-LLM rules + the per-document inspection core
* app/checks/*.py      — per-category check modules (detect + settings + meta)
* invoice_firewall.py  — validate_invoice (pre-ledger single document)
* orchestrator.py      — run_batch_health_check (ties it together)

``validate_invoice`` and ``run_batch_health_check`` are exposed at the package
level **lazily** (PEP 562 ``__getattr__``): importing a submodule (e.g. ``shared``)
must NOT eagerly pull in ``orchestrator`` — that triggers a checks_llm import
cycle when ``checks_llm`` happens to be the first module imported in a process.
"""
from typing import Any

__all__ = ["validate_invoice", "run_batch_health_check"]


def __getattr__(name: str) -> Any:
    if name == "run_batch_health_check":
        from app.modules.healthcheck.engine.orchestrator import run_batch_health_check
        return run_batch_health_check
    if name == "validate_invoice":
        from app.modules.healthcheck.engine.invoice_firewall import validate_invoice
        return validate_invoice
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
