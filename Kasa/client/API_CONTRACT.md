# Kasa Prototype API Contract

Transport: WebSocket
Default URL: ws://192.168.1.2:8775

Commands:
- get_status
- get_events
- kasa_discovery_refresh
- kasa_set_relay
- kasa_get_energy

Device keying:
- standalone plug => cache_id = device_id
- strip child => cache_id = parent_device_id:child_id
