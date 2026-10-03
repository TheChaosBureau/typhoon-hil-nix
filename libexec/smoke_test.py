r"""
Typhoon HIL smoke test / hello world.

Goal: figure out exactly *which layer* of the Typhoon toolchain is
reachable on the current machine, without requiring HIL hardware or a
specific scenario .tse file. Each layer prints its own pass/fail.

Layers (each runs even if a previous one fails, so the bottom of the
output is a complete diagnostic):

  L0   typhoon-hil-api package import + version
  L1   API handles ready (no connection required yet)
  L2   Schematic API connects to a running Typhoon HIL Control Center
  L2b  HIL API ping (get_sw_version) -- separate Control Center service
  L3   An example .tse model loads in the schematic editor
  L4   Device Manager enumerates HIL devices on the LAN
  L5   End-to-end mini-sim on the Virtual HIL device: compile + load
       + start + read a SCADA probe + stop. Uses the CC-bundled
       `10_basic_model/model.tse` (a unity loopback with a SCADA Input
       and a Probe1) copied into a scratch directory so compile
       outputs land somewhere writable.

Each network-touching layer (L2, L2b, L3, L4, L5) is wrapped with a
per-call timeout (default 90 s, override via TYPHOON_SMOKE_TIMEOUT env
var) so the script reports a clean FAIL instead of hanging when
Control Center is down.

Probe or gate: by default this is a diagnostic and exits 0 whatever the
layers say, because the value of a local run is the full picture. Set
TYPHOON_SMOKE_REQUIRE (e.g. ``TYPHOON_SMOKE_REQUIRE=L5``) to make it a
gate that exits non-zero unless the named layers passed -- CI does, so
that a failed Virtual HIL device cannot report a green check.

Environment:

    TYPHOON_SMOKE_REQUIRE  comma-separated layers that must pass (gate mode)
    TYPHOON_SMOKE_TIMEOUT  per-call timeout in seconds (default 90)
    TYPHOON_SMOKE_DIR      scratch directory for L5 (default: a fresh temp dir)
    TYPHOON_SMOKE_TSE      model to load at L3 (default: the CC-bundled one)

Inside a container started by `typhoon-hil` it is at $TYPHOON_HIL_SMOKE:

    typhoon-hil smoke
    typhoon-hil ci -- python3 /opt/typhoon-hil/smoke_test.py
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path


def _scratch_dir() -> Path:
    """Where L5 compiles. This script is usually mounted read-only, so the
    scratch directory cannot sit beside it."""
    override = os.environ.get("TYPHOON_SMOKE_DIR")
    if override:
        return Path(override)
    return Path(tempfile.mkdtemp(prefix="typhoon-smoke-"))


def _resolve_example_tse() -> Path:
    """Return the .tse L3 loads: TYPHOON_SMOKE_TSE if set, else the CC-bundled
    basic loopback model, which every install has.
    """
    override = os.environ.get("TYPHOON_SMOKE_TSE")
    if override:
        return Path(override)
    cc_basic = _resolve_cc_basic_model()
    return cc_basic if cc_basic is not None else Path("10_basic_model/model.tse")


def _resolve_cc_basic_model() -> Path | None:
    """The 10_basic_model/model.tse shipped under any installed CC version.
    Picks the highest-versioned install if multiple are present.
    """
    matches = sorted(Path("/opt/typhoon").glob(
        "typhoon_hil_control_center_*/examples/tests/10_basic_model/model.tse"
    ))
    return matches[-1] if matches else None


EXAMPLE_TSE = _resolve_example_tse()

# Default 90s — Control Center's first launch can take a minute or more,
# especially in WSL or after a fresh boot. Subsequent calls (CC already
# running) finish in well under a second. Override with TYPHOON_SMOKE_TIMEOUT.
DEFAULT_TIMEOUT = float(os.environ.get("TYPHOON_SMOKE_TIMEOUT", "90"))


PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"


def report(layer, status, msg):
    print(f"[{status:4s}] {layer:4s}  {msg}", flush=True)


def short_exc(exc=None):
    if exc is not None:
        return f"{type(exc).__name__}: {exc}"
    et, ev, _ = sys.exc_info()
    return f"{et.__name__}: {ev}"


class _Timeout(Exception):
    pass


def _call_with_timeout(fn, args=(), kwargs=None, timeout=DEFAULT_TIMEOUT):
    """Run fn in a daemon thread; raise _Timeout if it doesn't finish.

    The thread is left running (daemon=True) on timeout because there
    is no safe way to interrupt a blocking socket recv across all
    platforms; the process exits soon after.
    """
    kwargs = kwargs or {}
    box = {}

    def target():
        try:
            box["ok"] = fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise _Timeout(f"call did not return within {timeout}s")
    if "err" in box:
        raise box["err"]
    return box.get("ok")


def layer_env():
    report("ENV", INFO, f"python   = {sys.executable}")
    report("ENV", INFO, f"version  = {sys.version.split()[0]}")
    report("ENV", INFO, f"system   = {platform.system()} {platform.release()}")
    report("ENV", INFO, f"hostname = {socket.gethostname()}")
    report("ENV", INFO, f"cwd      = {os.getcwd()}")
    report("ENV", INFO, f"timeout  = {DEFAULT_TIMEOUT}s per layer")


def layer_l0_import():
    try:
        import typhoon  # noqa: F401
        import typhoon.api  # noqa: F401
        try:
            from importlib.metadata import version as _v
            v = _v("Typhoon-HIL-API")
        except Exception:
            v = "?"
        report("L0", PASS, f"typhoon-hil-api importable, dist version = {v}")
        return True
    except Exception:
        report("L0", FAIL, short_exc())
        return False


def layer_l1_handles():
    try:
        from typhoon.api.schematic_editor import SchematicAPI
        from typhoon.api import hil as hil_mod
        from typhoon.api.device_manager import DeviceManagerAPI
        sch = SchematicAPI()
        dm = DeviceManagerAPI()
        if not callable(getattr(hil_mod, "get_sw_version", None)):
            raise RuntimeError("typhoon.api.hil.get_sw_version missing")
        report("L1", PASS,
               "handles ready: SchematicAPI(), DeviceManagerAPI(), hil module")
        return sch, hil_mod, dm
    except Exception:
        report("L1", FAIL, short_exc())
        return None


def layer_l2_schematic(sch):
    if sch is None:
        report("L2", SKIP, "no SchematicAPI handle")
        return False
    try:
        # create_new_model() returns None on success in API >= 1.31; success is
        # signalled by the absence of an exception, not the return value.
        _call_with_timeout(sch.create_new_model)
        report("L2", PASS, "schematic_api.create_new_model() ok")
        return True
    except _Timeout as e:
        report("L2", FAIL,
               f"timeout: {e} (Control Center not announcing on localhost?)")
        return False
    except Exception:
        report("L2", FAIL,
               f"schematic call failed (Control Center down?): {short_exc()}")
        return False


def layer_l2b_hil(hil_mod):
    if hil_mod is None:
        report("L2b", SKIP, "no hil module handle")
        return False
    try:
        v = _call_with_timeout(hil_mod.get_sw_version)
        report("L2b", PASS, f"hil.get_sw_version() -> {v}")
        return True
    except _Timeout as e:
        report("L2b", FAIL, f"timeout: {e} (HIL service not responding)")
        return False
    except Exception:
        report("L2b", FAIL,
               f"hil call failed (HIL service down?): {short_exc()}")
        return False


def layer_l3_load(sch):
    if sch is None:
        report("L3", SKIP, "no SchematicAPI handle")
        return False
    if not EXAMPLE_TSE.exists():
        report("L3", SKIP, f"example .tse not found at {EXAMPLE_TSE}")
        return False
    try:
        ok = _call_with_timeout(sch.load, args=(str(EXAMPLE_TSE),),
                                timeout=DEFAULT_TIMEOUT * 2)
        report("L3", PASS if ok else FAIL,
               f"loaded {EXAMPLE_TSE.name} -> {ok}")
        return bool(ok)
    except _Timeout as e:
        report("L3", FAIL, f"timeout: {e}")
        return False
    except Exception:
        report("L3", FAIL, short_exc())
        return False


def layer_l4_devices(dm):
    if dm is None:
        report("L4", SKIP, "no DeviceManagerAPI handle")
        return False
    try:
        devs = _call_with_timeout(dm.get_available_devices)
        if not devs:
            report("L4", INFO,
                   "no HIL devices on LAN (expected without hardware)")
        else:
            report("L4", PASS, f"{len(devs)} device(s): {devs}")
        return True
    except _Timeout as e:
        report("L4", FAIL, f"timeout: {e}")
        return False
    except Exception:
        report("L4", FAIL, short_exc())
        return False


def layer_l5_vhil_sim(sch, hil_mod):
    """End-to-end mini-sim on the Virtual HIL device.

    Uses the CC-bundled `10_basic_model/model.tse` (unity loopback with
    SCADA `Input` driving probe `Probe1`). Copies it into a scratch
    directory first because the CC install tree is root-owned and
    `sch.compile()` writes outputs next to the .tse.
    """
    if sch is None or hil_mod is None:
        report("L5", SKIP, "no schematic/hil handles")
        return False
    src = _resolve_cc_basic_model()
    if src is None:
        report("L5", SKIP, "CC-bundled 10_basic_model/model.tse not found")
        return False
    try:
        scratch_dir = _scratch_dir()
        scratch_dir.mkdir(parents=True, exist_ok=True)
        scratch = scratch_dir / "smoke_model.tse"
        shutil.copy2(src, scratch)
    except Exception:
        report("L5", FAIL, f"could not copy basic model: {short_exc()}")
        return False

    try:
        _call_with_timeout(sch.load, args=(str(scratch),),
                           timeout=DEFAULT_TIMEOUT * 2)
        _call_with_timeout(sch.compile, timeout=DEFAULT_TIMEOUT * 6)
        cpd = sch.get_compiled_model_file(str(scratch))
        if not os.path.exists(cpd):
            report("L5", FAIL, f"compile reported ok but no .cpd at {cpd}")
            return False
        loaded = _call_with_timeout(
            hil_mod.load_model,
            kwargs={"file": cpd, "vhil_device": True},
            timeout=DEFAULT_TIMEOUT * 4,
        )
        if not loaded:
            report("L5", FAIL, "hil.load_model returned False")
            return False
        if not _call_with_timeout(hil_mod.start_simulation,
                                  timeout=DEFAULT_TIMEOUT * 2):
            report("L5", FAIL, "hil.start_simulation returned False")
            return False
        try:
            hil_mod.set_scada_input_value(scadaInputName="Input", value=10)
            hil_mod.wait_msec(200)
            value = hil_mod.read_analog_signal(name="Probe1")
            ok = abs(float(value) - 10.0) < 0.5
            report("L5", PASS if ok else FAIL,
                   f"vhil sim: Input=10 -> Probe1={value}")
            return ok
        finally:
            try:
                _call_with_timeout(hil_mod.stop_simulation,
                                   timeout=DEFAULT_TIMEOUT)
            except Exception:
                pass
    except _Timeout as e:
        report("L5", FAIL, f"timeout: {e}")
        return False
    except Exception:
        report("L5", FAIL, short_exc())
        return False


def required_layers():
    """Layers that must PASS for this run to be a gate rather than a probe.

    Empty by default: run interactively the script
    is a *diagnostic* and its value is the full layer-by-layer picture, so
    it reports and exits 0 even when Control Center is down.

    Set ``TYPHOON_SMOKE_REQUIRE=L5`` (or a comma/space separated list) to
    turn it into a gate. CI does exactly that: a container smoke that
    reports "L5 FAIL" and exits 0 is a green check over a broken image,
    which is worse than no check at all.
    """
    raw = os.environ.get("TYPHOON_SMOKE_REQUIRE", "")
    return [name for name in raw.replace(",", " ").split() if name]


def main():
    print("=" * 72, flush=True)
    print("Typhoon HIL smoke test", flush=True)
    print("=" * 72, flush=True)
    layer_env()
    print("-" * 72, flush=True)

    required = required_layers()
    results = {}

    results["L0"] = layer_l0_import()
    if not results["L0"]:
        print("\nCannot proceed: typhoon-hil-api not importable. "
              "Use the project venv.", flush=True)
        return 2

    handles = layer_l1_handles()
    results["L1"] = handles is not None
    sch = handles[0] if handles else None
    hil_mod = handles[1] if handles else None
    dm = handles[2] if handles else None

    results["L2"] = layer_l2_schematic(sch)
    results["L2b"] = layer_l2b_hil(hil_mod)
    results["L3"] = layer_l3_load(sch)
    results["L4"] = layer_l4_devices(dm)
    results["L5"] = layer_l5_vhil_sim(sch, hil_mod)

    print("-" * 72, flush=True)
    print("L0/L1 PASS         -> Python bindings are good.", flush=True)
    print("L2/L2b/L3 PASS     -> Typhoon HIL Control Center is up.", flush=True)
    print("L4 with devices    -> real HIL hardware is reachable.", flush=True)
    print("L5 PASS            -> Virtual HIL device runs a real sim.", flush=True)

    exit_code = 0
    if required:
        unknown = [name for name in required if name not in results]
        missing = [name for name in required
                   if name in results and not results[name]]
        print("-" * 72, flush=True)
        print(f"TYPHOON_SMOKE_REQUIRE={' '.join(required)}", flush=True)
        if unknown:
            print(f"[{FAIL}] gate  unknown layer(s): {' '.join(unknown)} "
                  f"(known: {' '.join(results)})", flush=True)
            exit_code = 2
        if missing:
            print(f"[{FAIL}] gate  required layer(s) did not pass: "
                  f"{' '.join(missing)}", flush=True)
            exit_code = exit_code or 1
        if not unknown and not missing:
            print(f"[{PASS}] gate  every required layer passed", flush=True)

    # os._exit short-circuits the daemon-thread cleanup, in case a
    # blocked timeout-call thread is still parked in a socket recv.
    sys.stdout.flush()
    os._exit(exit_code)


if __name__ == "__main__":
    # main() exits through os._exit() on every path that reached the
    # layers; this catches the early "cannot proceed" return, whose
    # status used to be discarded -- the script announced that it
    # could not run and then exited 0.
    sys.exit(main())
