import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SSAT = ROOT/"examples"/"notifier.ssat.yaml"

@pytest.fixture()
def proj(tmp_path):
    (tmp_path/".forge").mkdir()
    (tmp_path/"skills").mkdir()
    import shutil
    dst = tmp_path/"notifier.ssat.yaml"
    shutil.copy(SSAT, dst)
    return tmp_path

def fill_good(root):
    """Write correct, SSAT-conformant, documented, genuinely test-backed impls
    (meets the stricter QA bar: docstrings + tests that call the functions)."""
    f = root/"slices"/"notifier"/"formatter.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        '"""formatter"""\n\n'
        'def format_message(event: dict) -> str:\n'
        '    """Render an event dict into a human-readable notification string."""\n'
        '    return f"{event.get(\'kind\',\'event\')}: {event.get(\'text\',\'\')}"\n')
    s = root/"slices"/"notifier"/"sender.py"
    s.write_text(
        '"""sender"""\n'
        'from slices.notifier.formatter import format_message\n\n'
        'def send(event: dict, channel: str) -> bool:\n'
        '    """Format then dispatch to a channel. Returns success."""\n'
        '    msg = format_message(event)\n'
        '    return bool(msg and channel)\n')
    t = root/"tests"/"test_notifier.py"
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text(
        'from slices.notifier.formatter import format_message\n'
        'from slices.notifier.sender import send\n\n'
        'def test_format_message():\n'
        '    assert format_message({"kind":"ping","text":"hi"}) == "ping: hi"\n\n'
        'def test_send():\n'
        '    assert send({"kind":"ping","text":"hi"}, "email") is True\n')

def write_smoke_manifest(root, feature="notifier", passing=True):
    """Write a runtime smoke manifest for tests. When passing=True the check
    imports and calls the built code; when False it asserts a wrong result so
    the gate fails."""
    import json
    smoke = root/"smoke"; smoke.mkdir(exist_ok=True)
    if passing:
        snippet = (
            "import sys; sys.path.insert(0, '.')\n"
            "from slices.notifier.formatter import format_message\n"
            "assert format_message({'kind':'ping','text':'hi'}) == 'ping: hi'\n"
            "print('SMOKE_OK')\n"
        )
        expect_stdout = "SMOKE_OK"
    else:
        snippet = (
            "import sys; sys.path.insert(0, '.')\n"
            "from slices.notifier.formatter import format_message\n"
            "assert format_message({'kind':'ping','text':'hi'}) == 'WRONG'\n"
        )
        expect_stdout = None
    manifest = {"checks": [{
        "name": "formatter_runtime",
        "kind": "python",
        "run": snippet,
        "expect_exit": 0,
        **({"expect_stdout": expect_stdout} if expect_stdout else {}),
        "timeout_s": 15,
    }]}
    (smoke/f"{feature}.json").write_text(json.dumps(manifest))
    return smoke/f"{feature}.json"
