#!/usr/bin/env python3
from __future__ import annotations
import asyncio, dataclasses, socket, time
from typing import Dict, Any, Optional
from kasa_protocol import DISCOVERY_PORT, build_discovery, build_get_sysinfo, build_get_realtime, build_set_relay, from_packet

def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

@dataclasses.dataclass
class KasaDeviceState:
    cache_id: str
    device_id: str
    child_id: Optional[str]
    alias: str
    model: str
    ip: str
    reachable: bool = False
    relay_on: Optional[bool] = None
    has_emeter: bool = False
    last_seen_utc: Optional[str] = None
    last_refresh_utc: Optional[str] = None
    last_error: Optional[str] = None
    energy_json: Optional[Dict[str, Any]] = None

class KasaManager:
    def __init__(self, cfg: Dict[str, Any], events):
        self.cfg = cfg
        self.events = events
        self.devices: Dict[str, KasaDeviceState] = {}
        self.last_refresh_utc: Optional[str] = None
        self.last_error: Optional[str] = None
        self.role_aliases: Dict[str, str] = dict(cfg.get("kasa_role_aliases", {}))
        self.roles: Dict[str, Optional[str]] = dict(cfg.get("kasa_roles", {}))

    def _mk_sock(self, port) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(float(self.cfg.get("kasa_command_timeout_s", 3)))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind((self.cfg["iot_ip"], port))
        return s

    async def _udp_query(self, ip: str, packet: bytes):
        def run():
            s = self._mk_sock(0)
            try:
                s.sendto(packet, (ip, DISCOVERY_PORT))
                data, _ = s.recvfrom(4096)
                return from_packet(data)
            finally:
                s.close()
        try:
            return await asyncio.to_thread(run)
        except Exception:
            return None

    async def discovery_refresh(self):
        self.last_error = None
        self.events.add("kasa", "Kasa discovery refresh started")
        found: Dict[str, KasaDeviceState] = {}
        def run():
            s = self._mk_sock(DISCOVERY_PORT)
            try:
                s.sendto(build_discovery(), ("255.255.255.255", DISCOVERY_PORT))
                deadline = time.time() + float(self.cfg.get("kasa_command_timeout_s", 3))
                responses = []

                while time.time() < deadline:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break

                    s.settimeout(remaining)

                    try:
                        data, addr = s.recvfrom(4096)
                        obj = from_packet(data)
                        responses.append((addr[0], obj))
                    except socket.timeout:
                        break
                    except Exception:
                        continue

                return responses
            finally:
                s.close()

        try:
            responses = await asyncio.to_thread(run)
            for ip, obj in responses:
                sysinfo = obj.get("system", {}).get("get_sysinfo", {})
                device_id = str(sysinfo.get("deviceId", "") or sysinfo.get("mic_mac", "") or ip)
                model = str(sysinfo.get("model", ""))
                children = sysinfo.get("children")

                if isinstance(children, list):
                    for child in children:
                        child_id = str(child.get("id", ""))
                        alias = str(child.get("alias", ""))
                        if not child_id:
                            continue
                        cache_id = f"{device_id}{child_id[-2:]}"
                        found[cache_id] = KasaDeviceState(
                            cache_id,
                            device_id,
                            child_id,
                            alias,
                            model,
                            ip,
                            True,
                            (child.get("state") == 1) if "state" in child else None,
                            ("feature" in sysinfo and "ENE" in str(sysinfo.get("feature"))),
                            utc_now(),
                            utc_now()
                        )
                else:
                    alias = str(sysinfo.get("alias", ""))
                    cache_id = device_id
                    found[cache_id] = KasaDeviceState(
                        cache_id,
                        device_id,
                        None,
                        alias,
                        model,
                        ip,
                        True,
                        (sysinfo.get("relay_state") == 1) if "relay_state" in sysinfo else None,
                        ("feature" in sysinfo and "ENE" in str(sysinfo.get("feature"))),
                        utc_now(),
                        utc_now()
                    )

            self.devices = found
            self.last_refresh_utc = utc_now()

            for role, alias in self.role_aliases.items():
                matches = [d for d in self.devices.values() if d.alias == alias]
                if len(matches) == 1:
                    prev = self.roles.get(role)
                    self.roles[role] = matches[0].cache_id
                    if prev and prev != matches[0].cache_id:
                        self.events.add("kasa", "Role auto-adopted replacement device", role=role, old_cache_id=prev, new_cache_id=matches[0].cache_id, alias=alias)
                elif len(matches) > 1:
                    self.events.add("error", "Kasa alias ambiguous", role=role, alias=alias, match_count=len(matches))
                else:
                    self.events.add("kasa", "Kasa role alias not currently found", role=role, alias=alias)

            self.events.add("kasa", "Kasa discovery refresh complete", device_count=len(self.devices))
            return self.build_status_block()

        except Exception as e:
            self.last_error = str(e)
            self.events.add("error", "Kasa discovery refresh failed", error=self.last_error)
            return self.build_status_block()

    async def refresh_device(self, cache_id: str):
        dev = self.devices.get(cache_id)
        if not dev:
            return None
        obj = await self._udp_query(dev.ip, build_get_sysinfo())
        if not obj:
            dev.reachable = False
            dev.last_error = "No response"
            dev.last_refresh_utc = utc_now()
            return dev
        sysinfo = obj.get("system", {}).get("get_sysinfo", {})
        dev.reachable = True
        dev.last_seen_utc = utc_now()
        dev.last_refresh_utc = utc_now()
        dev.last_error = None
        if dev.child_id:
            for child in (sysinfo.get("children") or []):
                if str(child.get("id", "")) == dev.child_id:
                    dev.alias = str(child.get("alias", dev.alias))
                    if "state" in child:
                        dev.relay_on = (child.get("state") == 1)
                    break
        else:
            dev.alias = str(sysinfo.get("alias", dev.alias))
            if "relay_state" in sysinfo:
                dev.relay_on = (sysinfo.get("relay_state") == 1)
        return dev

    async def refresh_all_devices(self):
        for cache_id in list(self.devices.keys()):
            await self.refresh_device(cache_id)
        self.last_refresh_utc = utc_now()

    async def set_relay(self, cache_id: str, enabled: bool):
        dev = self.devices.get(cache_id)
        if not dev:
            return {"type": "ack", "action": "kasa_set_relay", "ok": False, "cache_id": cache_id, "error": "Unknown device"}
        obj = await self._udp_query(dev.ip, build_set_relay(enabled, dev.cache_id if dev.child_id else None))
        if obj is None:
            dev.reachable = False
            dev.last_error = "No response to set_relay"
            self.events.add("error", "Kasa set relay failed", cache_id=cache_id, enabled=enabled, error=dev.last_error)
            return {"type": "ack", "action": "kasa_set_relay", "ok": False, "cache_id": cache_id, "requested_enabled": enabled, "device": self._device_brief(dev), "error": dev.last_error}
        await self.refresh_device(cache_id)
        self.events.add("kasa", "Kasa relay changed", cache_id=cache_id, enabled=enabled, relay_on=dev.relay_on)
        return {"type": "ack", "action": "kasa_set_relay", "ok": True, "cache_id": cache_id, "requested_enabled": enabled, "device": self._device_brief(dev)}

    async def get_energy(self, cache_id: str):
        dev = self.devices.get(cache_id)
        if not dev:
            return {"type": "ack", "action": "kasa_get_energy", "ok": False, "cache_id": cache_id, "error": "Unknown device"}
        if not dev.has_emeter:
            return {"type": "ack", "action": "kasa_get_energy", "ok": False, "cache_id": cache_id, "error": "Energy monitoring not supported"}
        obj = await self._udp_query(dev.ip, build_get_realtime(dev.cache_id if dev.child_id else None))
        if obj is None:
            dev.last_error = "No response to get_energy"
            return {"type": "ack", "action": "kasa_get_energy", "ok": False, "cache_id": cache_id, "error": dev.last_error}
        realtime = obj.get("emeter", {}).get("get_realtime", {})
        norm = {"current_ma": realtime.get("current_ma"), "voltage_mv": realtime.get("voltage_mv"), "power_mw": realtime.get("power_mw"), "total_wh": realtime.get("total_wh"), "err_code": realtime.get("err_code"), "raw": realtime}
        dev.energy_json = norm
        dev.last_refresh_utc = utc_now()
        dev.last_error = None
        self.events.add("kasa", "Kasa energy queried", cache_id=cache_id)
        return {"type": "ack", "action": "kasa_get_energy", "ok": True, "cache_id": cache_id, "energy": norm}

    def _device_brief(self, dev: KasaDeviceState):
        return {"cache_id": dev.cache_id, "alias": dev.alias, "reachable": dev.reachable, "relay_on": dev.relay_on, "last_error": dev.last_error}

    def build_status_block(self):
        return {"device_count": len(self.devices), "last_refresh_utc": self.last_refresh_utc, "last_error": self.last_error, "devices": {cid: {"cache_id": d.cache_id, "device_id": d.device_id, "child_id": d.child_id, "alias": d.alias, "model": d.model, "ip": d.ip, "reachable": d.reachable, "relay_on": d.relay_on, "has_emeter": d.has_emeter, "last_seen_utc": d.last_seen_utc, "last_refresh_utc": d.last_refresh_utc, "last_error": d.last_error} for cid, d in self.devices.items()}}

    def build_dashboard_controls(self):
        cache_id = self.roles.get("a1_smartplug")
        if not cache_id:
            return {"a1_power": {"cache_id": None, "alias": self.role_aliases.get("a1_smartplug"), "reachable": False, "relay_on": None, "last_error": "Role unresolved"}}
        dev = self.devices.get(cache_id)
        if not dev:
            return {"a1_power": {"cache_id": cache_id, "alias": self.role_aliases.get("a1_smartplug"), "reachable": False, "relay_on": None, "last_error": "Mapped device not present"}}
        return {"a1_power": self._device_brief(dev)}

    async def periodic_discovery(self, interval_s: float):
        while True:
            await self.discovery_refresh()
            await asyncio.sleep(interval_s)

    async def periodic_status(self, interval_s: float):
        while True:
            await self.refresh_all_devices()
            await asyncio.sleep(interval_s)
