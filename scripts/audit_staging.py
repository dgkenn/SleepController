#!/usr/bin/env python3
"""Judge each night's stage calls against their physiological signatures (no PSG needed).

    python3 scripts/audit_staging.py /path/to/nights/night-2026-09-*.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleepctl.eval.stage_consistency import format_report, stage_consistency


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    for p in argv[1:]:
        try:
            with open(p) as fh:
                night = json.load(fh)
        except Exception as exc:
            print(f"{p}: {exc!r}")
            continue
        res = stage_consistency(night)
        print(format_report(Path(p).stem, res))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
