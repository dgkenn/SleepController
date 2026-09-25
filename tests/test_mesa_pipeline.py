"""MESA (NSRR) reduction and on-box pipeline, on tiny synthetic EDF / XML fixtures.

The reader, the pleth beat detector and its artifact rejection, the stage mapping and the
reduced formats (the ones dreamt_reduce writes and the dataset loader reads); then the
pipeline: it stops with a clear status without a token, with a rejected token, without an
approved data request or when the layout differs, and the token never reaches the status."""
import hashlib
import json
import os
import re
import sys
from datetime import datetime

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mesa_pipeline as P   # noqa: E402
import mesa_reduce as M     # noqa: E402

TOKEN = "1234-SecretTokenAbCdEf987"


# ------------------------------------------------------------------------------ fixtures
def synth_ppg(minutes=10.0, fs=128, seed=0, hr=62.0, noise=0.03, artifacts=False, ectopic=False):
    """A finger PPG: systolic peak + dicrotic wave per beat, RSA, baseline wander, noise.
    Returns (signal, fs, true beat times, artifact spans)."""
    rng = np.random.default_rng(seed)
    n = int(minutes * 60 * fs)
    beats, t = [], 0.5
    while t < minutes * 60 - 1:
        t += 60.0 / hr + 0.05 * np.sin(2 * np.pi * 0.25 * t) + rng.normal(0, 0.015)
        beats.append(t)
    beats = np.array(beats)
    if ectopic:
        k = len(beats) // 3
        beats[k] -= 0.3 * (beats[k] - beats[k - 1])
    tt = np.arange(n) / fs
    x = np.zeros(n)
    for b in beats:
        i0, i1 = max(int((b - 0.3) * fs), 0), min(int((b + 0.8) * fs), n)
        s = tt[i0:i1] - b
        x[i0:i1] += np.where(s < 0, np.exp(-(s / 0.06) ** 2), np.exp(-(s / 0.12) ** 2)) \
            + 0.35 * np.exp(-((s - 0.32) / 0.07) ** 2)
    x += 0.5 * np.sin(2 * np.pi * 0.08 * tt) + noise * rng.normal(size=n)
    spans = []
    if artifacts:
        a0 = int(200 * fs)
        x[a0:a0 + 20 * fs] = x[a0]                                   # probe off: flat
        spans.append((200.0, 220.0))
        m0 = int(400 * fs)
        x[m0:m0 + 15 * fs] += 6 * rng.normal(size=15 * fs).cumsum() / 40   # hand movement
        spans.append((400.0, 415.0))
    return x, fs, beats, spans


def _true_ibi(beats, t):
    k = int(np.argmin(np.abs(beats - t)))
    return (beats[k] - beats[k - 1]) * 1000.0


STAGE_CONCEPTS = [("Wake|0", 0), ("Stage 1 sleep|1", 1), ("Stage 2 sleep|2", 2),
                  ("Stage 3 sleep|3", 3), ("Stage 4 sleep|4", 3), ("REM sleep|5", 5),
                  ("Unscored|9", -1)]


def nsrr_xml(path, stages, extra_events=True):
    """NSRR XML with runs of stages: ``stages`` is [(concept, n_epochs)]."""
    ev = ['<ScoredEvent><EventType/><EventConcept>Recording Start Time</EventConcept>'
          '<Start>0</Start><Duration>99999.0</Duration><ClockTime>00.00.00 22.00.00</ClockTime>'
          '</ScoredEvent>']
    t = 0.0
    for concept, n in stages:
        ev.append(f'<ScoredEvent><EventType>Stages|Stages</EventType><EventConcept>{concept}'
                  f'</EventConcept><Start>{t:.1f}</Start><Duration>{30.0 * n:.1f}</Duration>'
                  '</ScoredEvent>')
        if extra_events:
            ev.append('<ScoredEvent><EventType>Respiratory|Respiratory</EventType><EventConcept>'
                      f'Hypopnea|Hypopnea</EventConcept><Start>{t + 5:.1f}</Start>'
                      '<Duration>12.0</Duration></ScoredEvent>')
        t += 30.0 * n
    path.write_text('<?xml version="1.0" encoding="UTF-8"?><PSGAnnotation>'
                    '<SoftwareVersion>Compumedics</SoftwareVersion><EpochLength>30</EpochLength>'
                    f'<ScoredEvents>{"".join(ev)}</ScoredEvents></PSGAnnotation>')
    return path


def make_record(tmp, rid="mesa-sleep-0001", minutes=40.0, seed=0, pleth=True):
    """(edf, xml, actigraphy csv, overlap row) for a synthetic MESA record."""
    fs = 128
    x, fs, beats, _ = synth_ppg(minutes=minutes, fs=fs, seed=seed, hr=58 + seed)
    rng = np.random.default_rng(seed)
    n = len(x)
    sigs = [("EKG", 128.0, rng.normal(size=n)), ("Pleth" if pleth else "Thor", 128.0, x),
            ("HR", 1.0, np.full(int(minutes * 60), 60.0))]
    edf = tmp / f"{rid}.edf"
    M.write_edf(str(edf), sigs, record_s=1.0, start=datetime(2011, 3, 4, 22, 0, 0))
    n_ep = int(minutes * 2)
    runs, left = [], n_ep
    for concept, _ in STAGE_CONCEPTS * 10:
        k = min(left, 4)
        if k <= 0:
            break
        runs.append((concept, k))
        left -= k
    xml = nsrr_xml(tmp / f"{rid}-nsrr.xml", runs)
    act = tmp / f"{rid}.csv"
    lines = ["mesaid,line,linetime,offwrist,activity,marker"]
    for i in range(1, 200):
        lines.append(f"{int(rid[-4:])},{i},21:{(i // 2) % 60:02d}:{30 * (i % 2):02d},"
                     f"{1 if i == 60 else 0},{(i * 7) % 40},0")
    act.write_text("\n".join(lines) + "\n")
    overlap = {"mesaid": str(int(rid[-4:])), "line": "40", "linetime": "22:00:00",
               "starttime_psg": "22:00:00"}
    return edf, xml, act, overlap, beats


# ----------------------------------------------------------------------------- EDF reader
def test_edf_reader_round_trips_header_and_samples(tmp_path):
    a = np.sin(np.arange(1000) / 10.0) * 100
    b = np.arange(250, dtype=float)
    p = tmp_path / "x.edf"
    M.write_edf(str(p), [("Pleth", 100.0, a), ("HR", 25.0, b)], record_s=2.0,
                start=datetime(2012, 5, 6, 23, 15, 1))
    with M.EdfReader(str(p)) as e:
        assert e.labels == ["Pleth", "HR"] and e.n_records == 5 and e.duration_s == 10.0
        assert e.fs(0) == 100.0 and e.fs(1) == 25.0 and e.n_samples(0) == 1000
        assert e.start == datetime(2012, 5, 6, 23, 15, 1)
        assert e.find_signal(("pleth",)) == 0 and e.find_signal(("SpO2",)) is None
        full = e.read(0)
        assert np.max(np.abs(full - a)) < 0.01                     # 16-bit quantisation
        part = e.read(0, 150, 420)                                 # across record boundaries
        assert np.allclose(part, full[150:420])
        assert np.allclose(e.read(1), b, atol=0.01)
    # a download cut short reads as far as it goes
    data = p.read_bytes()
    p.write_bytes(data[:-300])                               # 500-byte records
    with M.EdfReader(str(p)) as e:
        assert e.truncated and e.n_records == 4 and len(e.read(0)) == 800


def test_edf_reader_rejects_what_is_not_an_edf(tmp_path):
    p = tmp_path / "bad.edf"
    p.write_bytes(b"<html>login</html>" + b" " * 300)
    with pytest.raises(M.LayoutError):
        M.EdfReader(str(p))
    p.write_bytes(b"0")
    with pytest.raises(M.LayoutError):
        M.EdfReader(str(p))


# --------------------------------------------------------------------------- beat detector
@pytest.mark.parametrize("hr,noise,seed", [(62, 0.03, 0), (45, 0.08, 1), (100, 0.05, 2)])
def test_beats_on_clean_synthetic_ppg(hr, noise, seed):
    x, fs, beats, _ = synth_ppg(minutes=6, fs=256, seed=seed, hr=hr, noise=noise)
    times, amps, ok = M.beats_from_signal(lambda a, b: x[a:b], len(x), fs, chunk_s=60)
    # every true beat is found within 60 ms (chunk seams included) and nothing else is
    j = np.clip(np.searchsorted(times, beats), 1, len(times) - 1)
    miss = np.minimum(np.abs(times[j] - beats), np.abs(times[j - 1] - beats))
    assert np.mean(miss < 0.06) > 0.99
    assert abs(len(times) - len(beats)) <= 2
    ibis = M.clean_ibis(times, amps, ok)
    assert len(ibis) > 0.95 * len(beats)
    err = np.array([abs(v - _true_ibi(beats, t)) for t, v in ibis])
    assert np.median(err) < 5.0 and err.max() < 25.0


def test_artifacts_and_ectopic_beats_are_rejected():
    x, fs, beats, spans = synth_ppg(minutes=10, fs=256, seed=3, hr=72, noise=0.1,
                                    artifacts=True, ectopic=True)
    times, amps, ok = M.beats_from_signal(lambda a, b: x[a:b], len(x), fs)
    ibis = M.clean_ibis(times, amps, ok)
    assert len(ibis) > 0.85 * len(beats)
    for t, v in ibis:
        assert not any(a + 1.5 < t < b - 0.5 for a, b in spans), t      # nothing from inside
        assert abs(v - _true_ibi(beats, t)) < 40.0                        # nothing wrong kept
    k = len(beats) // 3                                                   # the premature beat
    assert not any(abs(t - beats[k]) < 0.1 or abs(t - beats[k + 1]) < 0.1 for t, _ in ibis)


def test_intervals_never_span_a_rejected_beat():
    t = np.arange(0, 60, 1.0)
    ok = np.ones(len(t), dtype=bool)
    ok[30] = False
    out = dict(M.clean_ibis(t, None, ok))
    assert 30.0 not in out and 31.0 not in out and out[29.0] == 1000.0 and out[32.0] == 1000.0
    gap = np.delete(t, 20)                                    # a missed beat: 2000 ms
    assert 21.0 not in dict(M.clean_ibis(gap))


def test_flat_and_railed_windows_are_flagged():
    fs = 100
    x = np.sin(np.arange(1000) * 2 * np.pi * 1.1 / fs)
    x[200:400] = 0.5                                          # flat
    x[600:800] = np.clip(x[600:800] * 5, -1, 1)               # clipped at the rails
    f = M.bad_windows(x, fs, rails=(-1.0, 1.0))
    assert f[1] and f[3] and not f[0] and not f[4]


def test_heart_rate_from_intervals():
    ibis = [(float(t), 1000.0) for t in range(1, 40)]
    hr = M.hr_from_ibis(ibis)
    assert hr and all(abs(v - 60.0) < 1e-6 for _t, v in hr)
    assert hr[0][0] == 2.0 and hr[-1][0] == 39.0


# ---------------------------------------------------------------------------- annotations
def test_nsrr_xml_stages_map_to_the_reduced_codes(tmp_path):
    p = nsrr_xml(tmp_path / "a-nsrr.xml", [(c, 2) for c, _ in STAGE_CONCEPTS])
    labels = M.parse_stages(str(p))
    want = [code for _c, code in STAGE_CONCEPTS for _ in range(2)]
    assert [labels[k] for k in sorted(labels)] == want and sorted(labels) == list(range(14))


def test_profusion_xml_is_read_too(tmp_path):
    p = tmp_path / "a-profusion.xml"
    p.write_text("<CMPStudyConfig><EpochLength>30</EpochLength><SleepStages>"
                 + "".join(f"<SleepStage>{v}</SleepStage>" for v in (0, 1, 2, 3, 4, 5, 9))
                 + "</SleepStages></CMPStudyConfig>")
    assert M.parse_stages(str(p)) == {0: 0, 1: 1, 2: 2, 3: 3, 4: 3, 5: 5, 6: -1}


def test_xml_without_stages_is_a_layout_error(tmp_path):
    p = tmp_path / "x.xml"
    p.write_text("<PSGAnnotation><EpochLength>30</EpochLength><ScoredEvents><ScoredEvent>"
                 "<EventType>Respiratory|Respiratory</EventType><EventConcept>Hypopnea|Hypopnea"
                 "</EventConcept><Start>1</Start><Duration>10</Duration></ScoredEvent>"
                 "</ScoredEvents></PSGAnnotation>")
    with pytest.raises(M.LayoutError):
        M.parse_stages(str(p))
    p.write_text("<html>not xml")
    with pytest.raises(M.LayoutError):
        M.parse_stages(str(p))


# --------------------------------------------------------------------------- output files
def test_a_record_reduces_to_the_dreamt_formats_the_loader_reads(tmp_path):
    from sleepctl.ml.sleep_staging import dataset as D
    edf, xml, act, overlap, beats = make_record(tmp_path)
    out = tmp_path / "reduced"
    summ = M.reduce_record("mesa-sleep-0001", str(xml), str(out), edf_path=str(edf),
                           act_path=str(act), overlap_row=overlap)
    rid = "mesa-sleep-0001"
    hr = (out / f"{rid}_heartrate.txt").read_text().splitlines()
    lab = (out / f"{rid}_labeled_sleep.txt").read_text().splitlines()
    ibi = (out / f"{rid}_ibi.txt").read_text().splitlines()
    acts = (out / "activity" / f"{rid}_activity.txt").read_text().splitlines()
    assert all(re.fullmatch(r"\d+,\d+\.\d\d", ln) for ln in hr)
    assert all(re.fullmatch(r"\d+ (-1|0|1|2|3|5)", ln) for ln in lab) and len(lab) == 80
    assert all(re.fullmatch(r"\d+\.\d{3},\d+\.\d", ln) for ln in ibi)
    assert acts[0].startswith("# epoch_start_s,pim,zcm,mad,std,pmax,n")
    assert all(re.fullmatch(r"-?\d+,[\d.]+,0,0,0,[\d.]+,1", ln) for ln in acts[1:])
    # actigraphy line 40 is PSG t=0; the off-wrist line 60 is dropped
    assert acts[1].startswith("0,") and not any(ln.startswith("600,") for ln in acts[1:])
    assert summ["ibi"] > 0.9 * len(beats) and summ["epochs"] == 80
    # the existing loader reads it unchanged: subject discovered, HRV rows built
    assert D.discover_subjects(str(out)) == [rid] and D.subjects_with_ibi(str(out)) == [rid]
    ds = D.build_subject_rows(rid, str(out), use_ibi=True, require_ibi=True)
    assert len(ds) > 40 and sum(ds.has_ibi) > 20 and sum(ds.has_activity) > 20


def test_a_record_without_a_pleth_channel_is_a_layout_error(tmp_path):
    edf, xml, *_ = make_record(tmp_path, minutes=4, pleth=False)
    with pytest.raises(M.LayoutError):
        M.reduce_record("mesa-sleep-0001", str(xml), str(tmp_path / "o"), edf_path=str(edf))
    assert not (tmp_path / "o" / "mesa-sleep-0001_ibi.txt").exists()


def test_rpoint_files_give_intervals_too(tmp_path):
    p = tmp_path / "r.csv"
    rows = ["epoch,type,seconds"] + [f"1,N,{0.5 + i * 0.9:.3f}" for i in range(400)]
    p.write_text("\n".join(rows))
    ibis = M.rpoint_ibis(str(p))
    assert len(ibis) > 390 and all(abs(v - 900.0) < 0.01 for _t, v in ibis)


# ------------------------------------------------------------------------------- pipeline
@pytest.fixture()
def run_dir(tmp_path, monkeypatch):
    for var in P.TOKEN_ENV + (P.TOKEN_FILE_ENV, "SLEEPCTL_NETRC_HOME", "USERPROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(P, "RUN_DIR", str(tmp_path / ".run"))
    monkeypatch.setattr(P, "STATUS_PATH", str(tmp_path / ".run" / "mesa.status.json"))
    monkeypatch.setattr(P, "DONE_PATH", str(tmp_path / ".run" / "mesa.done"))
    monkeypatch.setattr(P, "DREAMT_STATUS", str(tmp_path / ".run" / "dreamt.status.json"))
    monkeypatch.setattr(P, "INSTALL_DIR", str(tmp_path / ".run" / "staging_weights"))
    monkeypatch.setattr(P, "tls_mode", lambda work: True)
    monkeypatch.setattr(P, "_SECRETS", [])
    return tmp_path


def _status(run_dir):
    return json.loads((run_dir / ".run" / "mesa.status.json").read_text())


def _all_run_text(run_dir):
    return "".join(p.read_text(errors="ignore") for p in (run_dir / ".run").rglob("*") if p.is_file())


class _Resp:
    def __init__(self, status=200, body=b"", ctype="application/octet-stream"):
        self.status_code, self.content = status, body
        self.headers = {"Content-Type": ctype}

    def json(self):
        return json.loads(self.content)

    def iter_content(self, n):
        for i in range(0, len(self.content), max(1, n)):
            yield self.content[i:i + n]

    def close(self):
        pass


class FakeNsrr:
    """sleepdata.org for Fetcher.get: account, file listings, and token-in-URL downloads."""

    def __init__(self, files, token=TOKEN, approved=True, folders=None):
        self.files, self.token, self.approved = files, token, approved
        self.folders = folders
        self.urls = []

    def get(self, url, stream=False, headers=None, **k):
        self.urls.append(url)
        from urllib.parse import parse_qs, urlparse
        u = urlparse(url)
        q = parse_qs(u.query)
        if u.path.endswith("/account/profile.json"):
            ok = q.get("auth_token", [""])[0] == self.token
            return _Resp(200, json.dumps({"authenticated": ok}).encode(), "application/json")
        if u.path.endswith("/files.json"):
            path = q.get("path", [""])[0].strip("/")
            items = []
            dirs = set()
            for full, body in self.files.items():
                parent, _, name = full.rpartition("/")
                if parent == path:
                    items.append({"file_name": name, "full_path": full, "is_file": True,
                                  "file_size": len(body),
                                  "file_checksum_md5": hashlib.md5(body).hexdigest()})
                elif (parent + "/").startswith(path + "/" if path else ""):
                    rest = parent[len(path):].strip("/")
                    dirs.add(rest.split("/")[0])
            items += [{"file_name": d, "full_path": f"{path}/{d}".strip("/"), "is_file": False}
                      for d in sorted(dirs)]
            return _Resp(200, json.dumps(items).encode(), "application/json")
        m = re.match(r"/datasets/mesa/files/a/([^/]+)/m/[^/]+/(.+)$", u.path)
        if m:
            if m.group(1) != self.token or not self.approved:
                return _Resp(200, b"<!DOCTYPE html><html>Sign in</html>", "text/html")
            body = self.files.get(m.group(2))
            if body is None:
                return _Resp(404, b"")
            start = 0
            rng = (headers or {}).get("Range", "")
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                return _Resp(206, body[start:])
            return _Resp(200, body)
        return _Resp(404, b"")


def _serve(monkeypatch, server):
    import dreamt_pipeline as DP

    class F(DP.Fetcher):
        def __init__(self, tls=True):
            pass

        def get(self, url, stream=False, headers=None, tries=1):
            return server.get(url, stream=stream, headers=headers)

    monkeypatch.setattr(P, "NsrrFetcher", F)


def _corpus(tmp_path, n=3, minutes=40.0):
    files = {}
    src = tmp_path / "src"
    src.mkdir()
    for k in range(n):
        rid = f"mesa-sleep-{k + 1:04d}"
        edf, xml, act, _ov, _b = make_record(src, rid=rid, minutes=minutes, seed=k)
        files[f"polysomnography/edfs/{rid}.edf"] = edf.read_bytes()
        files[f"polysomnography/annotations-events-nsrr/{rid}-nsrr.xml"] = xml.read_bytes()
        files[f"actigraphy/{rid}.csv"] = act.read_bytes()
    files["overlap/mesa-actigraphy-psg-overlap.csv"] = (
        "mesaid,line,linetime,starttime_psg\n"
        + "".join(f"{k + 1},40,22:00:00,22:00:00\n" for k in range(n))).encode()
    files["datasets/mesa-sleep-dataset-0.7.0.csv"] = b"mesaid\n1\n"
    return files


def test_without_a_token_it_stops_and_says_where_to_put_one(run_dir):
    assert P.main(["--work", str(run_dir / "work")]) == 2
    st = _status(run_dir)
    assert st["stage"] == "blocked" and "token" in st["error"] and "MESA_TRAINING" in st["error"]


def test_a_rejected_token_is_reported_and_never_written(run_dir, monkeypatch):
    monkeypatch.setenv("SLEEPCTL_NSRR_TOKEN", TOKEN)
    _serve(monkeypatch, FakeNsrr({}, token="someone-elses-token-xyz"))
    assert P.main(["--work", str(run_dir / "work")]) == 3
    st = _status(run_dir)
    assert st["stage"] == "blocked" and "rejected" in st["error"]
    assert TOKEN not in _all_run_text(run_dir)


def test_a_token_without_an_approved_request_is_blocked(run_dir, monkeypatch):
    monkeypatch.setenv("NSRR_TOKEN", TOKEN)
    _serve(monkeypatch, FakeNsrr(_corpus(run_dir, n=1, minutes=4), approved=False))
    assert P.main(["--work", str(run_dir / "work")]) == 3
    st = _status(run_dir)
    assert st["stage"] == "blocked" and "not authorised" in st["error"]
    assert TOKEN not in _all_run_text(run_dir)


def test_a_different_layout_fails_with_a_clear_status(run_dir, monkeypatch):
    monkeypatch.setenv("SLEEPCTL_NSRR_TOKEN", TOKEN)
    files = {"polysomnography/somewhere-else/mesa-sleep-0001.edf": b"x",
             "polysomnography/annotations-events-nsrr/mesa-sleep-0001-nsrr.xml": b"<x/>"}
    _serve(monkeypatch, FakeNsrr(files))
    assert P.main(["--work", str(run_dir / "work")]) == 6
    st = _status(run_dir)
    assert st["stage"] == "failed" and "layout differs" in st["error"] and "edfs" in st["error"]


def test_an_error_quoting_the_download_url_is_scrubbed(run_dir, monkeypatch):
    monkeypatch.setenv("SLEEPCTL_NSRR_TOKEN", TOKEN)
    server = FakeNsrr(_corpus(run_dir, n=1, minutes=4))

    def boom(url, **k):
        if "/datasets/mesa/files/a/" in url:
            raise ConnectionError(f"Max retries exceeded with url: {url}")
        return FakeNsrr.get(server, url, **k)

    server.get = boom
    _serve(monkeypatch, server)
    assert P.main(["--work", str(run_dir / "work")]) == 1
    text = _all_run_text(run_dir)
    assert TOKEN not in text and "/a/***/" in text


def test_token_file_lookup(tmp_path, monkeypatch):
    for var in P.TOKEN_ENV + (P.TOKEN_FILE_ENV,):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(P, "_SECRETS", [])
    assert P.load_token()[0] is None
    (tmp_path / ".nsrr_token").write_text("# from sleepdata.org/token\n  " + TOKEN + "  \n")
    assert P.load_token() == (TOKEN, "file")
    (tmp_path / ".nsrr_token").write_text("not a token at all!\n")
    tok, why = P.load_token()
    assert tok is None and "not a token" not in why
    monkeypatch.setenv("NSRR_TOKEN", TOKEN)
    assert P.load_token() == (TOKEN, "env")
    assert P.scrub(f"https://sleepdata.org/datasets/mesa/files/a/{TOKEN}/m/x/y.edf") == \
        "https://sleepdata.org/datasets/mesa/files/a/***/m/x/y.edf"


def test_the_work_folder_may_not_be_inside_the_repo(run_dir, monkeypatch):
    monkeypatch.setenv("SLEEPCTL_NSRR_TOKEN", TOKEN)
    assert P.main(["--work", os.path.join(P.ROOT, "cache", "mesa")]) == 5
    assert "outside the repository" in _status(run_dir)["error"]
    assert not os.path.exists(os.path.join(P.ROOT, "cache", "mesa"))


def test_end_to_end_streams_reduces_deletes_and_installs_only_if_better(run_dir, monkeypatch, capsys):
    monkeypatch.setenv("SLEEPCTL_NSRR_TOKEN", TOKEN)
    monkeypatch.setattr(P, "MIN_RECORDS", 2)
    server = FakeNsrr(_corpus(run_dir, n=3))
    _serve(monkeypatch, server)
    import train_dreamt as TD
    seen = {}

    def fake_train(reduced, out, **k):
        from sleepctl.ml.sleep_staging.dataset import subjects_with_ibi
        seen["with_ibi"] = subjects_with_ibi(reduced)
        os.makedirs(out, exist_ok=True)
        for fn in P.WEIGHT_FILES:
            open(os.path.join(out, fn), "w").write("{}")
        return {"hrv": {"sm": {"kappa4": 0.66, "wake_kappa": 0.55}}, "participants": 3}

    monkeypatch.setattr(TD, "train", fake_train)
    work = run_dir / "work"
    assert P.main(["--work", str(work), "--workers", "1"]) == 0
    st = _status(run_dir)
    assert st["stage"] == "installed" and st["reduced"] == 3 and st["n_records"] == 3
    assert st["scores"]["hrv"]["kappa4_smoothed"] == 0.66 and st["actigraphy"] is True
    assert seen["with_ibi"] == ["mesa-sleep-0001", "mesa-sleep-0002", "mesa-sleep-0003"]
    assert (run_dir / ".run" / "staging_weights" / "stage4_hrv.json").exists()
    assert (run_dir / ".run" / "mesa.done").read_text().strip() == "installed"
    assert not (work / "raw").exists()                          # every raw file deleted
    assert len((work / "reduced" / "activity" / "mesa-sleep-0002_activity.txt")
               .read_text().splitlines()) > 20
    assert TOKEN not in _all_run_text(run_dir) + capsys.readouterr().out
    # resumable: a second run downloads nothing again
    n = len(server.urls)
    assert P.main(["--work", str(work), "--workers", "1"]) == 0
    assert not [u for u in server.urls[n:] if u.endswith(".edf")]


def test_a_permanent_record_failure_is_skipped_not_retried(run_dir, monkeypatch):
    monkeypatch.setenv("SLEEPCTL_NSRR_TOKEN", TOKEN)
    monkeypatch.setattr(P, "MIN_RECORDS", 50)
    files = _corpus(run_dir, n=2, minutes=4)          # 8 epochs: below MIN_EPOCHS
    server = FakeNsrr(files)
    _serve(monkeypatch, server)
    assert P.main(["--work", str(run_dir / "work"), "--workers", "1", "--no-actigraphy"]) == 4
    st = _status(run_dir)
    assert st["skipped"] == 2 and st["stage"] == "failed"
    n = len(server.urls)
    P.main(["--work", str(run_dir / "work"), "--workers", "1", "--no-actigraphy"])
    assert not [u for u in server.urls[n:] if u.endswith(".edf")]


def test_a_checksum_mismatch_is_retried_and_leaves_no_raw_file(run_dir, monkeypatch):
    monkeypatch.setenv("SLEEPCTL_NSRR_TOKEN", TOKEN)
    monkeypatch.setattr(P, "MIN_RECORDS", 50)
    files = _corpus(run_dir, n=1, minutes=4)
    server = FakeNsrr(files)
    real = server.get

    def corrupt(url, **k):
        r = real(url, **k)
        if url.endswith(".edf") and r.status_code in (200, 206):
            r.content = r.content[:-10] + b"0123456789"
        return r

    server.get = corrupt
    _serve(monkeypatch, server)
    assert P.main(["--work", str(run_dir / "work"), "--workers", "1", "--no-actigraphy"]) == 4
    st = _status(run_dir)
    assert st["failed"] == 1 and "checksum" in st["last_error"]
    assert not (run_dir / "work" / "raw").exists()
    assert sum(1 for u in server.urls if u.endswith(".edf")) == 2          # one retry


def test_the_verdict_must_beat_an_installed_model(run_dir):
    inst = run_dir / ".run" / "staging_weights"
    inst.mkdir(parents=True)
    rep = {"hrv": {"sm": {"kappa4": 0.62}}}
    assert P.verdict(rep)["install"] is True
    (inst / "stage4_hrv.json").write_text("{}")
    v = P.verdict(rep)
    assert v["install"] is False and "unknown" in v["why"]           # provenance unknown
    (run_dir / ".run" / "dreamt.status.json").write_text(json.dumps(
        {"stage": "installed", "scores": {"hrv": {"kappa4_smoothed": 0.64}}}))
    v = P.verdict(rep)
    assert v["install"] is False and v["installed_kappa4"] == 0.64
    assert P.verdict({"hrv": {"sm": {"kappa4": 0.70}}})["install"] is True
    assert P.verdict({"hrv": {"sm": {"kappa4": 0.30}}})["install"] is False


def test_subset_is_evenly_spaced_and_stable():
    ids = [f"mesa-sleep-{i:04d}" for i in range(1, 2057)]
    pick = P.evenly_spaced(list(reversed(ids)), 300)
    assert len(pick) == len(set(pick)) == 300 and pick == P.evenly_spaced(ids, 300)
    assert pick[0] == ids[0] and pick[-1] > ids[-10]
    assert P.evenly_spaced(ids[:5], 300) == ids[:5]


def test_the_intermediate_certificate_url_comes_from_the_leaf():
    der = b"\x30\x82junk\x86\x3fhttp://crt.example.com/Some-CA.crt\x30\x26more"
    assert P._aia_url(der) == "http://crt.example.com/Some-CA.crt"
    assert P._aia_url(b"nothing here") == P.AIA_FALLBACK
