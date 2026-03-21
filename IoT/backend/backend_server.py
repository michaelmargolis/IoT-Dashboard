#!/usr/bin/env python3
# backend_server.py
# version: v2.5
# date: 2026-03-19 21:36 GMT

# Main IoT backend server for the private and IoT subnets.
# Publishes backend status over WebSocket and handles firewall control, diagnostics,
# Bambu discovery relay, and A1 smartplug power integration via the Kasa backend.

from __future__ import annotations
import argparse, asyncio, collections, dataclasses, json, socket, subprocess, time, os
from pathlib import Path
from typing import Any, Deque, Dict, Optional
import websockets


@dataclasses.dataclass
class RelayState:
    running: bool = False
    packets_total: int = 0
    last_seen_utc: Optional[str] = None
    last_seen_monotonic: Optional[float] = None
    last_src_ip: Optional[str] = None
    last_len: Optional[int] = None
    last_error: Optional[str] = None
    timeout_count: int = 0
    restart_count: int = 0

@dataclasses.dataclass
class DiagState:
    tcp_8883_ok: Optional[bool] = None
    tcp_990_ok: Optional[bool] = None
    last_check_utc: Optional[str] = None
    last_error: Optional[str] = None

@dataclasses.dataclass
class FirewallState:
    iot_internet_enabled: Optional[bool] = None
    last_change_utc: Optional[str] = None
    last_error: Optional[str] = None

@dataclasses.dataclass
class SystemState:
    uptime_s: Optional[int] = None
    loadavg: Optional[list] = None
    temp_c: Optional[float] = None
    ifaces: str = ""
    last_error: Optional[str] = None

@dataclasses.dataclass
class KasaA1State:
    service_available: bool = False
    relay_on: Optional[bool] = None
    powered_off: bool = False
    role_resolved: Optional[bool] = None
    device_present: Optional[bool] = None
    cache_id: Optional[str] = None
    alias: Optional[str] = None
    last_check_utc: Optional[str] = None
    last_error: Optional[str] = None
    power_on_started_monotonic: Optional[float] = None

class EventLog:
    def __init__(self, maxlen: int = 100):
        self._events: Deque[Dict[str, Any]] = collections.deque(maxlen=maxlen)
    def add(self, kind: str, message: str, **extra):
        evt = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, "message": message}
        evt.update(extra)
        self._events.append(evt)
    def tail(self, n: int = 50):
        return list(self._events)[-n:]

def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def tcp_check(host, port, timeout=2.0):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0

def run_cmd(cmd, timeout=5.0):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)

async def safe_send(websocket, payload):
    try:
        await websocket.send(json.dumps(payload))
        return True
    except websockets.exceptions.ConnectionClosed:
        return False

class KasaA1Client:
    def __init__(self, cfg, events):
        self.cfg = cfg
        self.events = events
        self.state = KasaA1State()
        self.ws = None
        self.ws_lock = asyncio.Lock()

    async def _request(self, payload: Dict[str, Any]):
        timeout = float(self.cfg.get("kasa_timeout_s", 1.0))
        try:
            async with self.ws_lock:
                if self.ws is None or getattr(self.ws, "closed", False):
                    self.ws = await websockets.connect(
                        self.cfg["kasa_ws_url"],
                        open_timeout=timeout,
                        ping_interval=20,
                        ping_timeout=20
                    )
                await asyncio.wait_for(self.ws.send(json.dumps(payload)), timeout=timeout)
                expected_type = payload.get("type")
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError(f"Timed out waiting for {expected_type}")
                    raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
                    reply = json.loads(raw)

                    if reply.get("type") == "status":
                        a1_power = ((reply.get("kasa") or {}).get("a1_power"))
                        if a1_power:
                            self._apply_a1_power(a1_power)
                        continue

                    if expected_type == "kasa_get_a1_power":
                        if reply.get("type") == "ack" and reply.get("action") == "kasa_get_a1_power":
                            return reply
                        continue

                    if expected_type == "kasa_toggle_a1_power":
                        if reply.get("type") == "ack" and reply.get("action") == "kasa_toggle_a1_power":
                            return reply
                        continue

                    return reply
        except Exception as e:
            async with self.ws_lock:
                if self.ws is not None:
                    try:
                        await self.ws.close()
                    except Exception:
                        pass
                    self.ws = None
            self.state.service_available = False
            self.state.last_check_utc = utc_now()
            self.state.last_error = str(e)
            return None

    async def close(self):
        async with self.ws_lock:
            if self.ws is not None:
                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None

    def _apply_a1_power(self, a1_power: Optional[Dict[str, Any]]):
        if not a1_power:
            self.state.service_available = False
            self.state.last_check_utc = utc_now()
            self.state.last_error = "No A1 power payload"
            return self.state
        prev_powered_off = self.state.powered_off
        self.state.service_available = True
        self.state.relay_on = a1_power.get("relay_on")
        self.state.powered_off = (a1_power.get("relay_on") is False)
        self.state.role_resolved = a1_power.get("role_resolved")
        self.state.device_present = a1_power.get("device_present")
        self.state.cache_id = a1_power.get("cache_id")
        self.state.alias = a1_power.get("alias")
        self.state.last_check_utc = a1_power.get("last_refresh_utc") or utc_now()
        self.state.last_error = a1_power.get("last_error")
        if prev_powered_off is True and self.state.powered_off is False:
            self.state.power_on_started_monotonic = time.monotonic()
        elif self.state.powered_off is True:
            self.state.power_on_started_monotonic = None
        return self.state

    async def refresh(self):
        reply = await self._request({"type": "kasa_get_a1_power"})
        if not reply:
            return self.state
        self._apply_a1_power(reply.get("a1_power"))
        return self.state

    async def toggle(self):
        reply = await self._request({"type": "kasa_toggle_a1_power"})
        if not reply:
            return {"type": "ack", "action": "toggle_a1_power", "ok": False, "error": self.state.last_error, "a1_power": None}
        self._apply_a1_power(reply.get("a1_power"))
        return {"type": "ack", "action": "toggle_a1_power", "ok": bool(reply.get("ok")), "error": reply.get("error"), "a1_power": reply.get("a1_power")}

    async def periodic(self, interval_s):
        while True:
            await self.refresh()
            await asyncio.sleep(interval_s)

class FirewallController:
    def __init__(self, cfg, events):
        self.cfg = cfg
        self.events = events
        self.state = FirewallState()
    def _chain_list_cmd(self):
        return ["sudo", "nft", "list", "chain", "inet", "filter", self.cfg["iot_toggle_chain"]]
    def chain_exists(self):
        cp = run_cmd(self._chain_list_cmd())
        return cp.returncode == 0
    def ensure_chain_exists(self):
        if self.chain_exists():
            self.state.last_error = None
            return True
        self.state.last_error = f"Missing nft chain inet filter {self.cfg['iot_toggle_chain']}"
        self.events.add("error", "Firewall toggle chain missing", chain=self.cfg["iot_toggle_chain"])
        return False
    def read(self):
        try:
            cp = run_cmd(self._chain_list_cmd())
            if cp.returncode != 0:
                self.state.last_error = cp.stderr.strip() or cp.stdout.strip() or "nft list failed"
                self.state.iot_internet_enabled = None
                return None
            text = cp.stdout
            enabled = ("ip daddr !=" in text and "accept" in text)
            self.state.iot_internet_enabled = enabled
            self.state.last_error = None
            return enabled
        except Exception as e:
            self.state.last_error = str(e)
            self.state.iot_internet_enabled = None
            return None
    def enable(self):
        if not self.ensure_chain_exists():
            return False
        current = self.read()
        if current is True:
            self.events.add("action", "IoT internet already enabled")
            return True
        chain = self.cfg["iot_toggle_chain"]
        cmd = ["sudo", "nft", "add", "rule", "inet", "filter", chain, "ip", "daddr", "!=", "{10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16,127.0.0.0/8}", "accept"]
        cp = run_cmd(cmd)
        if cp.returncode != 0:
            self.state.last_error = cp.stderr.strip() or cp.stdout.strip() or "nft add rule failed"
            self.events.add("error", "Failed to enable IoT internet", error=self.state.last_error)
            return False
        self.state.last_change_utc = utc_now()
        self.state.last_error = None
        self.read()
        self.events.add("action", "IoT internet enabled")
        return True
    def disable(self):
        if not self.ensure_chain_exists():
            return False
        current = self.read()
        if current is False:
            self.events.add("action", "IoT internet already disabled")
            return True
        chain = self.cfg["iot_toggle_chain"]
        cp = run_cmd(["sudo", "nft", "flush", "chain", "inet", "filter", chain])
        if cp.returncode != 0:
            self.state.last_error = cp.stderr.strip() or cp.stdout.strip() or "nft flush chain failed"
            self.events.add("error", "Failed to disable IoT internet", error=self.state.last_error)
            return False
        self.state.last_change_utc = utc_now()
        self.state.last_error = None
        self.read()
        self.events.add("action", "IoT internet disabled")
        return True

class Relay:
    def __init__(self, cfg, events, is_a1_powered_off, is_a1_powering_up):
        self.cfg = cfg
        self.events = events
        self.state = RelayState()
        self.debug_verbose = False
        self.is_a1_powered_off = is_a1_powered_off
        self.is_a1_powering_up = is_a1_powering_up

    def run_blocking(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rx.bind(("", self.cfg["ssdp_port"]))
        rx.settimeout(float(self.cfg.get("relay_rx_timeout_s", 30.0)))
        mreq = socket.inet_aton(self.cfg["ssdp_multicast"]) + socket.inet_aton(self.cfg["iot_interface_ip"])
        rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        tx.bind((self.cfg["lan_ip"], 0))

        self.state.running = True
        self.state.last_error = None
        self.state.restart_count += 1
        self.events.add("relay", "Relay started", port=self.cfg["ssdp_port"], restart_count=self.state.restart_count)

        while True:
            try:
                data, addr = rx.recvfrom(4096)
            except socket.timeout:
                if self.is_a1_powered_off():
                    self.state.last_error = None
                    self.state.last_seen_monotonic = None
                    continue
                if self.is_a1_powering_up():
                    self.state.last_error = None
                    continue
                self.state.timeout_count += 1
                self.state.last_error = f"No discovery packets for {self.cfg.get('relay_rx_timeout_s', 30.0)}s"
                self.events.add("relay", "Relay receive timeout; restarting socket", timeout_count=self.state.timeout_count, timeout_s=self.cfg.get("relay_rx_timeout_s", 30.0))
                raise RuntimeError(self.state.last_error)

            if addr[0] != self.cfg["printer_ip"]:
                continue
            if b"NOTIFY * HTTP/1.1" not in data:
                continue

            tx.sendto(data, (self.cfg["lan_broadcast"], self.cfg["ssdp_port"]))
            self.state.packets_total += 1
            self.state.last_seen_utc = utc_now()
            self.state.last_seen_monotonic = time.monotonic()
            self.state.last_src_ip = addr[0]
            self.state.last_len = len(data)
            self.state.last_error = None

            if self.debug_verbose:
                self.events.add("relay", "Relayed discovery packet", bytes=len(data), src=addr[0])

    async def run(self):
        while True:
            try:
                await asyncio.to_thread(self.run_blocking)
            except Exception as e:
                self.state.running = False
                self.state.last_error = str(e)
                self.events.add("error", "Relay loop exited; retrying", error=self.state.last_error, timeout_count=self.state.timeout_count, restart_count=self.state.restart_count)
                await asyncio.sleep(1)

class Diagnostics:
    def __init__(self, cfg, events, is_a1_powered_off):
        self.cfg = cfg
        self.events = events
        self.state = DiagState()
        self.is_a1_powered_off = is_a1_powered_off
    async def run_once(self):
        try:
            if self.is_a1_powered_off():
                self.state.tcp_8883_ok = None
                self.state.tcp_990_ok = None
                self.state.last_check_utc = utc_now()
                self.state.last_error = None
                return
            ip = self.cfg["printer_ip"]
            self.state.tcp_8883_ok = await asyncio.to_thread(tcp_check, ip, 8883, 2.0)
            self.state.tcp_990_ok = await asyncio.to_thread(tcp_check, ip, 990, 2.0)
            self.state.last_check_utc = utc_now()
            self.state.last_error = None
            self.events.add("diag", "Connectivity diagnostics complete", tcp_8883_ok=self.state.tcp_8883_ok, tcp_990_ok=self.state.tcp_990_ok)
        except Exception as e:
            self.state.last_error = str(e)
            self.events.add("error", "Diagnostics failed", error=self.state.last_error)
    async def periodic(self, interval_s):
        while True:
            await self.run_once()
            await asyncio.sleep(interval_s)

class SystemStatus:
    def __init__(self):
        self.state = SystemState()
    def _read_uptime(self):
        try:
            return int(float(Path("/proc/uptime").read_text().split()[0]))
        except Exception:
            return None
    def _read_temp(self):
        p = Path("/sys/class/thermal/thermal_zone0/temp")
        try:
            return round(int(p.read_text().strip()) / 1000.0, 1) if p.exists() else None
        except Exception:
            return None
    def _read_ifaces(self):
        cp = run_cmd(["ip", "-br", "addr"])
        if cp.returncode != 0:
            raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "ip -br addr failed")
        return cp.stdout.strip()
    async def refresh(self):
        try:
            self.state.uptime_s = await asyncio.to_thread(self._read_uptime)
            self.state.loadavg = list(os.getloadavg())
            self.state.temp_c = await asyncio.to_thread(self._read_temp)
            self.state.ifaces = await asyncio.to_thread(self._read_ifaces)
            self.state.last_error = None
        except Exception as e:
            self.state.last_error = str(e)
    async def periodic(self, interval_s):
        while True:
            await self.refresh()
            await asyncio.sleep(interval_s)

class Backend:
    def __init__(self, cfg):
        self.cfg = cfg
        self.events = EventLog(maxlen=cfg["event_log_max"])
        self.kasa_a1 = KasaA1Client(cfg, self.events)
        self.relay = Relay(cfg, self.events, self.is_a1_powered_off, self.is_a1_powering_up)
        self.diag = Diagnostics(cfg, self.events, self.is_a1_powered_off)
        self.firewall = FirewallController(cfg, self.events)
        self.system = SystemStatus()
        self.clients = set()
    def is_a1_powered_off(self):
        return self.kasa_a1.state.service_available and self.kasa_a1.state.powered_off
    def is_a1_powering_up(self):
        ts = self.kasa_a1.state.power_on_started_monotonic
        if not self.kasa_a1.state.service_available:
            return False
        if self.kasa_a1.state.powered_off:
            return False
        if ts is None:
            return False
        return (time.monotonic() - ts) < float(self.cfg.get("a1_powerup_timeout_s", 25.0))
    def clear_errors(self):
        self.relay.state.last_error = None
        self.diag.state.last_error = None
        self.firewall.state.last_error = None
        self.system.state.last_error = None
        self.kasa_a1.state.last_error = None
        self.events.add("action", "Cleared last_error values")
    def startup_self_check(self):
        ok = True
        if not self.firewall.ensure_chain_exists():
            ok = False
        ifaces = self.system.state.ifaces or ""
        if self.cfg["iot_interface_ip"] not in ifaces:
            self.events.add("error", "Configured IoT interface IP not found", iot_interface_ip=self.cfg["iot_interface_ip"])
            ok = False
        self.events.add("startup", "Startup self-check complete", ok=ok)
        return ok
    def build_status(self):
        relay_age = None
        if not self.is_a1_powered_off() and self.relay.state.last_seen_monotonic is not None:
            relay_age = round(time.monotonic() - self.relay.state.last_seen_monotonic, 1)
        return {
            "type": "status",
            "schema_version": 1,
            "ts": utc_now(),
            "config": {
                "printer_ip": self.cfg["printer_ip"],
                "lan_ip": self.cfg["lan_ip"],
                "ssdp_port": self.cfg["ssdp_port"],
                "ws_port": self.cfg["ws_port"],
                "relay_rx_timeout_s": self.cfg["relay_rx_timeout_s"],
                "a1_powerup_timeout_s": self.cfg["a1_powerup_timeout_s"],
            },
            "relay": {
                "running": self.relay.state.running,
                "packets_total": self.relay.state.packets_total,
                "last_seen_utc": self.relay.state.last_seen_utc,
                "seconds_since_last": relay_age,
                "last_src_ip": self.relay.state.last_src_ip,
                "last_len": self.relay.state.last_len,
                "last_error": self.relay.state.last_error,
                "debug_verbose": self.relay.debug_verbose,
                "timeout_count": self.relay.state.timeout_count,
                "restart_count": self.relay.state.restart_count,
            },
            "relay_devices": {
                "bambu_A1": {
                    "ip": self.cfg["printer_ip"],
                    "status": "Powered Off" if self.is_a1_powered_off() else "Active",
                    "powered_off": self.is_a1_powered_off(),
                    "tcp_8883_ok": self.diag.state.tcp_8883_ok,
                    "tcp_990_ok": self.diag.state.tcp_990_ok,
                    "last_check_utc": self.diag.state.last_check_utc,
                    "last_error": self.diag.state.last_error
                }
            },
            "kasa": {
                "a1_power": {
                    "service_available": self.kasa_a1.state.service_available,
                    "relay_on": self.kasa_a1.state.relay_on,
                    "powered_off": self.kasa_a1.state.powered_off,
                    "role_resolved": self.kasa_a1.state.role_resolved,
                    "device_present": self.kasa_a1.state.device_present,
                    "cache_id": self.kasa_a1.state.cache_id,
                    "alias": self.kasa_a1.state.alias,
                    "last_check_utc": self.kasa_a1.state.last_check_utc,
                    "last_error": self.kasa_a1.state.last_error
                }
            },
            "firewall": {
                "iot_internet_enabled": self.firewall.state.iot_internet_enabled,
                "last_change_utc": self.firewall.state.last_change_utc,
                "last_error": self.firewall.state.last_error
            },
            "system": {
                "uptime_s": self.system.state.uptime_s,
                "loadavg": self.system.state.loadavg,
                "temp_c": self.system.state.temp_c,
                "ifaces": self.system.state.ifaces,
                "last_error": self.system.state.last_error
            },
            "events": self.events.tail(self.cfg["event_tail_default"])
        }
    async def broadcast_loop(self):
        while True:
            if self.clients:
                payload = self.build_status()
                stale = []
                for ws in list(self.clients):
                    ok = await safe_send(ws, payload)
                    if not ok:
                        stale.append(ws)
                for ws in stale:
                    self.clients.discard(ws)
            await asyncio.sleep(self.cfg["broadcast_interval_s"])
    async def handle_message(self, websocket, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            await safe_send(websocket, {"type": "error", "message": "invalid JSON"})
            return
        t = msg.get("type")
        print("got msg:", t)
        if t == "get_status":
            await safe_send(websocket, self.build_status()); return
        if t == "get_events":
            limit = int(msg.get("limit", self.cfg["event_tail_default"]))
            await safe_send(websocket, {"type": "events", "events": self.events.tail(limit)}); return
        if t == "run_diagnostics":
            await self.diag.run_once()
            await safe_send(websocket, {"type": "ack", "action": "run_diagnostics", "ok": True, "relay_devices": {"bambu_A1": {"ip": self.cfg["printer_ip"], "tcp_8883_ok": self.diag.state.tcp_8883_ok, "tcp_990_ok": self.diag.state.tcp_990_ok, "last_check_utc": self.diag.state.last_check_utc, "last_error": self.diag.state.last_error}}}); return
        if t == "set_iot_internet":
            enabled = bool(msg.get("enabled"))
            ok = await asyncio.to_thread(self.firewall.enable if enabled else self.firewall.disable)
            await safe_send(websocket, {"type": "ack", "action": "set_iot_internet", "ok": ok, "enabled": enabled, "state": {"iot_internet_enabled": self.firewall.state.iot_internet_enabled, "last_change_utc": self.firewall.state.last_change_utc, "last_error": self.firewall.state.last_error}}); return
        if t == "set_debug":
            self.relay.debug_verbose = bool(msg.get("enabled", False))
            self.events.add("action", "Relay debug verbosity changed", enabled=self.relay.debug_verbose)
            await safe_send(websocket, {"type": "ack", "action": "set_debug", "ok": True, "enabled": self.relay.debug_verbose}); return
        if t == "clear_errors":
            self.clear_errors()
            await safe_send(websocket, {"type": "ack", "action": "clear_errors", "ok": True}); return
        if t == "toggle_a1_power":
            result = await self.kasa_a1.toggle()
            await safe_send(websocket, result)
            return
        if t == "ping":
            await safe_send(websocket, {"type": "pong", "ts": utc_now()}); return
        await safe_send(websocket, {"type": "error", "message": f"unknown message type: {t}"})
    async def ws_handler(self, websocket):
        remote = websocket.remote_address[0] if websocket.remote_address else ""
        allowed = set(self.cfg.get("allowed_client_ips", []))
        if allowed and remote not in allowed:
            await websocket.close(code=4403, reason="forbidden")
            return
        self.clients.add(websocket)
        self.events.add("client", "Client connected", remote_ip=remote)
        await safe_send(websocket, self.build_status())
        try:
            async for raw in websocket:
                await self.handle_message(websocket, raw)
        finally:
            self.clients.discard(websocket)
            self.events.add("client", "Client disconnected", remote_ip=remote)
    async def run(self):
        self.firewall.read()
        await self.kasa_a1.refresh()
        await self.diag.run_once()
        await self.system.refresh()
        self.startup_self_check()
        asyncio.create_task(self.kasa_a1.periodic(self.cfg["kasa_poll_interval_s"]))
        asyncio.create_task(self.relay.run())
        asyncio.create_task(self.diag.periodic(self.cfg["diag_interval_s"]))
        asyncio.create_task(self.system.periodic(self.cfg["system_interval_s"]))
        asyncio.create_task(self.broadcast_loop())
        async with websockets.serve(self.ws_handler, self.cfg["ws_host"], self.cfg["ws_port"], ping_interval=20, ping_timeout=20):
            self.events.add("server", "Kasa WebSocket server started", host=self.cfg["ws_host"], port=self.cfg["ws_port"])
            try:
                await asyncio.Future()
            finally:
                pass

DEFAULT_CONFIG = {
    "printer_ip": "192.168.50.92",
    "lan_ip": "192.168.1.2",
    "lan_broadcast": "192.168.1.255",
    "iot_interface_ip": "192.168.50.1",
    "ssdp_multicast": "239.255.255.250",
    "ssdp_port": 1990,
    "ws_host": "0.0.0.0",
    "ws_port": 8765,
    "iot_toggle_chain": "iot_inet_toggle",
    "allowed_client_ips": ["192.168.1.50"],
    "diag_interval_s": 10.0,
    "system_interval_s": 5.0,
    "broadcast_interval_s": 1.0,
    "event_log_max": 100,
    "event_tail_default": 20,
    "relay_rx_timeout_s": 30.0,
    "kasa_ws_url": "ws://127.0.0.1:8775",
    "kasa_poll_interval_s": 2.0,
    "kasa_timeout_s": 1.0,
}
def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path:
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    args = ap.parse_args()
    await Backend(load_config(args.config)).run()
if __name__ == "__main__":
    asyncio.run(main())
