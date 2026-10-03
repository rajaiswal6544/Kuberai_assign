"""Every skip in the suite must carry a reason. Checked statically here, and at run time by the
pytest_runtest_makereport hook in conftest.py (which turns a reasonless skip into a failure)."""

import ast
import sys
from pathlib import Path

TESTS = Path(__file__).parent


def _call_name(node: ast.Call) -> str:
    parts = []
    target = node.func
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _has_reason(node: ast.Call, positional_index: int) -> bool:
    for kw in node.keywords:
        if kw.arg == "reason":
            return not (isinstance(kw.value, ast.Constant) and not kw.value.value)
    if len(node.args) > positional_index:
        arg = node.args[positional_index]
        return not (isinstance(arg, ast.Constant) and not arg.value)
    return False


def find_reasonless_skips() -> list[str]:
    problems = []
    for path in sorted(TESTS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            # pytest.skip("why") / pytest.mark.skip(reason=...) / pytest.mark.skipif(cond, reason=...)
            if name.endswith("pytest.skip") or name == "skip":
                ok = _has_reason(node, 0)
            elif name.endswith("mark.skip"):
                ok = _has_reason(node, 0)
            elif name.endswith("mark.skipif"):
                ok = _has_reason(node, 1)
            else:
                continue
            if not ok:
                problems.append((path.name, node.lineno, f"{path.name}:{node.lineno} {name} has no reason"))
    return [text for _f, _l, text in sorted(problems)]


def test_every_skip_has_a_reason():
    assert find_reasonless_skips() == []


def test_detector_catches_a_reasonless_skip(tmp_path, monkeypatch):
    bad = tmp_path / "test_bad.py"
    bad.write_text(
        "import pytest\n"
        "def test_a():\n    pytest.skip()\n"
        "@pytest.mark.skipif(True)\ndef test_b(): pass\n"
        "@pytest.mark.skip(reason='documented')\ndef test_c(): pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "TESTS", tmp_path)
    assert [p.split(" ")[0] for p in find_reasonless_skips()] == ["test_bad.py:3", "test_bad.py:4"]
