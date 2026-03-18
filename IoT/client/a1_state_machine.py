# a1_state_machine.py
# version: v1.0
# date: 2026-03-18 10:08 GMT

from dataclasses import dataclass
from enum import Enum, auto

class A1State(Enum):
    DISCONNECTED = auto()
    POWER_OFF = auto()
    POWERING_UP = auto()
    AVAILABLE = auto()
    UNAVAILABLE = auto()
    FAULT = auto()

class TrayState(Enum):
    DISCONNECTED = auto()
    NORMAL = auto()
    WARNING = auto()
    ALERT = auto()

@dataclass
class A1Inputs:
    connected: bool
    plug_powered_off: bool | None
    relay_running: bool
    ping_ok: bool
    tcp_8883_ok: bool
    tcp_990_ok: bool
    relay_age: float | None
    relay_timeout_s: float
    now: float

class A1StateMachine:
    def __init__(self, powerup_timeout_s: float = 12.0):
        self.powerup_timeout_s = powerup_timeout_s
        self.prev_plug_off = None
        self.power_on_ts = None
        self.state = A1State.DISCONNECTED

    def update(self, i: A1Inputs):
        if self.prev_plug_off is True and i.plug_powered_off is False:
            self.power_on_ts = i.now
        self.prev_plug_off = i.plug_powered_off

        elapsed = None
        if self.power_on_ts is not None:
            elapsed = i.now - self.power_on_ts

        all_ok = (
            i.relay_running and
            i.ping_ok and
            i.tcp_8883_ok and
            i.tcp_990_ok and
            i.relay_age is not None and
            i.relay_age <= i.relay_timeout_s
        )

        any_signal = i.ping_ok or i.tcp_8883_ok or i.tcp_990_ok

        if not i.connected:
            self.state = A1State.DISCONNECTED
        elif i.plug_powered_off is True:
            self.state = A1State.POWER_OFF
        elif all_ok:
            self.state = A1State.AVAILABLE
        elif i.plug_powered_off is False and elapsed is not None and elapsed < self.powerup_timeout_s:
            self.state = A1State.POWERING_UP
        elif i.plug_powered_off is False and elapsed is not None and elapsed >= self.powerup_timeout_s:
            if any_signal:
                self.state = A1State.FAULT
            else:
                self.state = A1State.UNAVAILABLE
        else:
            self.state = A1State.UNAVAILABLE

        return {
            "a1_state": self.state,
            "tray_state": self._map_tray(self.state),
            "text": self._text(self.state)
        }

    def _map_tray(self, s):
        if s == A1State.DISCONNECTED:
            return TrayState.DISCONNECTED
        if s == A1State.POWER_OFF:
            return TrayState.NORMAL
        if s == A1State.POWERING_UP:
            return TrayState.WARNING
        if s == A1State.AVAILABLE:
            return TrayState.NORMAL
        if s == A1State.UNAVAILABLE:
            return TrayState.WARNING
        if s == A1State.FAULT:
            return TrayState.ALERT

    def _text(self, s):
        if s == A1State.POWER_OFF:
            return "A1 Power Off"
        if s == A1State.POWERING_UP:
            return "A1 Powering Up"
        if s == A1State.AVAILABLE:
            return "A1 Available"
        return None
