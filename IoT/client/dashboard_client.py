# dashboard_client.py
# version: v2.2
# date: 2026-03-18 08:34 GMT
import json
import sys
from datetime import datetime
from pathlib import Path

from client_tests import ClientTests
from PyQt6 import uic
from PyQt6.QtCore import QTimer, QUrl, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtNetwork import QAbstractSocket
from PyQt6.QtWebSockets import QWebSocket
from PyQt6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


COLOR_OK = "#1f7a1f"
COLOR_WARN = "#d18b00"
COLOR_ERR = "#c62828"
COLOR_NA = "#6e6e6e"

DEFAULT_CLIENT_CONFIG = {
    "backend_ip": "192.168.1.2",
    "backend_port": 8765,
    "relay_packet_timeout_s": 10,
    "client_ping_interval_ms": 2000,
    "client_ping_timeout_ms": 800,
    "cpu_temp_warning": 60,
    "cpu_temp_red": 75,
    "reconnect_interval_ms": 3000,
    "event_limit": 10,
}


class MainWindow(QMainWindow):
    def __init__(self, config_path: str) -> None:
        super().__init__()
        self.config_path = config_path
        self.client_config = self.load_client_config(config_path)
        self.last_status = None
        self.current_severity = "red"
        self.notified_red = False
        self.a1_ping_ok = None
        self.a1_powered_off = None
        self.a1_powering_up = False
        self.allow_close = False

        self.socket = QWebSocket()
        self.socket.connected.connect(self.on_connected)
        self.socket.disconnected.connect(self.on_disconnected)
        self.socket.textMessageReceived.connect(self.on_message)
        self.socket.errorOccurred.connect(self.on_error)

        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setInterval(int(self.client_config["reconnect_interval_ms"]))
        self.reconnect_timer.timeout.connect(self.connect_socket)

        self.client_tests = ClientTests(
            self.client_config["backend_ip"],
            int(self.client_config["client_ping_timeout_ms"]),
        )
        self.ping_timer = QTimer(self)
        self.ping_timer.setInterval(int(self.client_config["client_ping_interval_ms"]))
        self.ping_timer.timeout.connect(self.run_client_ping_test)

        self.request_events_timer = QTimer(self)
        self.request_events_timer.setInterval(15000)
        self.request_events_timer.timeout.connect(self.request_events)

        self.icon_green = self.make_status_icon(QColor(COLOR_OK))
        self.icon_warning = self.make_status_icon(QColor(COLOR_WARN))
        self.icon_red = self.make_status_icon(QColor(COLOR_ERR))
        self.icon_grey = self.make_status_icon(QColor(COLOR_NA))

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setToolTip("IoT Dashboard")
        self.tray_icon.setIcon(self.icon_red)
        self.tray_icon.activated.connect(self.on_tray_activated)

        tray_menu = QMenu(self)
        show_action = QAction("Show Dashboard", self)
        show_action.triggered.connect(self.show_dashboard)
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(self.exit_application)
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

        ui_path = Path(__file__).with_name("dashboard_client.ui")
        uic.loadUi(str(ui_path), self)
        self.setMinimumWidth(640)

        self.connection_target_label.setText(
            f"Backend: {self.client_config['backend_ip']}:{self.client_config['backend_port']}"
        )

        self.run_diagnostics_button.clicked.connect(self.run_diagnostics)
        self.iot_on_button.clicked.connect(
            lambda: self.send_message({"type": "set_iot_internet", "enabled": True})
        )
        self.iot_off_button.clicked.connect(
            lambda: self.send_message({"type": "set_iot_internet", "enabled": False})
        )
        self.clear_errors_button.clicked.connect(
            lambda: self.send_message({"type": "clear_errors"})
        )
        self.A1_toggle_button.clicked.connect(
            lambda: self.send_message({"type": "toggle_a1_power"})
        )
        self.show_recent_events_checkbox.toggled.connect(self.on_toggle_events)

        self.events_table.setHorizontalHeaderLabels(["Time", "Kind", "Message"])
        self.events_table.verticalHeader().setVisible(False)
        self.events_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.events_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.events_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.events_table.setAlternatingRowColors(True)
        self.events_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.events_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.events_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        self.apply_state_label(self.overall_status_label, "red")
        self.apply_state_label(self.relay_status_value, "red")
        self.apply_state_label(self.a1_status_value, "grey")
        self.apply_bool_label(
            self.iot_internet_value,
            None,
            false_text="DISABLED",
            true_text="ENABLED",
        )
        self.events_table.setVisible(False)
        self.events_group.setVisible(False)
        self.show_recent_events_checkbox.setChecked(False)

        self.update_a1_toggle_button(None)
        self.set_connected_state(False)
        QTimer.singleShot(0, self.sync_window_height)
        self.connect_socket()


    def load_client_config(self, path: str) -> dict:
        config = dict(DEFAULT_CLIENT_CONFIG)
        file_path = Path(path)
        if file_path.exists():
            with file_path.open("r", encoding="utf-8") as handle:
                config.update(json.load(handle))
        return config

    def make_status_icon(self, color: QColor) -> QIcon:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(8, 8, 48, 48)
        painter.end()
        return QIcon(pixmap)

    def connect_socket(self) -> None:
        if self.socket.state() in (
            QAbstractSocket.SocketState.ConnectingState,
            QAbstractSocket.SocketState.ConnectedState,
        ):
            return
        url = QUrl(
            f"ws://{self.client_config['backend_ip']}:{self.client_config['backend_port']}"
        )
        self.backend_state_label.setText("Connecting")
        self.socket.open(url)

    def on_connected(self) -> None:
        self.backend_state_label.setText("Backend: Connected")
        self.set_connected_state(True)
        self.reconnect_timer.stop()
        self.request_events_timer.start()
        self.run_client_ping_test()
        self.ping_timer.start()
        self.send_message({"type": "get_status"})
        self.request_events()

    def on_disconnected(self) -> None:
        self.backend_state_label.setText("Backend: Disconnected")
        self.set_connected_state(False)
        self.request_events_timer.stop()
        self.ping_timer.stop()
        self.a1_ping_ok = None
        self.a1_powered_off = None
        self.a1_powering_up = False
        self.alert_status_value_2.setText("No Alert")
        self.alert_detail_value_2.setText("-")
        self.evaluate_and_apply_overall_state(disconnected=True)
        if not self.reconnect_timer.isActive():
            self.reconnect_timer.start()

    def on_error(self, _error) -> None:
        self.backend_state_label.setText(f"Backend: Error - {self.socket.errorString()}")
        self.evaluate_and_apply_overall_state(disconnected=True)

    def on_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return

        msg_type = payload.get("type")

        if msg_type == "status":
            self.last_status = payload
            self.apply_status(payload)
            return

        if msg_type == "events":
            self.populate_events(payload.get("events", []))
            return

        if msg_type == "ack":
            action = payload.get("action")
            if action == "run_diagnostics":
                self.show_diagnostics_result(payload)
                self.send_message({"type": "get_status"})
                self.request_events()
                return
            if action == "toggle_a1_power":
                if payload.get("ok") is False:
                    QMessageBox.warning(self, "A1 Toggle Failed", payload.get("error") or "Unknown backend error")
                self.send_message({"type": "get_status"})
                self.request_events()
                return
            if action in ("set_iot_internet", "clear_errors"):
                self.send_message({"type": "get_status"})
                self.request_events()
                return

        if msg_type == "error":
            QMessageBox.warning(self, "Backend Error", payload.get("message", "Unknown backend error"))

    def on_toggle_events(self, checked: bool) -> None:
        self.events_group.setVisible(checked)
        self.events_table.setVisible(checked)
        self.sync_window_height()

    def sync_window_height(self) -> None:
        current_width = max(self.width(), self.minimumWidth())
        frame_delta = self.frameGeometry().height() - self.geometry().height()
        target_height = self.centralWidget().sizeHint().height() + frame_delta
        self.setMinimumHeight(target_height)
        self.setMaximumHeight(target_height)
        self.resize(current_width, target_height)

    def exit_application(self) -> None:
        self.allow_close = True
        self.reconnect_timer.stop()
        self.request_events_timer.stop()
        self.ping_timer.stop()
        self.socket.abort()
        self.tray_icon.hide()
        QApplication.instance().quit()

    def send_message(self, payload: dict) -> None:
        if self.socket.state() != QAbstractSocket.SocketState.ConnectedState:
            return
        self.socket.sendTextMessage(json.dumps(payload))

    def request_events(self) -> None:
        self.send_message({"type": "get_events", "limit": int(self.client_config["event_limit"])})

    def run_diagnostics(self) -> None:
        self.send_message({"type": "run_diagnostics"})
        self.run_client_ping_test()

    def run_client_ping_test(self) -> None:
        self.a1_ping_ok = self.client_tests.ping()
        if self.last_status is not None:
            self.apply_status(self.last_status)

    def show_diagnostics_result(self, payload: dict) -> None:
        device = payload.get("relay_devices", {}).get("bambu_A1", {})
        text = (
            "Diagnostics result\n\n"
            f"TCP 8883: {self.text_for_bool(device.get('tcp_8883_ok'))}\n"
            f"TCP 990: {self.text_for_bool(device.get('tcp_990_ok'))}\n"
            f"Last check: {self.format_timestamp(device.get('last_check_utc'))}\n"
            f"Error: {device.get('last_error') or '-'}"
        )
        QMessageBox.information(self, "Diagnostics", text)

    def update_a1_power_state(self, a1_power: dict | None) -> None:
        powered_off = None if not a1_power else a1_power.get("powered_off")
        if self.a1_powered_off is True and powered_off is False:
            self.a1_powering_up = True
        elif powered_off is True:
            self.a1_powering_up = False
        self.a1_powered_off = powered_off

    def a1_available(self, relay_running: bool, ping_ok: bool, tcp_8883_ok: bool, tcp_990_ok: bool, relay_age, relay_timeout: float) -> bool:
        return (
            relay_running
            and ping_ok
            and tcp_8883_ok
            and tcp_990_ok
            and relay_age is not None
            and relay_age <= relay_timeout
        )

    def apply_status(self, payload: dict) -> None:
        relay = payload.get("relay", {})
        a1 = payload.get("relay_devices", {}).get("bambu_A1", {})
        a1_power = payload.get("kasa", {}).get("a1_power", {})
        self.update_a1_power_state(a1_power)
        firewall = payload.get("firewall", {})
        system = payload.get("system", {})

        self.update_a1_toggle_button(a1_power)

        relay_running = relay.get("running") is True
        tcp_8883_ok = a1.get("tcp_8883_ok") is True
        tcp_990_ok = a1.get("tcp_990_ok") is True
        relay_age = relay.get("seconds_since_last")
        relay_timeout = self.client_config["relay_packet_timeout_s"]
        ping_ok = self.a1_ping_ok is True

        relay_state = "green" if relay_running else "red"

        if a1_power.get("powered_off") is True:
            a1_state = "grey"
            a1_status_text = "A1 POWER OFF"
            self.set_na_label(self.a1_port_8883_value, "Not available")
            self.set_na_label(self.a1_port_990_value, "Not available")
        elif not relay_running:
            a1_state = "grey"
            a1_status_text = "N/A"
            self.set_na_label(self.a1_port_8883_value, "Not available")
            self.set_na_label(self.a1_port_990_value, "Not available")
        elif tcp_8883_ok and tcp_990_ok and relay_age is not None and relay_age <= relay_timeout:
            self.a1_powering_up = False
            a1_state = "green"
            a1_status_text = None
            self.apply_bool_label(self.a1_port_8883_value, a1.get("tcp_8883_ok"), false_text="FAIL", true_text="OK")
            self.apply_bool_label(self.a1_port_990_value, a1.get("tcp_990_ok"), false_text="FAIL", true_text="OK")
        elif self.a1_powering_up:
            a1_state = "grey"
            a1_status_text = "A1 POWERING UP"
            self.set_na_label(self.a1_port_8883_value, "Not available")
            self.set_na_label(self.a1_port_990_value, "Not available")
        elif not ping_ok:
            a1_state = "grey"
            a1_status_text = "OFFLINE"
            self.set_na_label(self.a1_port_8883_value, "Not available")
            self.set_na_label(self.a1_port_990_value, "Not available")
        elif not tcp_8883_ok and not tcp_990_ok:
            a1_state = "grey"
            a1_status_text = "BOOTING"
            self.set_na_label(self.a1_port_8883_value, "Not available")
            self.set_na_label(self.a1_port_990_value, "Not available")
        elif (tcp_8883_ok or tcp_990_ok) and relay_age is not None and relay_age > relay_timeout:
            a1_state = "red"
            a1_status_text = None
            self.apply_bool_label(self.a1_port_8883_value, a1.get("tcp_8883_ok"), false_text="FAIL", true_text="OK")
            self.apply_bool_label(self.a1_port_990_value, a1.get("tcp_990_ok"), false_text="FAIL", true_text="OK")
        else:
            a1_state = "grey"
            a1_status_text = "N/A"
            self.apply_bool_label(self.a1_port_8883_value, a1.get("tcp_8883_ok"), false_text="FAIL", true_text="OK")
            self.apply_bool_label(self.a1_port_990_value, a1.get("tcp_990_ok"), false_text="FAIL", true_text="OK")

        self.apply_state_label(self.relay_status_value, relay_state)
        self.apply_state_label(self.a1_status_value, a1_state, a1_status_text)

        self.relay_running_value.setText(self.text_for_bool(relay.get("running"), true_text="YES", false_text="NO"))
        self.relay_age_value.setText(self.text_or_dash(relay_age))
        packets = relay.get("packets_total",0)
        restarts = relay.get("restart_count",0)
        self.relay_packets_value.setText(f"{packets} : {restarts}")
        self.relay_error_value.setText(relay.get("last_error") or "-")

        self.a1_last_check_value.setText(self.format_timestamp(a1.get("last_check_utc")))
        self.a1_error_value.setText(a1.get("last_error") or "-")

        temp_c = system.get("temp_c")
        if temp_c is None:
            self.temp_value.setText("-")
            self.temp_value.setStyleSheet("")
        else:
            self.temp_value.setText(f"{temp_c:.1f} C")
            if temp_c > self.client_config["cpu_temp_red"]:
                self.temp_value.setStyleSheet("color: #c62828; font-weight: 700;")
            elif temp_c > self.client_config["cpu_temp_warning"]:
                self.temp_value.setStyleSheet("color: #d18b00; font-weight: 700;")
            else:
                self.temp_value.setStyleSheet("color: #1f7a1f; font-weight: 700;")

        loadavg = system.get("loadavg")
        if isinstance(loadavg, list) and len(loadavg) == 3:
            self.loadavg_value.setText(f"{loadavg[0]:.2f} {loadavg[1]:.2f} {loadavg[2]:.2f}")
        else:
            self.loadavg_value.setText("-")

        self.uptime_value.setText(self.format_uptime(system.get("uptime_s")))
        self.system_error_value.setText(system.get("last_error") or "-")

        iot_enabled = firewall.get("iot_internet_enabled")
        self.apply_bool_label(
            self.iot_internet_value,
            iot_enabled,
            false_text="DISABLED",
            true_text="ENABLED",
            true_color=COLOR_WARN,
            false_color=COLOR_OK,
        )
        self.firewall_last_change_value.setText(self.format_timestamp(firewall.get("last_change_utc")))
        self.firewall_error_value.setText(firewall.get("last_error") or "-")

        self.update_alert_panel(payload.get("events", []))
        self.populate_events(payload.get("events", []))

        self.last_update_label.setText(
            f"Updated: {self.format_timestamp(payload.get('ts'))}"
        )
        self.evaluate_and_apply_overall_state(disconnected=False)

    def evaluate_and_apply_overall_state(self, disconnected: bool) -> None:
        if disconnected or self.last_status is None:
            severity = "red"
        else:
            relay = self.last_status.get("relay", {})
            a1 = self.last_status.get("relay_devices", {}).get("bambu_A1", {})
            firewall = self.last_status.get("firewall", {})
            system = self.last_status.get("system", {})

            relay_running = relay.get("running") is True
            tcp_8883_ok = a1.get("tcp_8883_ok") is True
            tcp_990_ok = a1.get("tcp_990_ok") is True
            relay_age = relay.get("seconds_since_last")
            relay_timeout = self.client_config["relay_packet_timeout_s"]
            temp_c = system.get("temp_c")
            ping_ok = self.a1_ping_ok is True

            if not relay_running:
                severity = "red"
            elif ping_ok and (tcp_8883_ok or tcp_990_ok) and relay_age is not None and relay_age > relay_timeout:
                severity = "red"
            elif relay.get("last_error") or a1.get("last_error") or firewall.get("last_error") or system.get("last_error"):
                severity = "red"
            elif temp_c is not None and temp_c > self.client_config["cpu_temp_red"]:
                severity = "red"
            elif firewall.get("iot_internet_enabled") is True:
                severity = "warning"
            elif temp_c is not None and temp_c > self.client_config["cpu_temp_warning"]:
                severity = "warning"
            else:
                severity = "green"

        self.apply_state_label(self.overall_status_label, severity)
        self.set_tray_state(severity)

    def set_tray_state(self, severity: str) -> None:
        if severity == "green":
            self.tray_icon.setIcon(self.icon_green)
        elif severity == "warning":
            self.tray_icon.setIcon(self.icon_warning)
        elif severity == "grey":
            self.tray_icon.setIcon(self.icon_grey)
        else:
            self.tray_icon.setIcon(self.icon_red)

        if severity == "red" and self.current_severity != "red" and self.tray_icon.supportsMessages():
            self.tray_icon.showMessage(
                "IoT Dashboard Alert",
                "Red alert state detected. Open the dashboard.",
                QSystemTrayIcon.MessageIcon.Warning,
                5000,
            )
        self.current_severity = severity

    def apply_state_label(self, label: QLabel, state: str, display_text: str | None = None) -> None:
        text = display_text if display_text is not None else state.upper()
        if state == "green":
            label.setText(text)
            label.setStyleSheet(
                f"background-color: {COLOR_OK}; color: white; font-weight: 700; "
                "padding: 4px 8px; border-radius: 4px;"
            )
        elif state == "warning":
            label.setText(text)
            label.setStyleSheet(
                f"background-color: {COLOR_WARN}; color: black; font-weight: 700; "
                "padding: 4px 8px; border-radius: 4px;"
            )
        elif state == "grey":
            label.setText("N/A")
            label.setStyleSheet(
                f"background-color: {COLOR_NA}; color: white; font-weight: 700; "
                "padding: 4px 8px; border-radius: 4px;"
            )
        else:
            label.setText("RED")
            label.setStyleSheet(
                f"background-color: {COLOR_ERR}; color: white; font-weight: 700; "
                "padding: 4px 8px; border-radius: 4px;"
            )

    def set_na_label(self, label: QLabel, text: str = "Not available") -> None:
        label.setText(text)
        label.setStyleSheet(f"color: {COLOR_NA}; font-weight: 700;")

    def apply_bool_label(
        self,
        label: QLabel,
        value,
        false_text: str,
        true_text: str,
        true_color: str = COLOR_OK,
        false_color: str = COLOR_ERR,
    ) -> None:
        if value is True:
            label.setText(true_text)
            label.setStyleSheet(f"color: {true_color}; font-weight: 700;")
        elif value is False:
            label.setText(false_text)
            label.setStyleSheet(f"color: {false_color}; font-weight: 700;")
        else:
            label.setText("-")
            label.setStyleSheet("")

    def update_a1_toggle_button(self, a1_power: dict | None) -> None:
        if not a1_power:
            self.A1_toggle_button.setText("A1 Power")
            return
        if a1_power.get("relay_on") is True:
            self.A1_toggle_button.setText("A1 Power Off")
        elif a1_power.get("relay_on") is False:
            self.A1_toggle_button.setText("A1 Power On")
        else:
            self.A1_toggle_button.setText("A1 Power")

    def format_event_message(self, event: dict) -> str:
        message = str(event.get("message", "-"))
        causes = event.get("causes")
        if isinstance(causes, dict) and causes:
            details = ", ".join(f"{key}: {value}" for key, value in causes.items())
            return f"{message} ({details})"
        return message

    def update_alert_panel(self, events: list) -> None:
        status_text = "No Alert"
        detail_text = "-"
        for event in reversed(list(events)):
            message = str(event.get("message", ""))
            print("wha alert:", message)
            if message == "Alert active":
                status_text = "Alert Active"
                causes = event.get("causes")
                if isinstance(causes, dict) and causes:
                    detail_text = ", ".join(f"{key}: {value}" for key, value in causes.items())
                else:
                    detail_text = self.format_event_message(event)
                break
            if message == "Alert cleared":
                break
        self.alert_status_value_2.setText(status_text)
        self.alert_detail_value_2.setText(detail_text)

    def populate_events(self, events: list) -> None:
        self.events_table.setRowCount(0)
        recent = list(events)[-int(self.client_config["event_limit"]):]
        recent.reverse()
        for row_index, event in enumerate(recent):
            self.events_table.insertRow(row_index)
            time_item = QTableWidgetItem(self.format_timestamp(event.get("ts")))
            kind_item = QTableWidgetItem(str(event.get("kind", "-")))
            message_item = QTableWidgetItem(self.format_event_message(event))
            self.events_table.setItem(row_index, 0, time_item)
            self.events_table.setItem(row_index, 1, kind_item)
            self.events_table.setItem(row_index, 2, message_item)

    def set_connected_state(self, connected: bool) -> None:
        self.run_diagnostics_button.setEnabled(connected)
        self.iot_on_button.setEnabled(connected)
        self.iot_off_button.setEnabled(connected)
        self.clear_errors_button.setEnabled(connected)
        self.A1_toggle_button.setEnabled(connected)

    def on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_dashboard()

    def show_dashboard(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        if self.allow_close:
            event.accept()
            return
        self.hide()
        event.ignore()
        if self.tray_icon.supportsMessages():
            self.tray_icon.showMessage(
                "IoT Dashboard",
                "Dashboard minimized to the system tray.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    def text_for_bool(self, value, true_text: str = "True", false_text: str = "False") -> str:
        if value is True:
            return true_text
        if value is False:
            return false_text
        return "-"

    def text_or_dash(self, value) -> str:
        return "-" if value is None else str(value)

    def format_timestamp(self, value) -> str:
        if not value:
            return "-"
        if not isinstance(value, str):
            return str(value)
        try:
            dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            return dt.strftime("%H:%M:%S")
        except ValueError:
            return value

    def format_uptime(self, seconds) -> str:
        if seconds is None:
            return "-"
        total = int(seconds)
        days = total // 86400
        hours = (total % 86400) // 3600
        minutes = (total % 3600) // 60
        if days > 0:
            return f"{days}d {hours:02d}:{minutes:02d}"
        return f"{hours:02d}:{minutes:02d}"


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    config_path = "client_config.json"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    window = MainWindow(config_path)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
