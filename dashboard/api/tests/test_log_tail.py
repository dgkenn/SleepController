"""The log tails read only the end of the file, and return exactly the lines a whole-file read
would. daemon.log is redirected stdout that grows for as long as the daemon runs, and the battery
plus the health snapshot tail it several times per publish."""
import random

from app.diagnostics import read_tail_lines


def _whole(path, n):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.readlines()[-n:]


def test_tail_matches_a_whole_file_read(tmp_path):
    rng = random.Random(4)
    pieces = [b"plain line", "café — über".encode("utf-8"), b"bad \xff\xfe bytes",
              b"", b"x" * 300, "あい".encode("utf-8")]
    ends = [b"\n", b"\r\n", b"\r", b"\n\n"]
    for trial in range(40):
        body = b"".join(rng.choice(pieces) + rng.choice(ends) for _ in range(rng.randint(0, 400)))
        if trial % 3 == 0:
            body += b"no trailing newline \xe2\x80"          # truncated multibyte at the end
        path = tmp_path / f"f{trial}.log"
        path.write_bytes(body)
        for n in (1, 2, 12, 40, 200, 1000):
            for block in (7, 64, 65536):
                assert read_tail_lines(str(path), n, block=block) == _whole(path, n), (trial, n, block)


def test_tail_reads_only_the_end(tmp_path):
    path = tmp_path / "daemon.log"
    with open(path, "wb") as fh:
        for i in range(400_000):                   # ~20 MB
            fh.write(f"2026-09-25 13:31:{i % 60:02d} tick {i} ok ....................\n".encode())
    reads = []

    import builtins
    real_open = builtins.open

    class _Counting:
        def __init__(self, fh):
            self._fh = fh

        def read(self, size=-1):
            data = self._fh.read(size)
            reads.append(len(data))
            return data

        def __getattr__(self, k):
            return getattr(self._fh, k)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self._fh.__exit__(*a)

    def counting_open(p, mode="r", *a, **kw):
        fh = real_open(p, mode, *a, **kw)
        return _Counting(fh) if "b" in mode else fh

    builtins.open = counting_open
    try:
        lines = read_tail_lines(str(path), 40)
    finally:
        builtins.open = real_open
    assert lines == _whole(path, 40)
    assert sum(reads) <= 65536
