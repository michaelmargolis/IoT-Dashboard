#!/usr/bin/env python3
from __future__ import annotations
import argparse, asyncio, collections, json, time
from typing import Any, Deque, Dict
import websockets
from kasa_manager import KasaManager

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

async def safe_send(websocket, payload):
    try:
        await websocket.send(json.dumps(payload))
        return True
    except websockets.exceptions.ConnectionClosed:
        return False

class KasaBackend:
    def __init__(self, cfg):
        self.cfg = cfg
        self.events = EventLog(maxlen=cfg["event_log_max"])
        self.kasa = KasaManager(cfg, self.events)
        self.clients = set()
        

    def build_status(self):
        return {
            "type": "status",
            "schema_version": 1,
            "ts": utc_now(),
            "config": {
                "ws_port": self.cfg["ws_port"],
                "iot_ip": self.cfg["iot_ip"],
                "kasa_discovery_interval_s": self.cfg["kasa_discovery_interval_s"],
                "kasa_status_interval_s": self.cfg["kasa_status_interval_s"],
            },
            "kasa": self.kasa.build_status_block(),
            "dashboard_controls": self.kasa.build_dashboard_controls(),
            "events": self.events.tail(self.cfg["event_tail_default"]),
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
        if t == "get_status":
            await safe_send(websocket, self.build_status()); return
        if t == "get_events":
            limit = int(msg.get("limit", self.cfg["event_tail_default"]))
            await safe_send(websocket, {"type": "events", "events": self.events.tail(limit)}); return
        if t == "kasa_discovery_refresh":
            status = await self.kasa.discovery_refresh()
            await safe_send(websocket, {"type": "ack", "action": "kasa_discovery_refresh", "ok": True, "kasa": status}); return
        if t == "kasa_set_relay":
            await safe_send(websocket, await self.kasa.set_relay(msg.get("cache_id"), bool(msg.get("enabled")))); return
        if t == "kasa_get_energy":
            await safe_send(websocket, await self.kasa.get_energy(msg.get("cache_id"))); return
        if t == "kasa_get_a1_power":
            await safe_send(websocket, {"type": "ack", "action": "kasa_get_a1_power", "ok": True, "a1_power": self.kasa.build_a1_power_status()}); return
        if t == "kasa_toggle_a1_power":
            await safe_send(websocket, await self.kasa.toggle_a1_power()); return
        await safe_send(websocket, {"type": "error", "message": f"unknown message type: {t}"})

    async def ws_handler(self, websocket):
        remote = websocket.remote_address[0] if websocket.remote_address else ""
        print("Client connect attempt from", remote, flush=True)
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
        print("Kasa backend starting on", self.cfg["ws_host"], self.cfg["ws_port"], flush=True)
        await self.kasa.discovery_refresh()
        asyncio.create_task(self.kasa.periodic_discovery(self.cfg["kasa_discovery_interval_s"]))
        asyncio.create_task(self.kasa.periodic_status(self.cfg["kasa_status_interval_s"]))
        asyncio.create_task(self.broadcast_loop())
        async with websockets.serve(self.ws_handler, self.cfg["ws_host"], self.cfg["ws_port"], ping_interval=20, ping_timeout=20):
            self.events.add("server", "Kasa WebSocket server started", host=self.cfg["ws_host"], port=self.cfg["ws_port"])
            await asyncio.Future()

DEFAULT_CONFIG = {
    "ws_host": "0.0.0.0",
    "ws_port": 8775,
    "allowed_client_ips": [], # ["192.168.1.50"],
    "iot_ip": "192.168.50.1",
    "iot_broadcast": "255.255.255.255",
    "kasa_discovery_interval_s": 300,
    "kasa_status_interval_s": 15,
    "kasa_command_timeout_s": 3,
    "kasa_role_aliases": {"a1_smartplug": "A1 Smartplug"},
    "kasa_roles": {"a1_smartplug": None},
    "broadcast_interval_s": 1.0,
    "event_log_max": 100,
    "event_tail_default": 20
}
def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path:
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
            print("using config at", path)
    return cfg
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    args = ap.parse_args()
    await KasaBackend(load_config(args.config)).run()
if __name__ == "__main__":
    asyncio.run(main())
