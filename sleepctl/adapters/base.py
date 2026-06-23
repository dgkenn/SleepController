"""Abstract adapter interfaces.

The controller is hardware-agnostic: it only ever talks to these ABCs. Concrete
implementations (Eight Sleep cloud, raw network capture, gated local Frank tap,
Google Calendar, simulator) plug in without the controller changing. This is what
lets data fidelity improve (Tier 0 cloud -> Tier 1 raw capture -> Tier 2 local) with
zero controller changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from sleepctl.models import ContextRecord, NightSummary, SensorFrame


class PodSensorSource(ABC):
    """Reads in-bed physiology from the Pod."""

    @abstractmethod
    def read_frame(self) -> SensorFrame:
        """Return the freshest available sensor sample."""

    @abstractmethod
    def fetch_night_summary(self, date: str) -> NightSummary:
        """Return the end-of-night rollup for the given ISO date."""

    @abstractmethod
    def capabilities(self) -> dict:
        """Report which fields/commands this source actually supports (Pod 2 probe)."""


class ThermalActuator(ABC):
    """Issues temperature commands to the Pod (unitless level, -100..100)."""

    @abstractmethod
    def set_level(self, level: int, duration_s: int = 0) -> None:
        """Set the immediate heating level; ``duration_s`` 0 means until changed."""

    @abstractmethod
    def set_smart_level(self, level: int, stage: str) -> None:
        """Set the per-stage smart level (stage: bedtime/initial/final)."""

    @abstractmethod
    def set_alarm(self, time: datetime, vibration: int, thermal_level: int) -> None:
        """Configure a wake alarm (vibration 0/20/50/100; thermal_level -100..100)."""

    @abstractmethod
    def get_current_level(self) -> int:
        """Return the current device heating level."""


class CalendarSource(ABC):
    """Provides schedule context: required wake time, first commitment, etc."""

    @abstractmethod
    def get_context(self, date: str) -> ContextRecord:
        """Return schedule-derived context for the given ISO date."""


class WearableSource(ABC):
    """Optional daytime wearable context.

    Currently unused: the Pod's own bed sensors are the physiology source. This ABC
    exists so a wearable (Whoop/Oura/etc.) can be added later without controller
    changes; no concrete implementation is provided in this build.
    """

    @abstractmethod
    def get_context(self, date: str) -> ContextRecord:
        """Return wearable-derived context for the given ISO date."""
