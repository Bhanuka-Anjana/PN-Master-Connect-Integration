"""Local MQTT console. Run python app.py, or python app.py --demo for offline UI."""
from __future__ import annotations

import argparse
from collections import deque
import copy
import getpass
import json
import logging
import math
import os
import secrets
import ssl
import threading
import time
import uuid

from flask import Flask, Response, jsonify, request, send_from_directory
import config

LOG = logging.getLogger("pn_connect")
TOPICS = {"environment", "analog-inputs", "contact-inputs", "outputs", "relay",
          "thermistor-probe", "precision-probe"}


class DeviceStore:
    def __init__(self, prefix=config.MQTT_TOPIC_PREFIX, mode="live"):
        if not prefix or any(char in prefix for char in "+#\0"):
            raise ValueError("Topic prefix cannot be empty or contain wildcards/NUL")
        self.prefix = prefix.rstrip("/")
        self.mode = mode
        self.lock = threading.RLock()
        self.connected = False
        self.connection_detail = "Connecting to broker" if mode == "live" else "Starting local simulation"
        self.started = time.time()
        self.latest = {}
        self.history = {}
        self.events = deque(maxlen=80)
        self.commands = deque(maxlen=40)
        self.message_count = 0
        self.last_message = None
        self.csrf = secrets.token_urlsafe(32)

    def event(self, kind, detail, topic=""):
        self.events.appendleft({"at": time.time(), "kind": kind, "detail": detail[:300], "topic": topic})

    def connection(self, connected, detail):
        with self.lock:
            self.connected = connected
            self.connection_detail = detail
            self.event("connection", detail)

    def ingest(self, topic, raw, retained=False):
        root = self.prefix + "/"
        if not topic.startswith(root) or len(raw) > 262144:
            return False
        suffix = topic[len(root):]
        if suffix not in TOPICS:
            parts = suffix.split("/")
            if len(parts) != 2 or parts[0] != "modbus-values" or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 10:
                return False
        try:
            payload = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
            if not isinstance(payload, dict):
                raise ValueError("Telemetry must be a JSON object")
            if "channels" in payload and (not isinstance(payload["channels"], list) or len(payload["channels"]) > 16
                                           or any(not isinstance(channel, dict) for channel in payload["channels"])):
                raise ValueError("Malformed channel array")
            if suffix.startswith("modbus-values/") and (not isinstance(payload.get("registers"), dict)
                                                         or len(payload["registers"]) > 150):
                raise ValueError("Malformed Modbus registers")
        except (ValueError, UnicodeError, TypeError):
            with self.lock:
                self.event("warning", "Ignored malformed telemetry", suffix)
            return False
        now = time.time()
        with self.lock:
            self.latest[suffix] = {"payload": payload, "at": now, "retained": retained}
            self.message_count += 1
            self.last_message = now
            self.event("rx", "Telemetry received", suffix)
            if not retained:
                self._record_history(suffix, payload, now)
            for command in self.commands:
                if command["status"] != "sent" or retained or now < command["at"]:
                    continue
                if now - command["at"] > config.COMMAND_TIMEOUT_S:
                    command["status"] = "timeout"
                    continue
                state = None
                if suffix == "outputs" and command["target"] == "do":
                    state = next((channel.get("active") for channel in payload.get("channels", [])
                                  if channel.get("channel") == command["channel"]), None)
                elif suffix == "relay" and command["target"] == "relay":
                    state = payload.get("state")
                if type(state) is bool and state == command["state"]:
                    command["status"] = "confirmed"
                    self.event("confirmed", f"{command['label']} reported {'ON' if state else 'OFF'}", suffix)
        return True

    def _record_history(self, suffix, payload, now):
        values = {}
        for name in ("temperature_c", "humidity_percent"):
            values[name] = payload.get(name)
        if suffix == "analog-inputs":
            values = {str(channel.get("channel")): channel.get("engineering_value", channel.get("input_value"))
                      for channel in payload.get("channels", [])}
        elif suffix.startswith("modbus-values/"):
            values = {str(name)[:64]: register.get("value") for name, register in payload.get("registers", {}).items()
                      if isinstance(register, dict) and register.get("valid") is True}
        for name, value in values.items():
            if type(value) not in (int, float):
                continue
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                continue
            key = f"{suffix}.{name}"
            if key not in self.history and len(self.history) >= 256:
                continue
            self.history.setdefault(key, deque(maxlen=config.HISTORY_LIMIT)).append({"at": now, "value": value})

    def snapshot(self, include_history=False):
        with self.lock:
            now = time.time()
            for command in self.commands:
                if command["status"] == "sent" and now - command["at"] > config.COMMAND_TIMEOUT_S:
                    command["status"] = "timeout"
            live_at = max((item["at"] for item in self.latest.values() if not item["retained"]), default=None)
            result = {"mode": self.mode, "connected": self.connected, "connection_detail": self.connection_detail,
                      "topic_prefix": self.prefix, "broker": config.MQTT_HOST, "port": config.MQTT_PORT,
                      "message_count": self.message_count, "last_message": self.last_message,
                      "fresh": bool(live_at and now - live_at < config.STALE_AFTER_S),
                      "now": now, "started": self.started, "csrf": self.csrf,
                      "latest": copy.deepcopy(self.latest), "events": list(self.events),
                      "commands": copy.deepcopy(list(self.commands)), "stale_after_s": config.STALE_AFTER_S}
            if include_history:
                result["history"] = {key: list(value) for key, value in self.history.items()}
            return result

    def prepare_command(self, target, channel, state):
        if target not in ("do", "relay") or type(state) is not bool:
            raise ValueError("Target must be do/relay and state must be a JSON boolean")
        if target == "do" and (type(channel) is not int or not 1 <= channel <= 3):
            raise ValueError("DO channel must be an integer from 1 to 3")
        with self.lock:
            if not self.connected:
                raise ConnectionError("MQTT is not connected")
            suffix = "outputs" if target == "do" else "relay"
            latest = self.latest.get(suffix)
            if not latest or latest["retained"] or time.time() - latest["at"] > config.STALE_AFTER_S:
                raise ConnectionError("Waiting for fresh device state before enabling controls")
            if target == "do":
                actual = next((item.get("active") for item in latest["payload"].get("channels", [])
                               if item.get("channel") == channel), None)
            else:
                actual = latest["payload"].get("state")
            if type(actual) is not bool:
                raise ConnectionError("Device has not reported a valid actuator state")
            for command in self.commands:
                if command["target"] == target and command["channel"] == channel and command["status"] == "sent":
                    if time.time() - command["at"] <= config.COMMAND_TIMEOUT_S:
                        raise ConnectionError("A command for this output is still pending")
                    command["status"] = "timeout"
            command = {"id": uuid.uuid4().hex, "target": target, "channel": channel, "state": state,
                       "label": f"DO {channel}" if target == "do" else "Relay", "at": time.time(), "status": "sent"}
            self.commands.appendleft(command)
            self.event("tx", f"{command['label']} requested {'ON' if state else 'OFF'}", f"{target}/set")
            return copy.deepcopy(command)


class MqttBridge:
    def __init__(self, store, password):
        import paho.mqtt.client as mqtt
        self.store = store
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"pn-connect-{uuid.uuid4().hex[:10]}", protocol=mqtt.MQTTv5)
        self.client.username_pw_set(config.MQTT_USERNAME, password)
        self.client.tls_set_context(ssl.create_default_context())
        self.client.reconnect_delay_set(1, 30)
        self.client.on_connect = self.on_connect
        self.client.on_subscribe = self.on_subscribe
        self.client.on_disconnect = self.on_disconnect
        self.client.on_connect_fail = lambda *_: self.store.connection(False, "Connection failed; retrying")
        self.client.on_message = lambda client, userdata, msg: self.store.ingest(msg.topic, msg.payload, msg.retain)

    def start(self):
        self.client.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=60)
        self.client.loop_start()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()

    def on_connect(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            self.store.connection(False, f"Broker rejected connection: {reason}")
            return
        result, _ = client.subscribe(self.store.prefix + "/#", qos=1)
        if result != 0:
            self.store.connection(False, "Could not subscribe to device topics")

    def on_subscribe(self, client, userdata, mid, reasons, properties):
        if any(reason.is_failure for reason in reasons):
            self.store.connection(False, "Broker denied subscription; check master2 topic permissions")
        else:
            self.store.connection(True, "Subscribed to device telemetry")

    def on_disconnect(self, client, userdata, flags, reason, properties):
        self.store.connection(False, "Disconnected; reconnecting" if reason.is_failure else "Disconnected")

    def send(self, command):
        if not self.client.is_connected():
            raise ConnectionError("MQTT disconnected; command was not sent")
        payload = {"state": command["state"]}
        if command["target"] == "do":
            payload["channel"] = command["channel"]
        # QoS 0 deliberately avoids offline command queues/replay after reconnects.
        info = self.client.publish(f"{self.store.prefix}/{command['target']}/set",
                                   json.dumps(payload), qos=0, retain=False)
        if info.rc != 0:
            raise ConnectionError("MQTT transport rejected the command")


class DemoBridge:
    """Explicit offline mode: no broker connection or external publications."""
    def __init__(self, store):
        self.store = store
        self.stop_event = threading.Event()
        self.outputs = [False, False, False]
        self.relay = False
        self.sequence = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.store.connection(True, "Local demo • no broker traffic")
        for index in range(45):
            self.tick(at=time.time() - (45 - index) * 2)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)

    def send(self, command):
        if command["target"] == "do":
            self.outputs[command["channel"] - 1] = command["state"]
        else:
            self.relay = command["state"]
        threading.Timer(0.35, self.tick).start()

    def _run(self):
        self.tick()
        while not self.stop_event.wait(2):
            self.tick()

    def tick(self, at=None):
        self.sequence += 1
        n = self.sequence
        common = {"device_id": "virtual-master-001", "site_id": "test-site", "sequence": n,
                  "timestamp_us": n * 2000000}
        values = {
            "environment": {"temperature_c": round(27 + math.sin(n / 8) * 2.2, 2),
                            "humidity_percent": round(55 + math.sin(n / 11) * 8, 1)},
            "analog-inputs": {"channels": [{"channel": i, "name": name, "unit": unit,
                                            "engineering_value": round(maximum * (.55 + math.sin(n / 9 + i) * .25), 2),
                                            "percent": round(55 + math.sin(n / 9 + i) * 25, 1),
                                            "input_value": round(5.5 + math.sin(n / 9 + i) * 2.5, 2)}
                                           for i, (name, unit, maximum) in enumerate([
                                               ("Tank level", "%", 100), ("Pressure", "bar", 10),
                                               ("Flow", "L/min", 50), ("Current", "mA", 10)])]},
            "contact-inputs": {"channels": [{"channel": i + 1, "label": f"DI {i + 1}", "active": bool((n // (i + 3)) % 2),
                                             "counter": str(n // (i + 3)), "value": n // (i + 3), "unit": "pulse", "mode": 2}
                                            for i in range(3)]},
            "outputs": {"channels": [{"channel": i + 1, "active": state} for i, state in enumerate(self.outputs)]},
            "relay": {"state": self.relay},
            "thermistor-probe": {"label": "NTC probe", "valid": True, "fault": "none", "temperature_c": round(31 + math.sin(n / 10) * 3, 2)},
            "precision-probe": {"label": "PT100 probe", "valid": True, "fault": "0x00", "temperature_c": round(48 + math.sin(n / 13) * 6, 2)},
            "modbus-values/1": {"channel_id": 1, "slave_address": 1, "registers": {
                "voltage": {"value": round(230 + math.sin(n / 9) * 6, 2), "unit": "V", "valid": True},
                "current": {"value": round(4 + math.sin(n / 8) * 2, 2), "unit": "A", "valid": True},
                "power": {"value": round(920 + math.sin(n / 8) * 240, 1), "unit": "W", "valid": True}}},
            "modbus-values/2": {"channel_id": 2, "slave_address": 2, "registers": {
                "temperature": {"value": round(26 + math.sin(n / 11) * 3, 2), "unit": "C", "valid": True},
                "humidity": {"value": round(55 + math.sin(n / 10) * 5, 2), "unit": "%", "valid": True}}}}
        for suffix, payload in values.items():
            self.store.ingest(f"{self.store.prefix}/{suffix}", json.dumps({**common, **payload}))
            if at is not None:
                with self.store.lock:
                    for key, points in self.store.history.items():
                        if key.startswith(suffix + ".") and points:
                            points[-1]["at"] = at


def create_app(store, bridge):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 4096

    @app.before_request
    def local_host_only():
        if request.host.split(":")[0] not in ("127.0.0.1", "localhost", "[::1]"):
            return jsonify(error="This console is local-only"), 403

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/state")
    def state():
        return jsonify(store.snapshot(include_history=True))

    @app.get("/api/events")
    def events():
        def stream():
            while True:
                yield f"data: {json.dumps(store.snapshot(), allow_nan=False)}\n\n"
                time.sleep(1)
        return Response(stream(), mimetype="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/api/control")
    def control():
        if not secrets.compare_digest(request.headers.get("X-Control-Token", ""), store.csrf):
            return jsonify(error="Refresh the page before sending controls"), 403
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(error="Cross-origin controls are not allowed"), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Expected a JSON object"), 400
        command = None
        try:
            command = store.prepare_command(payload.get("target"), payload.get("channel"), payload.get("state"))
            bridge.send(command)
            return jsonify(command=command), 202
        except (ValueError, ConnectionError) as exc:
            if command:
                with store.lock:
                    for item in store.commands:
                        if item["id"] == command["id"]:
                            item["status"] = "failed"
                    store.event("warning", str(exc))
            return jsonify(error=str(exc)), 400 if isinstance(exc, ValueError) else 409

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Offline simulation; never connects/publishes to MQTT")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = DeviceStore(mode="demo" if args.demo else "live")
    if args.demo:
        bridge = DemoBridge(store)
    else:
        password = os.getenv("MQTT_PASSWORD") or getpass.getpass(f"MQTT password for {config.MQTT_USERNAME}: ")
        if not password:
            parser.error("MQTT password is required")
        bridge = MqttBridge(store, password)
    app = create_app(store, bridge)
    bridge.start()
    try:
        print(f"\nPN Master Connect: http://127.0.0.1:{args.port}\n", flush=True)
        app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True, use_reloader=False)
    finally:
        bridge.stop()


if __name__ == "__main__":
    main()
