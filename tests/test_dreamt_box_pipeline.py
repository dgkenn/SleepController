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


class _RangeServer:
    """A fake PhysioNet for Fetcher.get: serves one ZIP with Range support and can drop the
    connection once, part-way through a read, to exercise reconnect-at-offset."""

    def __init__(self, blob: bytes, drop_after: int = 0):
        self.blob, self.drop_after, self.requests = blob, drop_after, 0

    def get(self, url, stream=False, headers=None, **k):
        self.requests += 1
        rng = (headers or {}).get("Range", "")
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        end = len(self.blob) - 1
        if rng.endswith("-0") and rng.startswith("bytes=0"):
            end = 0
        server = self

        class _Raw:
            def __init__(self):
                self.pos = start
                self.sent = 0

            def read(self, n, decode_content=True):
                if server.drop_after and self.sent >= server.drop_after:
                    server.drop_after = 0            # only once
                    raise ConnectionResetError("dropped")
                n = min(n, 16_384)                   # a socket hands back partial reads
                chunk = server.blob[self.pos:min(self.pos + n, end + 1)]
                self.pos += len(chunk)
                self.sent += len(chunk)
                return chunk

        class _R:
            status_code = 206
            url = "https://physionet.org/static/dreamt-2.2.0.zip"
            raw = _Raw()
            headers = {"Content-Range": f"bytes {start}-{end}/{len(server.blob)}"}

            def close(self):
                pass

        return _R()


def _synthetic_zip(tmp_path, n=3, minutes=4):
    src = tmp_path / "src"
    src.mkdir()
    z = tmp_path / "dreamt.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for k in range(n):
            f = src / f"S{k:03d}_whole_df.csv"
            write_synthetic_night(f, minutes=minutes, seed=k)
            zf.write(f, f"dreamt/2.2.0/data_64Hz/S{k:03d}_whole_df.csv")
        zf.writestr("dreamt/2.2.0/participant_info.csv", "SID\n")
        zf.writestr("dreamt/2.2.0/data_100Hz/S000_PSG_df.csv", "x\n" * 1000)
    return src, z


def test_the_remote_zip_is_read_member_by_member_over_range_requests(tmp_path, monkeypatch):
    """Only data_64Hz members are read, straight off the server with no raw copy on disk,
    a dropped connection resumes at the same byte, and the output equals a local reduce."""
    import dreamt_reduce as R
    src, z = _synthetic_zip(tmp_path)
    server = _RangeServer(z.read_bytes(), drop_after=50_000)
    assert P.resolve_zip(server) == ("https://physionet.org/static/dreamt-2.2.0.zip",
                                     z.stat().st_size)
    with P.open_remote_zip(server, "u", z.stat().st_size) as zf:
        names = P.zip_members(zf)
    assert [os.path.basename(n) for n in names] == [f"S{k:03d}_whole_df.csv" for k in range(3)]

    monkeypatch.setattr(P, "Fetcher", lambda netrc: server)
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    remote_out, local_out = tmp_path / "remote", tmp_path / "local"
    got = []
    P._run_jobs([(n, None) for n in names], 1,
                ("remote_zip", ("n", "u", z.stat().st_size), str(remote_out), str(tmp_path / "raw")),
                lambda pid, err, nb: got.append((pid, err, nb)))
    assert [(p, e) for p, e, _ in got] == [("S000", None), ("S001", None), ("S002", None)]
    assert all(nb > 0 for _p, _e, nb in got) and server.drop_after == 0   # the drop happened
    assert not (tmp_path / "raw").exists()                                   # nothing raw on disk
    for k in range(3):
        R.reduce_file(str(src / f"S{k:03d}_whole_df.csv"), str(local_out), verbose=False)
        for name in (f"S{k:03d}_heartrate.txt", f"S{k:03d}_labeled_sleep.txt",
                     f"S{k:03d}_ibi.txt", f"activity/S{k:03d}_activity.txt"):
            assert (remote_out / name).read_text() == (local_out / name).read_text()


def test_a_failed_participant_is_not_left_looking_done(tmp_path):
    P._init_worker("local", None, str(tmp_path / "out"), str(tmp_path / "raw"))
    bad = tmp_path / "S077_whole_df.csv"
    bad.write_text("nonsense,columns\n1,2\n")
    pid, err, _ = P._reduce_one(str(bad))
    assert pid == "S077" and err and not (tmp_path / "out" / "S077_ibi.txt").exists()


def test_parallel_workers_reduce_the_same_as_one(tmp_path):
    import dreamt_reduce as R
    _src, z = _synthetic_zip(tmp_path, n=4, minutes=3)
    members = R.discover(str(z))
    outs = {}
    for w in (1, 3):
        out = tmp_path / f"w{w}"
        seen = []
        P._run_jobs([(m, None) for m in members], w, ("local", None, str(out), str(tmp_path)),
                    lambda pid, err, nb: seen.append((pid, err)))
        assert sorted(seen) == [(f"S{k:03d}", None) for k in range(4)]
        outs[w] = {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.txt")}
    assert outs[1] == outs[3] and len(outs[1]) == 16


def test_the_install_bar_includes_the_bidsleep_retrain():
    """The shipped HR / HR+motion weights are the BIDSleep + sleep-accel retrain (held-out
    HR+motion kappa 0.471): a DREAMT HRV model must beat THAT, not the older 0.44 report."""
    assert P._better_than_bundled({"hrv": {"sm": {"kappa4": 0.46}}})["install"] is False
    v = P._better_than_bundled({"hrv": {"sm": {"kappa4": 0.49}}})
    assert v["install"] is True and v["bundled_kappa4"] >= 0.47
