"""Wiring added when the MESA, EEG-calibration and DREAMT work was merged (2026-09-25)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_watchdog_runs_mesa_after_dreamt_and_never_both_at_once():
    ps = (ROOT / "scripts" / "windows-watchdog.ps1").read_text(encoding="utf-8")
    assert "function Ensure-MesaModel {" in ps
    loop = ps[ps.index("    Ensure-DreamtModel\n"):]
    assert loop.index("Ensure-DreamtModel") < loop.index("Ensure-MesaModel")
    dreamt = ps[ps.index("function Ensure-DreamtModel {"):]
    dreamt = dreamt[:dreamt.index("\n}\n")]
    assert "*mesa_pipeline.py*" in dreamt
    assert all(ord(ch) < 128 for ch in ps)


def test_dataset_caches_are_git_ignored():
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert "cache/" in lines


def test_mesa_status_is_published_as_known_fields_only(tmp_path):
    sys.path.insert(0, str(ROOT / "dashboard" / "api"))
    from app.health_snapshot import _mesa_block
    (tmp_path / "mesa.status.json").write_text(json.dumps(
        {"stage": "reducing", "reduced": 12, "of": 200, "url": "https://x/?t=SECRET",
         "token": "SECRET"}))
    blk = _mesa_block(str(tmp_path))
    assert blk == {"stage": "reducing", "reduced": 12, "of": 200}
    assert _mesa_block(str(tmp_path / "missing")) is None


def test_calibrated_wake_threshold_decides_both_ways():
    from sleepctl.ml.sleep_staging.infer import SleepStager
    st = SleepStager.load()
    assert st is not None and st.wake_threshold is None
    st.set_wake_threshold(0.3)
    assert st.wake_threshold == 0.3
    st.set_wake_threshold(5)
    assert st.wake_threshold == 0.9
    st.set_wake_threshold(None)
    assert st.wake_threshold is None


def test_dreamt_block_says_whether_it_was_ever_launched(tmp_path):
    sys.path.insert(0, str(ROOT / "dashboard" / "api"))
    from app.health_snapshot import _dreamt_block
    blk = _dreamt_block(str(tmp_path))
    assert blk == {"stage": "no_status", "last_launch": None, "deps_ok_at": None,
                   "installed": False}
    (tmp_path / "dreamt.lastrun").write_text("x")
    assert _dreamt_block(str(tmp_path))["last_launch"] is not None


def test_snapshot_reads_pipeline_status_without_an_explicit_run_dir(tmp_path, monkeypatch):
    """publish-health.ps1 passes no run_dir; the dreamt/mesa blocks were always null because
    they read the raw None instead of resolving the box's .run folder."""
    sys.path.insert(0, str(ROOT / "dashboard" / "api"))
    import app.diagnostics as diag
    from app.health_snapshot import _dreamt_block, _resolve_run_dir
    monkeypatch.setattr(diag, "_default_run_dir", lambda: str(tmp_path))
    (tmp_path / "dreamt.status.json").write_text(json.dumps({"stage": "reducing", "reduced": 3}))
    assert _resolve_run_dir(None) == str(tmp_path)
    assert _dreamt_block(_resolve_run_dir(None)) == {"stage": "reducing", "reduced": 3}
