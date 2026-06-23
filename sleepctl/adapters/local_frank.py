"""Tier 2 (GATED, LAST RESORT) on-device source — NOT wired by default.

Highest-fidelity raw data via the on-device "Frank" local API (GET /variables) plus a
tap of the STM32 sensor subsystem over USART. Reaching it requires rooting the Pod
(inject one SSH key into the microSD rootfs -> factory reset), which is only attempted
if ALL THREE gates pass, in order:

  1. NECESSITY  — Tier 0 (cloud) and Tier 1 (raw capture) are demonstrably insufficient.
  2. REVERSIBILITY — a full byte-for-byte microSD image is taken AND a restore-to-stock
     is PROVEN before any modification (no eFuse/bootloader-of-no-return), so the
     firmware state is always recoverable. (See recon/pod2_teardown.md.)
  3. MINIMALITY — the smallest possible change (a single authorized_keys entry).

Honest residual: the firmware path is 100% reversible via the verified image, but
physically opening a liquid-cooled hub carries a small non-software risk. Because of
that, this adapter ships as documented stubs and must be explicitly enabled.
"""

from __future__ import annotations

from sleepctl.adapters.base import PodSensorSource
from sleepctl.models import NightSummary, SensorFrame

_GATE_MESSAGE = (
    "LocalFrankSource is a gated last resort and is not enabled. It may only be used "
    "after the necessity + reversibility (verified SD image & proven restore) + "
    "minimality gates pass. See sleepctl/recon/pod2_teardown.md."
)


class LocalFrankSource(PodSensorSource):
    def __init__(self, host: str = "pod.local", port: int = 8000, enabled: bool = False) -> None:
        self.host = host
        self.port = port
        self.enabled = enabled

    def read_frame(self) -> SensorFrame:
        # Would GET http://<host>:<port>/variables and read the STM32 USART raw tap.
        raise NotImplementedError(_GATE_MESSAGE)

    def fetch_night_summary(self, date: str) -> NightSummary:
        raise NotImplementedError(_GATE_MESSAGE)

    def capabilities(self) -> dict:
        return {
            "source": "local_frank",
            "enabled": self.enabled,
            "gated": True,
            "gates": ["necessity", "reversibility(verified_sd_image)", "minimality"],
            "data": "Frank /variables + STM32 USART raw tap (highest fidelity)",
            "note": _GATE_MESSAGE,
        }
