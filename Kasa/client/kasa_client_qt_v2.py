#!/usr/bin/env python3

# PyQt6 test client for the standalone Kasa backend.
# Connects to the Kasa prototype server, shows discovered devices, preserves known devices,
# highlights alias conflicts, and provides controls for discovery, relay toggling,
# energy queries, and device detail inspection.

from __future__ import annotations
import json
import sys
from pathlib import Path
from PyQt6 import uic
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtNetwork import QAbstractSocket
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QDialog, QMainWindow, QMessageBox, QTextEdit, QVBoxLayout, QTableWidgetItem
from PyQt6.QtWebSockets import QWebSocket


class ObjectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModal(False)
        layout = QVBoxLayout(self)
        self.view = QTextEdit(self)
        self.view.setReadOnly(True)
        layout.addWidget(self.view)
        self.resize(700, 500)

    def show_payload(self, title: str, payload):
        self.setWindowTitle(title)
        self.view.clear()
        if isinstance(payload, (dict, list)):
            self.view.setPlainText(json.dumps(payload, indent=2))
        else:
            self.view.setPlainText(str(payload))
        self.show()
        self.raise_()
        self.activateWindow()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        ui_path = Path(__file__).with_name("kasa_client_qt_v2.ui")
        uic.loadUi(str(ui_path), self)

        self.socket = QWebSocket()
        self.socket.connected.connect(self.on_connected)
        self.socket.disconnected.connect(self.on_disconnected)
        self.socket.textMessageReceived.connect(self.on_message)
        self.socket.errorOccurred.connect(self.on_error)

        self.last_status = {}
        self.selected_cache_id = None
        self.console_event_keys = set()
        self.table_rows = []
        self.known_devices_path = Path(__file__).with_name("kasa_saved_devices.json")
        self.object_dialog = ObjectDialog(self)
        self.shown_alias_conflicts = set()

        self.hostEdit.setText("192.168.1.2")
        self.portEdit.setText("8775")
        self.plugsTable.setColumnCount(4)
        self.plugsTable.setHorizontalHeaderLabels(["Alias", "Model", "Relay State", "IP Address"])
        self.plugsTable.horizontalHeader().setStretchLastSection(True)
        self.plugsTable.verticalHeader().setVisible(False)
        self.plugsTable.setSelectionBehavior(self.plugsTable.SelectionBehavior.SelectRows)
        self.plugsTable.setSelectionMode(self.plugsTable.SelectionMode.SingleSelection)
        self.plugsTable.setEditTriggers(self.plugsTable.EditTrigger.NoEditTriggers)
        self.plugsTable.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.plugsTable.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.plugsTable.setStyleSheet("QTableWidget::item:selected { background-color: yellow; color: black; }")

        self.connectButton.clicked.connect(self.connect_socket)
        self.disconnectButton.clicked.connect(self.disconnect_socket)
        self.toggleA1Button.clicked.connect(self.toggle_a1)
        self.toggleRelayButton.clicked.connect(self.toggle_selected_plug)
        self.energyButton.clicked.connect(self.get_selected_energy)
        self.detailsButton.clicked.connect(self.show_selected_details)
        self.refreshButton.clicked.connect(lambda: self.send_message({"type": "kasa_discovery_refresh"}))
        self.plugsTable.itemSelectionChanged.connect(self.on_selection_changed)

        self.populate_table()
        self.set_connected_state(False)
        self.resize(1100, 760)

    def connect_socket(self):
        self.socket.open(QUrl(f"ws://{self.hostEdit.text().strip()}:{self.portEdit.text().strip()}"))

    def disconnect_socket(self):
        self.socket.close()

    def on_connected(self):
        self.connectionStatusLabel.setText("Connected")
        self.set_connected_state(True)
        self.send_message({"type": "kasa_discovery_refresh"})

    def on_disconnected(self):
        self.connectionStatusLabel.setText("Disconnected")
        self.set_connected_state(False)

    def on_error(self, error):
        self.connectionStatusLabel.setText(f"Error: {self.socket.errorString()}")

    def on_message(self, message):
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            return

        if self.showJsonCheckBox.isChecked():
            print(json.dumps(parsed, indent=2))

        if self.showEventsCheckBox.isChecked():
            for evt in parsed.get("events", []):
                key = json.dumps(evt, sort_keys=True)
                if key not in self.console_event_keys:
                    self.console_event_keys.add(key)
                    print(json.dumps(evt, indent=2))

        if parsed.get("type") == "status":
            self.last_status = parsed
            self.sync_saved_with_discovered()
            self.check_alias_conflicts()
            self.populate_table()
        elif parsed.get("type") == "ack" and parsed.get("action") == "kasa_get_energy":
            title = f"Energy - {self.selected_cache_id}" if self.selected_cache_id else "Energy"
            self.object_dialog.show_payload(title, parsed.get("energy", {}))
        elif parsed.get("type") == "ack" and parsed.get("action") == "kasa_discovery_refresh":
            if parsed.get("kasa"):
                self.last_status["kasa"] = parsed.get("kasa")
            self.sync_saved_with_discovered()
            self.check_alias_conflicts()
            self.populate_table()

    def saved_identity(self, dev):
        if not isinstance(dev, dict):
            return None
        return dev.get("cache_id")

    def normalize_saved_device(self, dev):
        identity = self.saved_identity(dev)
        if not identity:
            return None
        return {
            "identity": identity,
            "cache_id": dev.get("cache_id"),
            "device_id": dev.get("device_id"),
            "child_id": dev.get("child_id"),
            "alias": dev.get("alias"),
            "model": dev.get("model"),
            "ip": dev.get("ip"),
            "has_emeter": dev.get("has_emeter"),
        }

    def load_known_devices(self):
        if not self.known_devices_path.exists():
            return {}
        try:
            payload = json.loads(self.known_devices_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        devices = payload.get("devices", []) if isinstance(payload, dict) else []
        out = {}
        for dev in devices:
            norm = self.normalize_saved_device(dev)
            if norm:
                out[norm["identity"]] = norm
        return out

    def write_known_devices(self, known):
        data = {
            "devices": sorted(
                known.values(),
                key=lambda d: ((d.get("alias") or "").lower(), d.get("identity") or "")
            )
        }
        self.known_devices_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def sync_saved_with_discovered(self):
        known = self.load_known_devices()
        discovered = self.last_status.get("kasa", {}).get("devices", {})
        changed = False
        for dev in discovered.values():
            norm = self.normalize_saved_device(dev)
            if not norm:
                continue
            current = known.get(norm["identity"])
            if current != norm:
                known[norm["identity"]] = norm
                changed = True
        if changed:
            self.write_known_devices(known)

    def check_alias_conflicts(self):
        discovered = self.last_status.get("kasa", {}).get("devices", {})
        alias_map = {}
        conflicts = []
        for dev in discovered.values():
            alias = (dev.get("alias") or "").strip()
            identity = self.saved_identity(dev)
            if not alias or not identity:
                continue
            prev = alias_map.get(alias)
            if prev and prev != identity:
                pair = tuple(sorted([prev, identity]))
                token = (alias, pair)
                if token not in self.shown_alias_conflicts:
                    self.shown_alias_conflicts.add(token)
                    conflicts.append((alias, prev, identity))
            else:
                alias_map[alias] = identity
        if conflicts:
            lines = []
            for alias, first_id, second_id in conflicts:
                lines.append(f"Alias '{alias}' is used by multiple devices:\n{first_id}\n{second_id}")
            QMessageBox.warning(self, "Alias conflict", "\n\n".join(lines))

    def populate_table(self):
        known = self.load_known_devices()
        discovered = self.last_status.get("kasa", {}).get("devices", {})

        merged = {}
        for identity, dev in known.items():
            row = dict(dev)
            row["available"] = False
            row["relay_on"] = None
            merged[identity] = row
        for dev in discovered.values():
            identity = self.saved_identity(dev)
            if not identity:
                continue
            row = dict(merged.get(identity, {}))
            row.update(dev)
            row["identity"] = identity
            row["available"] = True
            merged[identity] = row

        rows = sorted(merged.values(), key=lambda d: ((d.get("alias") or "").lower(), d.get("identity") or ""))
        self.table_rows = rows
        self.plugsTable.setRowCount(len(rows))

        for row, dev in enumerate(rows):
            available = bool(dev.get("available"))
            relay_on = dev.get("relay_on")
            relay_text = "-"
            if available and relay_on is not None:
                relay_text = "On" if relay_on else "Off"
            values = [
                dev.get("alias") or "",
                dev.get("model") or "",
                relay_text,
                dev.get("ip") or "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, dev.get("cache_id"))
                if not available:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
                    item.setForeground(QColor("gray"))
                    item.setBackground(QColor("#e6e6e6"))
                self.plugsTable.setItem(row, col, item)

        self.restore_selection()
        self.update_table_height()
        self.update_action_buttons()

    def update_table_height(self):
        height = self.plugsTable.horizontalHeader().height() + (self.plugsTable.frameWidth() * 2)
        for row in range(self.plugsTable.rowCount()):
            height += self.plugsTable.rowHeight(row)
        self.plugsTable.setFixedHeight(max(height + 2, 80))

    def restore_selection(self):
        if not self.selected_cache_id:
            self.plugsTable.clearSelection()
            return
        for row, dev in enumerate(self.table_rows):
            if dev.get("cache_id") == self.selected_cache_id and dev.get("available"):
                self.plugsTable.selectRow(row)
                return
        self.selected_cache_id = None
        self.plugsTable.clearSelection()

    def on_selection_changed(self):
        selected = self.plugsTable.selectedItems()
        if not selected:
            self.selected_cache_id = None
            self.update_action_buttons()
            return
        cache_id = selected[0].data(Qt.ItemDataRole.UserRole)
        row = self.plugsTable.currentRow()
        if row < 0 or row >= len(self.table_rows) or not self.table_rows[row].get("available"):
            self.selected_cache_id = None
            self.plugsTable.clearSelection()
        else:
            self.selected_cache_id = cache_id
        self.update_action_buttons()

    def get_selected_device(self):
        if not self.selected_cache_id:
            return None
        return self.last_status.get("kasa", {}).get("devices", {}).get(self.selected_cache_id)

    def update_action_buttons(self):
        connected = self.socket.state() == QAbstractSocket.SocketState.ConnectedState
        has_selection = self.get_selected_device() is not None
        self.toggleRelayButton.setEnabled(connected and has_selection)
        self.energyButton.setEnabled(connected and has_selection)
        self.detailsButton.setEnabled(has_selection)
        self.toggleA1Button.setEnabled(connected)
        self.refreshButton.setEnabled(connected)

    def toggle_a1(self):
        a1 = self.last_status.get("dashboard_controls", {}).get("a1_power", {})
        cache_id = a1.get("cache_id")
        relay_on = a1.get("relay_on")
        if not cache_id or relay_on is None:
            QMessageBox.information(self, "Unavailable", "A1 plug role is not resolved.")
            return
        self.send_message({"type": "kasa_set_relay", "cache_id": cache_id, "enabled": (not relay_on)})

    def toggle_selected_plug(self):
        dev = self.get_selected_device()
        if not dev:
            QMessageBox.information(self, "No selection", "Select a discovered device first.")
            return
        relay_on = dev.get("relay_on")
        if relay_on is None:
            QMessageBox.information(self, "Unavailable", "Selected device relay state is unavailable.")
            return
        self.send_message({"type": "kasa_set_relay", "cache_id": self.selected_cache_id, "enabled": (not relay_on)})

    def get_selected_energy(self):
        dev = self.get_selected_device()
        if not dev:
            QMessageBox.information(self, "No selection", "Select a discovered device first.")
            return
        self.send_message({"type": "kasa_get_energy", "cache_id": self.selected_cache_id})

    def show_selected_details(self):
        dev = self.get_selected_device()
        if not dev:
            QMessageBox.information(self, "No selection", "Select a discovered device first.")
            return
        self.object_dialog.show_payload(dev.get("alias") or self.selected_cache_id, dev)

    def send_message(self, payload):
        if self.socket.state() != QAbstractSocket.SocketState.ConnectedState:
            QMessageBox.information(self, "Not connected", "Connect first.")
            return
        self.socket.sendTextMessage(json.dumps(payload))

    def set_connected_state(self, connected):
        self.connectButton.setEnabled(not connected)
        self.disconnectButton.setEnabled(connected)
        self.showEventsCheckBox.setEnabled(True)
        self.showJsonCheckBox.setEnabled(True)
        self.update_action_buttons()


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
