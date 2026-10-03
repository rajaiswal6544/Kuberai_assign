import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "trace.py"
spec = importlib.util.spec_from_file_location("trace_script", SCRIPT)
trace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trace)


def write_log(path: Path) -> None:
    lines = [
        {"ts": "2026-10-02T06:30:02Z", "level": "info", "event": "purchase_executed", "user_id": "u1", "request_id": "r2", "grams": "0.0320"},
        {"ts": "2026-10-02T06:30:01Z", "level": "info", "event": "quote_created", "user_id": "u1", "request_id": "r1"},
        {"ts": "2026-10-02T06:30:01Z", "level": "info", "event": "quote_created", "user_id": "u2", "request_id": "r9"},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")


def test_trace_with_user_flag_is_time_ordered_and_filtered(tmp_path, capsys):
    log = tmp_path / "app.jsonl"
    write_log(log)
    assert trace.main(["trace.py", "--user", "u1", "--file", str(log)]) == 0
    out = capsys.readouterr().out
    assert out.index("quote_created") < out.index("purchase_executed")
    assert "grams=0.0320" in out
    assert "r9" not in out


def test_positional_user_still_works(tmp_path, capsys):
    log = tmp_path / "app.jsonl"
    write_log(log)
    assert trace.main(["trace.py", "u2", "--file", str(log)]) == 0
    assert "Timeline for user u2" in capsys.readouterr().out


def test_missing_user_or_unknown_user(tmp_path, capsys):
    log = tmp_path / "app.jsonl"
    write_log(log)
    assert trace.main(["trace.py", "--file", str(log)]) == 2
    assert trace.main(["trace.py", "--user", "nobody", "--file", str(log)]) == 1
