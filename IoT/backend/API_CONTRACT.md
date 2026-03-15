# IoT Backend WebSocket API Contract

Transport:
- WebSocket
- Default URL: ws://192.168.1.2:8765

Request message types:
- {"type":"get_status"}
- {"type":"get_events","limit":50}
- {"type":"run_diagnostics"}
- {"type":"set_iot_internet","enabled":true|false}
- {"type":"set_debug","enabled":true|false}
- {"type":"clear_errors"}
- {"type":"ping"}

Response message types:
- status
- events
- ack
- pong
- error

Status schema:
- type = "status"
- schema_version = 1
- ts
- config
- relay
- relay_devices
- firewall
- system
- events
