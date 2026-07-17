"""SOP hierarchy precedence — Capital > Prepayment > Payment/Inconsistency."""
from app.modules.healthcheck.tasks import _apply_hierarchy


def _f(txid, issue_type):
    return {"transaction_id": txid, "issue_type": issue_type, "message": issue_type}


def _by(out):
    return {f["issue_type"]: f for f in out}


def test_capital_supersedes_payment_and_prepayment():
    out = _by(_apply_hierarchy([
        _f("t1", "capital_item_review"), _f("t1", "unusual_payment"), _f("t1", "prepayment_review")]))
    assert out["capital_item_review"].get("superseded_by") is None
    assert out["unusual_payment"]["superseded_by"] == "capital_item_review"
    assert out["prepayment_review"]["superseded_by"] == "capital_item_review"


def test_low_cost_fixed_asset_is_capital_tier():
    out = _by(_apply_hierarchy([_f("t1", "low_cost_fixed_asset"), _f("t1", "unusual_payment")]))
    assert out["low_cost_fixed_asset"].get("superseded_by") is None
    assert out["unusual_payment"]["superseded_by"] == "low_cost_fixed_asset"


def test_prepayment_supersedes_payment():
    out = _by(_apply_hierarchy([
        _f("t1", "prepayment_review"), _f("t1", "unusual_payment"), _f("t1", "amount_outlier")]))
    assert out["prepayment_review"].get("superseded_by") is None
    assert out["unusual_payment"]["superseded_by"] == "prepayment_review"
    assert out["amount_outlier"]["superseded_by"] == "prepayment_review"


def test_genuine_payment_anomaly_not_superseded():
    out = _apply_hierarchy([_f("t1", "unusual_payment")])
    assert out[0].get("superseded_by") is None


def test_other_axes_not_superseded():
    out = _by(_apply_hierarchy([
        _f("t1", "capital_item_review"), _f("t1", "duplicate_bill"),
        _f("t1", "undocumented_bill"), _f("t1", "unusual_payment")]))
    assert out["duplicate_bill"].get("superseded_by") is None
    assert out["undocumented_bill"].get("superseded_by") is None
    assert out["unusual_payment"]["superseded_by"] == "capital_item_review"


def test_separate_transactions_are_independent():
    out = {(f["transaction_id"], f["issue_type"]): f
           for f in _apply_hierarchy([_f("t1", "capital_item_review"), _f("t2", "unusual_payment")])}
    assert out[("t2", "unusual_payment")].get("superseded_by") is None
