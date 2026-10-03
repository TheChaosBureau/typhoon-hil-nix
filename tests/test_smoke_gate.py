"""
The smoke test's exit contract, exercised against a stand-in Typhoon API.

By default the smoke test is a diagnostic that reports every layer and exits 0.
A CI job needs a gate, so TYPHOON_SMOKE_REQUIRE turns it into one. These tests
pin the difference, on a machine with no Typhoon install.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SMOKE = Path(__file__).resolve().parent.parent / "libexec" / "smoke_test.py"


def test_smoke_test_honours_the_gate():
    """The knob CI relies on has to exist."""
    text = SMOKE.read_text()
    assert "TYPHOON_SMOKE_REQUIRE" in text
    assert "def required_layers" in text


# A Typhoon API stand-in: enough for L0-L4 to pass and L5 to fail, so the
# gate can be exercised on a machine with no Typhoon install. It asserts
# nothing about Typhoon -- only about smoke_test.py's own exit contract.
_STUB_MODULES = {
    "typhoon/__init__.py": "",
    "typhoon/api/__init__.py": (
        "class _Hil:\n"
        "    def get_sw_version(self):\n"
        "        return 'stub'\n"
        "hil = _Hil()\n"
    ),
    "typhoon/api/schematic_editor.py": (
        "class const:\n"
        "    ITEM_TERMINAL = 'terminal'\n"
        "class SchematicAPI:\n"
        "    def raise_exceptions(self, *a):\n"
        "        pass\n"
        "    def create_new_model(self):\n"
        "        return None\n"
        "    def load(self, filename=None, **kw):\n"
        "        return True\n"
    ),
    "typhoon/api/device_manager.py": (
        "class DeviceManagerAPI:\n"
        "    def get_available_devices(self):\n"
        "        return []\n"
    ),
}


def _stub_api(tmp_path: Path) -> str:
    root = tmp_path / "stub-api"
    for relative, body in _STUB_MODULES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return str(root)


def _run_smoke(pythonpath: str | None, require: str | None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TYPHOON_SMOKE_TIMEOUT": "5"}
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    if require is not None:
        env["TYPHOON_SMOKE_REQUIRE"] = require
    return subprocess.run(
        [sys.executable, str(SMOKE)],
        env=env, capture_output=True, text=True, timeout=180,
    )


def test_smoke_is_a_probe_by_default(tmp_path):
    """Without the knob it is a diagnostic: report everything, exit 0.

    A developer debugging a dead Control Center wants the full layer-by-layer picture rather than an early abort.
    """
    result = _run_smoke(_stub_api(tmp_path), None)
    # L5 cannot pass against the stub -- it FAILs where a Control Center
    # install exists and SKIPs where one does not; either way the probe
    # still exits 0, which is the behaviour being pinned here.
    assert "[PASS] L5" not in result.stdout, result.stdout
    assert result.returncode == 0, result.stdout + result.stderr


def test_smoke_gate_fails_on_a_required_layer(tmp_path):
    """With TYPHOON_SMOKE_REQUIRE=L5 the same failing run must exit non-zero.

    This is the difference between a CI check and a CI decoration.
    """
    result = _run_smoke(_stub_api(tmp_path), "L5")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "required layer(s) did not pass: L5" in result.stdout


def test_smoke_gate_passes_when_required_layers_pass(tmp_path):
    result = _run_smoke(_stub_api(tmp_path), "L0,L1,L2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "every required layer passed" in result.stdout


def test_smoke_gate_rejects_an_unknown_layer(tmp_path):
    """A typo in the knob must not silently gate on nothing."""
    result = _run_smoke(_stub_api(tmp_path), "L5,L9")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "unknown layer(s): L9" in result.stdout


def test_smoke_reports_an_unusable_api_as_failure():
    """"Cannot proceed" must not exit 0.

    The early return existed but `__main__` discarded it, so a run with no
    Typhoon API printed "Cannot proceed" and reported success.
    """
    result = _run_smoke(None, None)
    assert "Cannot proceed" in result.stdout
    assert result.returncode == 2, result.stdout + result.stderr
