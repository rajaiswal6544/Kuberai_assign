"""H9: reconciliation finds exactly the anomalies planted in samples/txn_log.csv."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("reconcile", ROOT / "scripts" / "reconcile.py")
reconcile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reconcile)

SAMPLE = ROOT / "samples" / "txn_log.csv"


def run(path: Path, out: Path, *extra: str) -> tuple[int, dict]:
    code = reconcile.main(["reconcile.py", str(path), "--out", str(out), *extra])
    return code, json.loads(out.read_text(encoding="utf-8"))


def key(a: dict) -> tuple:
    return (
        a["type"],
        a.get("reason"),
        a.get("order_id") or tuple(a.get("order_ids", ())),
        a.get("txn_id") or tuple(a.get("txn_ids", ())),
    )


def test_sample_log_anomalies_are_exactly_the_planted_ones(tmp_path, capsys):
    code, report = run(SAMPLE, tmp_path / "report.json")
    assert code == 1  # anomalies found
    assert report["rows"] == 30
    assert report["anomaly_counts"] == {
        "DUPLICATE_ROW": 1, "DOUBLE_CHARGE": 2, "MISSING_CONFIRMATION": 2,
        "ORPHAN_CONFIRMATION": 1, "AMOUNT_MISMATCH": 1,
    }
    assert [key(a) for a in report["anomalies"]] == [
        ("DUPLICATE_ROW", None, "O2", "t05"),
        ("DOUBLE_CHARGE", "same_order", "O3", ("t08", "t09")),
        ("DOUBLE_CHARGE", "same_user_amount_within_window", ("O4", "O5"), ("t12", "t15")),
        ("MISSING_CONFIRMATION", None, "O6", ()),
        ("MISSING_CONFIRMATION", None, "O7", ()),
        ("ORPHAN_CONFIRMATION", None, "O8", "t22"),
        ("AMOUNT_MISMATCH", None, "O9", ()),
    ]
    by_order = {a.get("order_id"): a for a in report["anomalies"]}
    assert by_order["O6"]["missing"] == ["confirmation"]
    assert by_order["O7"]["missing"] == ["gold_credited", "confirmation"]
    assert (by_order["O9"]["charged"], by_order["O9"]["credited"], by_order["O9"]["difference"]) == ("750.00", "700.00", "50.00")
    assert "DOUBLE_CHARGE" in capsys.readouterr().out  # readable table printed too


def test_clean_orders_and_decoys_are_not_flagged(tmp_path):
    _, report = run(SAMPLE, tmp_path / "report.json")
    flagged = json.dumps(report["anomalies"])
    for clean in ('"O1"', '"O10"', '"O11"'):  # complete; same user+amount but 5 min later; initiated only
        assert clean not in flagged


def test_window_is_configurable(tmp_path):
    _, report = run(SAMPLE, tmp_path / "report.json", "--window", "10")
    assert report["anomaly_counts"]["DOUBLE_CHARGE"] == 1  # O4/O5 are 30 s apart


def test_jsonl_input_and_malformed_rows(tmp_path):
    log = tmp_path / "log.jsonl"
    rows = [
        {"txn_id": "a", "user_id": "u", "order_id": "X", "event": "charge_success", "amount": "10.00", "ts": "2026-10-02T10:00:00Z"},
        {"txn_id": "b", "user_id": "u", "order_id": "X", "event": "gold_credited", "amount": "10.00", "ts": "2026-10-02T10:00:01Z"},
        {"txn_id": "c", "user_id": "u", "order_id": "X", "event": "confirmation", "amount": "10.00", "ts": "2026-10-02T10:00:02Z"},
        {"txn_id": "d", "user_id": "u", "order_id": "Y", "event": "refund", "amount": "NaN", "ts": "bad"},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    _, report = run(log, tmp_path / "report.json")
    assert [a["type"] for a in report["anomalies"]] == ["MALFORMED_ROW"]


def test_clean_log_exits_zero(tmp_path):
    log = tmp_path / "clean.csv"
    log.write_text(
        "txn_id,user_id,order_id,event,amount,ts\n"
        "a,u,X,charge_success,10.00,2026-10-02T10:00:00Z\n"
        "b,u,X,gold_credited,10.00,2026-10-02T10:00:01Z\n"
        "c,u,X,confirmation,10.00,2026-10-02T10:00:02Z\n",
        encoding="utf-8",
    )
    code, report = run(log, tmp_path / "report.json")
    assert code == 0 and report["anomalies"] == []
