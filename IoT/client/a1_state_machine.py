# a1_state_machine.py
# version: v1.326-03-18 18:46 GMT

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
    def __init__(self, powerup_timeout_s: float = 25.0):
        self.powerup_timeout_s = powerup_timeout_s
        self.power_on_ts = None
        self.state = A1State.DISCONNECTED

    def update(self, inputs: A1Inputs):
        if inputs.plug_powered_off is True:
            self.power_on_ts = None
        elif inputs.plug_powered_off is False and self.power_on_ts is None:
            self.power_on_ts = inputs.now

        elapsed = None if self.power_on_ts is None else inputs.now - self.power_on_ts
        relay_recent = (
            inputs.relay_running
            and inputs.relay_age is not None
            and inputs.relay_age <= inputs.relay_timeout_s
        )
        all_ok = relay_recent and inputs.ping_ok and inputs.tcp_8883_ok and inputs.tcp_990_ok
        any_signal = inputs.ping_ok or inputs.tcp_8883_ok or inputs.tcp_990_ok

        if not inputs.connected:
            state = A1State.DISCONNECTED
        elif inputs.plug_powered_off is True:
            state = A1State.POWER_OFF
        elif all_ok:
            state = A1State.AVAILABLE
        elif inputs.plug_powered_off is False and elapsed is not None and elapsed < self.powerup_timeout_s:
            state = A1State.POWERING_UP
        elif inputs.plug_powered_off is False and elapsed is not None and elapsed >= self.powerup_timeout_s:
            state = A1State.FAULT if any_signal else A1State.UNAVAILABLE
        else:
            state = A1State.UNAVAILABLE

        self.state = state

        return {
            "a1_state": state,
            "tray_state": self._map_tray(state),
            "text": self._text(state, elapsed),
            "alert_reason": self._alert_reason(state, inputs),
            "powerup_elapsed_s": elapsed,
        }

    def _map_tray(self, state: A1State) -> TrayState:
        if state == A1State.DISCONNECTED:
            return TrayState.DISCONNECTED
        if state == A1State.POWER_OFF:
            return TrayState.NORMAL
        if state == A1State.POWERING_UP:
            return TrayState.WARNING
        if state == A1State.AVAILABLE:
            return TrayState.NORMAL
        if state == A1State.UNAVAILABLE:
            return TrayState.WARNING
        return TrayState.ALERT

    def _text(self, state: A1State, elapsed: float | None = None) -> str | None:
        if state == A1State.POWER_OFF:
            return "A1 Power Off"
        if state == A1State.POWERING_UP:
            seconds = int(elapsed) if elapsed is not None else 0
            return f"A1 Powering Up {seconds}s"
        if state == A1State.AVAILABLE:
            return "A1 Available"
        if state == A1State.UNAVAILABLE:
            return "OFFLINE"
        if state == A1State.FAULT:
            return "FAULT"
        return None

    def _alert_reason(self, state: A1State, inputs: A1Inputs) -> str | None:
        if state != A1State.FAULT:
            return None
        parts = []
        if not inputs.ping_ok:
            parts.append("Ping FAIL")
        if not inputs.tcp_8883_ok:
            parts.append("TCP 8883 FAIL")
        if not inputs.tcp_990_ok:
            parts.append("TCP 990 FAIL")
        return ", ".join(parts) if parts else "Partial A1 response"
