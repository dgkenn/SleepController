"""Two independent breathing estimates, fused with a confidence the consumers gate on."""
from app import bridge


def test_agreeing_estimates_fuse_to_their_mean_at_high_confidence():
    rate, conf, src = bridge.fuse_respiration(14.0, 0.6, {"rate": 13.0, "conc": 0.5, "n": 4, "agreeing": 4})
    assert rate == 13.5 and conf >= 0.85 and src == "rsa+acc"


def test_disagreeing_estimates_keep_the_stronger_peak_at_low_confidence():
    rate, conf, src = bridge.fuse_respiration(14.0, 0.56, {"rate": 20.0, "conc": 0.7, "n": 4, "agreeing": 4})
    assert rate == 20.0 and conf < 0.5 and src.startswith("acc")
    rate, conf, src = bridge.fuse_respiration(14.0, 0.8, {"rate": 20.0, "conc": 0.6, "n": 4, "agreeing": 4})
    assert rate == 14.0 and conf < 0.5 and src.startswith("rsa")


def test_a_single_estimate_carries_its_own_concentration():
    rate, conf, src = bridge.fuse_respiration(None, None, {"rate": 12.0, "conc": 0.7, "n": 3, "agreeing": 3})
    assert (rate, src) == (12.0, "acc") and 0.6 <= conf <= 0.8
    rate, conf, src = bridge.fuse_respiration(15.0, 0.6, None)
    assert (rate, src) == (15.0, "rsa") and conf == 0.6
    assert bridge.fuse_respiration(None, None, None) == (None, 0.0, None)


def test_the_accelerometer_rate_needs_agreeing_batches(tmp_path):
    from sleepctl.storage.repository import Repository
    from app import db as app_db
    r = Repository(str(tmp_path / "a.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL); app_db._apply_migrations(r.conn); r.conn.commit()
    bridge.append_actigraphy(r.conn, {"pim": 0.3, "n": 104, "fs": 52, "resp_brpm": 13.0, "resp_conc": 0.7})
    assert bridge.read_acc_respiration(r.conn) is None                    # one batch is not a rate
    bridge.append_actigraphy(r.conn, {"pim": 0.3, "n": 104, "fs": 52, "resp_brpm": 22.0, "resp_conc": 0.7})
    assert bridge.read_acc_respiration(r.conn) is None                    # two that disagree: no rate
    bridge.append_actigraphy(r.conn, {"pim": 0.3, "n": 104, "fs": 52, "resp_brpm": 13.5, "resp_conc": 0.6})
    d = bridge.read_acc_respiration(r.conn)
    assert d is not None and abs(d["rate"] - 13.5) <= 0.5 and d["agreeing"] == 2
