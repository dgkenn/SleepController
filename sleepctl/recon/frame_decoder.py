"""Binary-frame analyzer + BCG decode scaffold for a PASSIVELY CAPTURED Pod raw batch.

RISK-FREE, NON-ROOTING: this never touches the Pod. It is run later, OFFLINE, against bytes
obtained by passive packet capture of the Pod's upload to ``raw-api-upload.8slp.net:1337``
(see ``passive_capture.md``). It only does something useful if that stream turns out to be
PLAINTEXT — the analyzer's first job is to tell you whether it's plaintext or ciphertext.

UNVALIDATED against the real format (nobody has published a decode). Everything that depends on
the real framing is marked TODO. What IS real and tested (see ``__main__``): the structural
heuristics (entropy / record-size / magic) and the BCG beat detector, proven on synthetic bytes.

Pure stdlib — no numpy/scipy — so it imports and runs anywhere.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Known payload content (from the Pod's own device logs, community-documented):
#   6x capacitance @ 2 Hz, 2x piezo/BCG @ ~500 Hz, 8x temperatures. The *wire encoding* of the
#   batch is unknown; these constants anchor the decode search once real bytes exist.
PIEZO_FS_HZ = 500.0
CAP_FS_HZ = 2.0
N_PIEZO, N_CAP, N_TEMP = 2, 6, 8


# --------------------------------------------------------------------------- structure analysis
def shannon_entropy(data: bytes) -> float:
    """Bits/byte. ~8.0 => looks encrypted/compressed (NO-GO for decode); low => structured."""
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts if c)


def looks_encrypted(data: bytes, threshold: float = 7.5) -> bool:
    return shannon_entropy(data) >= threshold


def hexdump(data: bytes, width: int = 16, limit: int = 256) -> str:
    out = []
    for i in range(0, min(len(data), limit), width):
        chunk = data[i:i + width]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{i:08x}  {hexs:<{width*3}}  {ascii_}")
    return "\n".join(out)


def detect_record_size(data: bytes, min_size: int = 4, max_size: int = 4096) -> Optional[int]:
    """Find a repeating record length via byte-autocorrelation (the lag that best self-matches).
    A clean periodicity strongly suggests fixed-size records (e.g. per-sample frames)."""
    n = len(data)
    if n < min_size * 4:
        return None
    best_lag, best_score = None, 0.0
    for lag in range(min_size, min(max_size, n // 3)):
        matches = sum(1 for i in range(n - lag) if data[i] == data[i + lag])
        score = matches / (n - lag)
        if score > best_score:
            best_score, best_lag = score, lag
    return best_lag if best_score >= 0.35 else None


def sniff_serialization(data: bytes) -> str:
    """Best-effort guess of the container format from leading bytes."""
    if not data:
        return "empty"
    b0 = data[0]
    if b0 in (0x78, 0x1f) or data[:1] == b"\x1f":
        return "maybe-zlib/gzip-compressed"
    if 0xa0 <= b0 <= 0xbf or 0x80 <= b0 <= 0x9f or b0 in (0xd9, 0xda, 0xdb):
        return "maybe-cbor/msgpack"
    if (b0 & 0x07) in (0, 2) and (b0 >> 3) in range(1, 20):
        return "maybe-protobuf (field-1ish tag)"
    if all(32 <= c < 127 or c in (9, 10, 13) for c in data[:64]):
        return "ascii/text-ish (e.g. log lines)"
    return "unknown-binary"


@dataclass
class StructureReport:
    n_bytes: int
    entropy: float
    encrypted_likely: bool
    record_size: Optional[int]
    serialization_guess: str

    def summary(self) -> str:
        verdict = ("CIPHERTEXT — decode NOT possible without the device key (stop; zero risk taken)"
                   if self.encrypted_likely else "PLAINTEXT-ish — decode is worth pursuing")
        return (f"{self.n_bytes} bytes | entropy {self.entropy:.2f} bits/byte | {verdict}\n"
                f"  record_size≈{self.record_size}  serialization={self.serialization_guess}")


def analyze(data: bytes) -> StructureReport:
    return StructureReport(
        n_bytes=len(data), entropy=shannon_entropy(data),
        encrypted_likely=looks_encrypted(data),
        record_size=detect_record_size(data),
        serialization_guess=sniff_serialization(data))


# --------------------------------------------------------------------------- BCG -> heart rate
def _moving_avg(xs: List[float], k: int) -> List[float]:
    if k <= 1:
        return list(xs)
    out, acc, q = [], 0.0, []
    for x in xs:
        q.append(x); acc += x
        if len(q) > k:
            acc -= q.pop(0)
        out.append(acc / len(q))
    return out


def detrend(samples: List[float], fs: float) -> List[float]:
    """Remove slow respiration/baseline drift: subtract a ~1s moving average."""
    k = max(1, int(fs))
    base = _moving_avg(samples, k)
    return [s - b for s, b in zip(samples, base)]


def find_beats(samples: List[float], fs: float, min_bpm: float = 35.0,
               max_bpm: float = 140.0, detrended: Optional[List[float]] = None) -> List[int]:
    """Peak-pick the detrended BCG: local maxima above a robust threshold, spaced by the
    physiological refractory period. Returns sample indices of detected beats.

    ``detrended``, if given, is used instead of recomputing ``detrend(samples, fs)`` -- callers
    that also need the detrended signal / beats elsewhere (see ``BCGProcessor.vitals``) can
    compute it once and pass it to every helper instead of each one redoing it."""
    if len(samples) < int(fs):
        return []
    x = detrended if detrended is not None else detrend(samples, fs)
    mean = sum(x) / len(x)
    sd = (sum((v - mean) ** 2 for v in x) / len(x)) ** 0.5
    thresh = mean + 0.5 * sd
    min_gap = int(fs * 60.0 / max_bpm)
    beats: List[int] = []
    i = 1
    while i < len(x) - 1:
        if x[i] > thresh and x[i] >= x[i - 1] and x[i] >= x[i + 1]:
            if not beats or (i - beats[-1]) >= min_gap:
                beats.append(i)
                i += min_gap
                continue
        i += 1
    return beats


def heart_rate_from_beats(beats: List[int], fs: float) -> Optional[float]:
    """Beat-to-beat HR (bpm) from already-detected beat indices -- the shared tail end of
    ``heart_rate_from_bcg``, split out so a caller that already ran ``find_beats`` (e.g.
    ``BCGProcessor.vitals``) doesn't have to re-detect beats just to get the rate."""
    if len(beats) < 2:
        return None
    intervals = [(beats[i] - beats[i - 1]) / fs for i in range(1, len(beats))]
    mean_ibi = sum(intervals) / len(intervals)
    return 60.0 / mean_ibi if mean_ibi > 0 else None


def heart_rate_from_bcg(samples: List[float], fs: float) -> Optional[float]:
    beats = find_beats(samples, fs)
    return heart_rate_from_beats(beats, fs)


def movement_index(samples: List[float], fs: float, window_s: float = 1.0,
                   detrended: Optional[List[float]] = None) -> float:
    """Sub-second restlessness proxy: short-window RMS of the high-passed signal, normalized.
    THIS is the fast precursor the 60s cloud bins away — the main prize of raw capture.

    ``detrended``, if given, is used instead of recomputing ``detrend(samples, fs)`` (see
    ``find_beats`` for why)."""
    if not samples:
        return 0.0
    x = detrended if detrended is not None else detrend(samples, fs)
    rms = (sum(v * v for v in x) / len(x)) ** 0.5
    return rms


# --------------------------------------------------------------------------- self-test
def _synth_batch() -> Tuple[bytes, List[float], float]:
    """Fabricate a plausible PLAINTEXT batch: a small header + fixed-size little-endian records,
    each carrying a piezo sample from a synthetic ~66 bpm BCG at 500 Hz. Lets the analyzer +
    beat detector run end-to-end with NO real capture."""
    fs = PIEZO_FS_HZ
    secs = 6
    bpm = 66.0
    f_beat = bpm / 60.0
    piezo = []
    for n in range(int(fs * secs)):
        t = n / fs
        # heartbeat impulse-ish + small respiration + noise (deterministic)
        beat = math.sin(2 * math.pi * f_beat * t) ** 7  # peaky
        resp = 0.2 * math.sin(2 * math.pi * 0.25 * t)
        noise = 0.02 * math.sin(2 * math.pi * 37 * t)
        piezo.append(beat + resp + noise)
    # frame: 4-byte magic, then 2-byte little-endian length, then records of [int16 piezo]
    body = bytearray()
    for s in piezo:
        v = max(-32767, min(32767, int(s * 10000)))
        body += int(v).to_bytes(2, "little", signed=True)
    blob = b"8SLP" + len(body).to_bytes(2, "little") + bytes(body)
    return bytes(blob), piezo, fs


def _decode_int16_le_channel(body: bytes) -> List[float]:
    return [int.from_bytes(body[i:i + 2], "little", signed=True) / 10000.0
            for i in range(0, len(body) - 1, 2)]


def _self_test() -> int:
    blob, piezo_truth, fs = _synth_batch()
    print("== structure analysis ==")
    rep = analyze(blob)
    print(rep.summary())
    print("\n== hexdump (head) ==")
    print(hexdump(blob, limit=48))
    # In the synthetic batch we know the layout; in the REAL one this is the TODO the analyzer
    # narrows down. Strip the 6-byte header and decode the int16 channel.
    body = blob[6:]
    samples = _decode_int16_le_channel(body)
    hr = heart_rate_from_bcg(samples, fs)
    mv = movement_index(samples, fs)
    print(f"\n== BCG decode ==\n  samples={len(samples)} recovered_HR={hr:.1f} bpm "
          f"(truth 66) | movement_index={mv:.3f}")
    ok = rep.encrypted_likely is False and hr is not None and abs(hr - 66.0) <= 6.0
    print("\nSELF-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
