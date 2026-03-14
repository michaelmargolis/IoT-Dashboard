#!/usr/bin/env python3
from __future__ import annotations
import json
from typing import Dict, Any

DISCOVERY_PORT = 9999

def encrypt(payload: str) -> bytes:
    key = 171
    out = bytearray()
    for ch in payload.encode("utf-8"):
        enc = key ^ ch
        key = enc
        out.append(enc)
    return bytes(out)

def decrypt(payload: bytes) -> str:
    key = 171
    out = bytearray()
    for b in payload:
        dec = key ^ b
        key = b
        out.append(dec)
    return out.decode("utf-8", errors="ignore")

def to_packet(obj: Dict[str, Any]) -> bytes:
    return encrypt(json.dumps(obj, separators=(",", ":")))

def from_packet(payload: bytes) -> Dict[str, Any]:
    return json.loads(decrypt(payload))

def build_discovery() -> bytes:
    return to_packet({"system": {"get_sysinfo": {}}})

def build_get_sysinfo() -> bytes:
    return to_packet({"system": {"get_sysinfo": {}}})

def build_set_relay(enabled: bool, child_id: str | None = None) -> bytes:
    state = 1 if enabled else 0
    if child_id:
        payload = {"context": {"child_ids": [child_id]}, "system": {"set_relay_state": {"state": state}}} 
    else:
        payload = {"system": {"set_relay_state": {"state": state}}}
    print(payload)     
    return to_packet(payload)

def build_get_realtime(child_id: str | None = None) -> bytes:
    if child_id:
        return to_packet({"context": {"child_ids": [child_id]}, "emeter": {"get_realtime": {}}})
    return to_packet({"emeter": {"get_realtime": {}}})
