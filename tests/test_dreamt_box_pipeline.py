"""The on-box DREAMT pipeline: never handles credentials, stops cleanly without access, streams
one participant at a time, and installs weights only when they beat the bundled model."""
import json
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import dreamt_pipeline as P          # noqa: E402
from test_dreamt_pipeline import write_synthetic_night  # noqa: E402


@pytest.fixture()
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "RUN_DIR", str(tmp_path / ".run"))
    monkeypatch.setattr(P, "STATUS_PATH", str(tmp_path / ".run" / "dreamt.status.json"))
    monkeypatch.setattr(P, "INSTALL_DIR", str(tmp_path / ".run" / "staging_weights"))
    monkeypatch.setattr(P, "find_local_zip", lambda: None)
    return tmp_path


def _status(run_dir):
    return json.loads((run_dir / ".run" / "dreamt.status.json").read_text())


def test_without_a_physionet_login_it_stops_and_says_so(run_dir, monkeypatch):
    monkeypatch.setattr(P, "netrc_path", lambda: None)
    assert P.main(["--work", str(run_dir / "work")]) == 2
    st = _status(run_dir)
    assert st["stage"] == "blocked" and "netrc" in st["error"]


def test_a_403_is_reported_as_access_not_live(run_dir, monkeypatch):
    class _R:
        status_code = 403

    class _F:
        def __init__(self, netrc):
            pass

        def get(self, url, **k):
            return _R()

    monkeypatch.setattr(P, "netrc_path", lambda: "/nonexistent/.netrc")
    monkeypatch.setattr(P, "Fetcher", _F)
    assert P.main(["--work", str(run_dir / "work")]) == 3
    st = _status(run_dir)
    assert st["stage"] == "blocked" and "403" in st["error"]


def test_the_status_never_carries_the_netrc_path_or_contents(run_dir, monkeypatch, tmp_path):
    netrc = tmp_path / ".netrc"
    netrc.write_text("machine physionet.org login someone password s3cret\\n")
    monkeypatch.setenv("NETRC", str(netrc))
    assert P.netrc_path() == str(netrc)
    monkeypatch.setattr(P, "Fetcher", lambda n: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError):
        P.main(["--work", str(run_dir / "work")])
    text = (run_dir / ".run" / "dreamt.status.json").read_text()
    assert "s3cret" not in text and ".netrc" not in text


def test_a_local_zip_is_reduced_then_trained_and_only_a_better_model_is_installed(
        run_dir, monkeypatch, tmp_path):
    raw = tmp_path / "zipsrc"
    raw.mkdir()
    z = tmp_path / "dreamt.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for k in range(10):
            f = raw / f"S{k:03d}_whole_df.csv"
            write_synthetic_night(f, minutes=6, seed=k)
            zf.write(f, f"dreamt/data_64Hz/S{k:03d}_whole_df.csv")
    monkeypatch.setattr(P, "find_local_zip", lambda: str(z))
    import train_dreamt as TD

    def fake_train(reduced, out, **k):
        os.makedirs(out, exist_ok=True)
        for fn in P.WEIGHT_FILES:
            open(os.path.join(out, fn), "w").write("{}")
        return {"hrv": {"sm": {"kappa4": 0.62, "wake_kappa": 0.5}}, "participants": 10}

    monkeypatch.setattr(TD, "train", fake_train)
    assert P.main(["--work", str(tmp_path / "work")]) == 0
    st = _status(run_dir)
    assert st["reduced"] == 10 and st["verdict"]["install"] is True
    assert st["stage"] == "installed"
    assert (run_dir / ".run" / "staging_weights" / "stage4_hrv.json").exists()


def test_a_weak_model_is_not_installed():
    v = P._better_than_bundled({"hrv": {"sm": {"kappa4": 0.30}}})
    assert v["install"] is False


def test_the_stager_prefers_an_installed_local_hrv_model(tmp_path, monkeypatch):
    from sleepctl.ml.sleep_staging import infer
    monkeypatch.setattr(infer, "STAGING_WEIGHTS_DIR", str(tmp_path))
    (tmp_path / "stage4_hrv.json").write_text("{}")
    assert infer._hrv_weight_path(infer.WEIGHTS_DIR, "stage4_hrv.json") == \
        os.path.normpath(str(tmp_path / "stage4_hrv.json"))
    assert infer._hrv_weight_path(infer.WEIGHTS_DIR, "wake_hrv.json").startswith(infer.WEIGHTS_DIR)
