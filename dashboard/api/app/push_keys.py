"""One-shot VAPID keypair generator for Web Push (run manually, not imported by the app).

Usage:
    pip install pywebpush          # optional dep, only needed for this + real push delivery
    python -m app.push_keys

Prints ``VAPID_PUBLIC_KEY=...`` / ``VAPID_PRIVATE_KEY=...`` lines to paste into your
``.env`` (see deploy/.env.example). Never commit real keys — keep them in the
gitignored ``.env``/deploy secrets only, same as ``JWT_SECRET``.
"""

from __future__ import annotations

import base64
import sys


def generate() -> tuple[str, str]:
    """Returns (public_key, private_key) as URL-safe base64 strings suitable for the
    VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY env vars. Requires ``py_vapid`` (a dependency
    of ``pywebpush``); raises ImportError with a helpful message if it's absent."""
    try:
        from py_vapid import Vapid  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only when dep is missing
        raise ImportError(
            "Generating VAPID keys requires the optional 'pywebpush' package "
            "(pip install pywebpush). It is NOT a hard dependency of the dashboard API."
        ) from exc

    v = Vapid()
    v.generate_keys()
    raw_public = v.public_key.public_bytes(
        encoding=__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.X962,
        format=__import__("cryptography.hazmat.primitives.serialization", fromlist=["PublicFormat"]).PublicFormat.UncompressedPoint,
    )
    raw_private = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    public_b64 = base64.urlsafe_b64encode(raw_public).rstrip(b"=").decode()
    private_b64 = base64.urlsafe_b64encode(raw_private).rstrip(b"=").decode()
    return public_b64, private_b64


def main() -> int:
    try:
        pub, priv = generate()
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"VAPID_PUBLIC_KEY={pub}")
    print(f"VAPID_PRIVATE_KEY={priv}")
    print("# Paste both into deploy/.env (gitignored) — never commit real keys.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
