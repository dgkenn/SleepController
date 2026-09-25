"""EEG-headband hypnograms: upload one for a night, see how the stager agreed, calibrate.

Thin API layer over the engine: parsing and storage are ``sleepctl.eval.hypnogram_import``, the
comparison is ``sleepctl.eval.eeg_agreement`` and the personal fit is
``sleepctl.learning.eeg_calibration``. The upload is the raw file as the request body (no
multipart dependency), with what the file itself cannot say -- the night, a start time for a
bare epoch list, the zone of naive timestamps -- passed as query parameters.
"""

from __future__ import annotations

import re
from typing import Optional

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class UploadError(ValueError):
    """A client-side problem with the upload; ``status`` is the HTTP code to return."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _night(date: Optional[str]) -> Optional[str]:
    if date in (None, "", "auto"):
        return None
    if not _DATE.match(str(date)):
        raise UploadError(f"night date must be YYYY-MM-DD or 'auto', got {date!r}")
    return str(date)


def upload(repo, body: bytes, *, date: Optional[str] = None, source: str = "",
           filename: str = "", fmt: str = "auto", tz: str = "", start: str = "",
           numeric_scheme: str = "auto", epoch_s: float = 30.0, restage: bool = True) -> dict:
    """Import one night's hypnogram and return the import summary + agreement report."""
    from sleepctl.eval.eeg_agreement import agreement_report
    from sleepctl.eval.hypnogram_import import import_hypnogram

    if not body:
        raise UploadError("empty upload: send the exported file as the request body")
    if len(body) > MAX_UPLOAD_BYTES:
        raise UploadError(f"file over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB", status=413)
    if numeric_scheme not in ("auto", "aasm", "rk"):
        raise UploadError("numeric_scheme must be auto, aasm or rk")
    if not (5.0 <= float(epoch_s) <= 300.0):
        raise UploadError("epoch_s must be between 5 and 300 seconds")
    try:
        summary = import_hypnogram(
            repo.conn, body, night_date=_night(date), source=source or None,
            filename=filename, fmt=fmt or "auto", tz=tz or None, start=start or None,
            numeric_scheme=numeric_scheme, epoch_s=float(epoch_s))
    except UploadError:
        raise
    except ValueError as exc:
        raise UploadError(f"could not import the hypnogram: {exc}") from exc
    report = agreement_report(repo, summary["night_date"], include_restage=restage)
    return {"import": summary, "agreement": report}


def report(repo, date: str, restage: bool = True) -> dict:
    from sleepctl.eval.eeg_agreement import agreement_report
    nd = _night(date)
    if nd is None:
        raise UploadError("a night date is required")
    return agreement_report(repo, nd, include_restage=restage)


def nights(repo) -> dict:
    from sleepctl.eval.hypnogram_import import imported_nights
    from sleepctl.learning.eeg_calibration import load_calibration
    cal = load_calibration(repo.conn) or {}
    return {"nights": imported_nights(repo.conn),
            "calibration": {k: cal.get(k) for k in ("enabled", "wake_threshold_enabled",
                                                    "n_nights", "fitted_ts", "rationale")}
            if cal else None}


def delete(repo, date: str) -> dict:
    from sleepctl.eval.hypnogram_import import delete_night
    nd = _night(date)
    if nd is None:
        raise UploadError("a night date is required")
    return {"night_date": nd, "deleted_epochs": delete_night(repo.conn, nd)}


def calibration(repo) -> dict:
    from sleepctl.learning.eeg_calibration import load_calibration
    return load_calibration(repo.conn) or {"enabled": False, "n_nights": 0,
                                           "rationale": "not fitted yet"}


def calibrate(repo) -> dict:
    """Fit + validate on every imported night and store the result. The daemon picks it up at
    its next profile load (session start / night close-out)."""
    from sleepctl.learning.eeg_calibration import calibrate as _calibrate
    return _calibrate(repo)
