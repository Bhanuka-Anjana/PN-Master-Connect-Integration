import json
from pathlib import Path
import socket
import struct
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as console


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.store = console.DeviceStore()
        self.bridge = Mock()
        self.client = console.create_app(self.store, self.bridge).test_client()
        self.headers = {"X-Control-Token": self.store.csrf}

    def ingest_outputs(self, retained=False):
        self.store.ingest(self.store.prefix + "/outputs", json.dumps({"channels": [
            {"channel": index, "active": False} for index in (1,2,3)]}), retained)

    def test_readonly_state_and_assets_work_without_mqtt(self):
        with self.client.get("/") as response:
            self.assertEqual(response.status_code, 200)
        with self.client.get("/static/app.js") as response:
            self.assertEqual(response.status_code, 200)
        state = self.client.get("/api/state").json
        self.assertFalse(state["connected"])
        self.assertFalse(state["fresh"])
        self.assertNotIn("password", state)

    def test_controls_require_token_origin_connection_and_correct_types(self):
        command = {"target":"do", "channel":1, "state":True}
        self.assertEqual(self.client.post("/api/control", json=command).status_code, 403)
        self.assertEqual(self.client.post("/api/control", headers=self.headers, json=command).status_code, 409)
        self.store.connection(True, "test")
        self.assertEqual(self.client.post("/api/control", headers={**self.headers,"Origin":"https://foreign.invalid"}, json=command).status_code, 403)
        for invalid in ({**command,"state":1},{**command,"channel":True},{**command,"channel":4},{**command,"target":"arbitrary/topic"}):
            self.assertEqual(self.client.post("/api/control", headers=self.headers, json=invalid).status_code, 400)
        self.bridge.send.assert_not_called()

    def test_controls_allow_repeated_on_off_without_telemetry(self):
        self.store.connection(True, "test")
        for target, channel in (("do", 1), ("do", 2), ("do", 3), ("relay", None)):
            for state in (True, False, True):
                response = self.client.post("/api/control", headers=self.headers,
                                            json={"target": target, "channel": channel, "state": state})
                self.assertEqual(response.status_code, 202, response.json)
                self.assertEqual(response.json["command"]["state"], state)
        self.assertEqual(self.bridge.send.call_count, 12)
        self.assertEqual(self.store.latest, {})

    def test_controls_allow_retained_stale_or_missing_actuator_state(self):
        self.store.connection(True, "test")
        for kind in ("retained", "stale", "missing"):
            with self.subTest(kind=kind):
                self.ingest_outputs(retained=kind == "retained")
                if kind == "stale":
                    self.store.latest["outputs"]["at"] -= 25
                elif kind == "missing":
                    self.store.ingest(self.store.prefix + "/outputs", '{"channels":[]}')
                response = self.client.post("/api/control", headers=self.headers,
                                            json={"target": "do", "channel": 1, "state": True})
                self.assertEqual(response.status_code, 202, response.json)

    def test_command_waits_for_matching_nonretained_telemetry(self):
        self.store.connection(True, "test")
        self.ingest_outputs()
        response = self.client.post("/api/control", headers=self.headers, json={"target":"do","channel":1,"state":True})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["command"]["status"], "sent")
        payload = {"channels":[{"channel":1,"active":True}]}
        self.store.ingest(self.store.prefix+"/outputs", json.dumps(payload), retained=True)
        self.assertEqual(self.store.snapshot()["commands"][0]["status"], "sent")
        self.store.ingest(self.store.prefix+"/outputs", json.dumps(payload))
        self.assertEqual(self.store.snapshot()["commands"][0]["status"], "confirmed")

    def test_failure_and_timeout_do_not_claim_success(self):
        self.store.connection(True, "test")
        self.ingest_outputs()
        self.bridge.send.side_effect = ConnectionError("test disconnected")
        response = self.client.post("/api/control", headers=self.headers, json={"target":"do","channel":1,"state":True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.snapshot()["commands"][0]["status"], "failed")
        self.bridge.send.side_effect = None
        self.client.post("/api/control", headers=self.headers, json={"target":"do","channel":1,"state":True})
        self.store.commands[0]["at"] -= 11
        self.assertEqual(self.store.snapshot()["commands"][0]["status"], "timeout")

    def test_unexpected_topics_and_malformed_telemetry_are_ignored(self):
        for suffix, body in (("relay/set",b'{"state":true}'),("modbus-values/500",b"{}"),
                             ("environment",b"NaN"),("outputs",b'{"channels":[1]}'),
                             ("modbus-values/1",b'{"registers":[]}')):
            self.assertFalse(self.store.ingest(self.store.prefix+"/"+suffix,body))
        self.assertEqual(self.store.message_count,0)

    def test_bounded_history(self):
        for count in range(400):
            self.store.ingest(self.store.prefix+"/environment",json.dumps({"temperature_c":count}))
        self.assertEqual(len(self.store.history["environment.temperature_c"]),360)


def exact(connection, size):
    data=b""
    while len(data)<size:
        chunk=connection.recv(size-len(data))
        if not chunk: raise EOFError()
        data+=chunk
    return data


def read_packet(connection):
    header=exact(connection,1)[0]
    size=0; multiplier=1
    while True:
        byte=exact(connection,1)[0];size+=(byte&127)*multiplier
        if byte<128:break
        multiplier*=128
    return header,exact(connection,size)


def packet(header,body):
    size=len(body);length=bytearray()
    while True:
        digit=size%128;size//=128;length.append(digit|(128 if size else 0))
        if not size:return bytes([header])+length+body


class LocalMqttIntegrationTests(unittest.TestCase):
    def test_real_mqtt_subscribe_http_control_and_device_confirmation(self):
        listener=socket.socket();listener.bind(("127.0.0.1",0));listener.listen(1);listener.settimeout(5)
        self.addCleanup(listener.close)
        port=listener.getsockname()[1]
        store=console.DeviceStore();errors=[];published=[]

        def broker():
            try:
                connection,_=listener.accept()
                with connection:
                    connection.settimeout(5)
                    self.assertEqual(read_packet(connection)[0]>>4,1)
                    connection.sendall(b"\x20\x03\x00\x00\x00")
                    def telemetry(suffix,body):
                        topic=(store.prefix+"/"+suffix).encode()
                        connection.sendall(packet(0x30,struct.pack("!H",len(topic))+topic+b"\x00"+json.dumps(body).encode()))
                    while True:
                        header,data=read_packet(connection)
                        kind=header>>4
                        if kind==8:
                            length=int.from_bytes(data[3:5],"big")
                            self.assertEqual(data[5:5+length].decode(),store.prefix+"/#")
                            connection.sendall(packet(0x90,data[:2]+b"\x00\x01"))
                            telemetry("outputs",{"channels":[{"channel":1,"active":False}]})
                            telemetry("relay",{"state":False})
                        elif kind==3:
                            self.assertEqual(header&7,0,"Control must be QoS0, non-retained")
                            length=int.from_bytes(data[:2],"big");topic=data[2:2+length].decode()
                            self.assertEqual(data[2+length],0)
                            body=json.loads(data[3+length:]);published.append((topic,body))
                            if topic.endswith("/do/set"):
                                telemetry("outputs",{"channels":[{"channel":body["channel"],"active":body["state"]}]})
                            else:telemetry("relay",{"state":body["state"]})
                        elif kind==12:connection.sendall(b"\xd0\x00")
                        elif kind==14:return
            except Exception as error:errors.append(error)

        worker=threading.Thread(target=broker,daemon=True);worker.start()
        # TLS is disabled ONLY for this isolated localhost test broker.
        with patch.object(console.config,"MQTT_HOST","127.0.0.1"),patch.object(console.config,"MQTT_PORT",port), \
             patch("paho.mqtt.client.Client.tls_set_context",return_value=None):
            bridge=console.MqttBridge(store,"test-only-password")
            client=console.create_app(store,bridge).test_client()
            bridge.start()
            try:
                deadline=time.monotonic()+5
                while (not store.connected or "relay" not in store.latest) and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(store.connected)
                for target,channel in (("do",1),("relay",None)):
                    response=client.post("/api/control",headers={"X-Control-Token":store.csrf},json={"target":target,"channel":channel,"state":True})
                    self.assertEqual(response.status_code,202,response.json)
                    deadline=time.monotonic()+5
                    while store.snapshot()["commands"][0]["status"]=="sent" and time.monotonic()<deadline:time.sleep(.01)
                    self.assertEqual(store.snapshot()["commands"][0]["status"],"confirmed")
            finally:bridge.stop();worker.join(timeout=5)
        self.assertEqual(errors,[])
        self.assertEqual(len(published),2)
        self.assertFalse(worker.is_alive())


if __name__=="__main__":unittest.main()
