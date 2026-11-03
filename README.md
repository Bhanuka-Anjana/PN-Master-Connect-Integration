# PN Master Connect

A local web dashboard for the Protonest master/virtual master MQTT protocol.
Python subscribes to HiveMQ over verified TLS and streams telemetry to the browser.
The browser never receives MQTT credentials and requires no CDN or frontend build.

## Start on Windows

Open this folder in a terminal and run:

```powershell
.\start.bat
```

Before starting, edit `config.py` and set `MQTT_PASSWORD` to your broker password.
The launcher creates `.venv`, installs the two Python dependencies and starts the
server using the settings in `config.py`. Credentials stay on the Python server
and are not sent to the browser. Keep the file private once it contains your password.

Open **http://127.0.0.1:8080**. Leave the terminal running; Ctrl+C stops the server.
Python 3.10+ is required. Alternative PowerShell launcher:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

You can also install/run directly:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

## Connect the virtual master

Run `tools/virtual_master.py` from the firmware repository in another terminal.
Set its topic prefix to match `MQTT_TOPIC_PREFIX` in `config.py`:

```text
protonest/gateway001/stream/master
```

The simulator uses its existing master1 credentials; this dashboard uses master2.
Both accounts must have broker permission on that prefix. A broker connection is
shown separately from fresh device telemetry. An empty live dashboard is expected
until the master publishes. Live mode never invents readings.

## Offline demo

```powershell
.\start.bat --demo
```

Demo mode uses local fake readings and simulated output confirmation. It sends no
MQTT messages. A persistent banner distinguishes the demo from live mode.

## Interface

- Ambient/NTC/PT100 line charts, selectable series, 2/10/30-minute windows, hover
  readings and chart pause/resume.
- Humidity semicircle gauge, sensor values and freshness/fault indicators.
- Four scaled analog bars with engineering units and range/alarm flags.
- Digital input LEDs, current states and pulse counters.
- Three DO switches plus a relay switch with pending/confirmed/timeout feedback.
- Slave selector, Modbus register table and selected-register trend.
- Filterable received/command/connection activity and JSON snapshot export.

Up to 360 samples per numeric series and 80 events are held in server memory.
History starts when this server starts; reconnecting a browser restores that buffer.
There is no database or long-term historian. Chart pause affects charts only;
telemetry and controls continue operating.

## MQTT contract

The server subscribes to `<prefix>/#` with QoS 1. Known telemetry suffixes:
`environment`, `analog-inputs`, `contact-inputs`, `outputs`, `relay`,
`thermistor-probe`, `precision-probe`, `modbus-values/1` through `/10`.
Formats match the firmware serializer and the virtual master script. `relay`
state telemetry is an extension implemented by the simulator; a physical firmware
that does not publish it can still receive relay commands without state feedback.

| Control topic | Payload |
| --- | --- |
| `<prefix>/do/set` | `{"channel":1,"state":true}` (channels 1..3) |
| `<prefix>/relay/set` | `{"state":true}` |

Commands use QoS 0, non-retained, to avoid offline command queues and replay.
`sent` means handed to the MQTT transport, not proof of actuation. A matching,
subsequent, non-retained output state changes it to `confirmed`; this is observed
telemetry, not a correlated command ID from the firmware. No state within ten
seconds is shown as unconfirmed/timeout. Commands are never automatically retried.
Controls are available whenever the broker is connected; device telemetry is optional.
You can send another command immediately without waiting for device confirmation.
Without new state feedback, switches use the last sent command to alternate ON/OFF
and label it as unconfirmed. Retained messages never confirm commands.

## Settings

All MQTT settings are hardcoded in `config.py`; environment variables and `.env`
files are not read. Edit these values directly:

```python
MQTT_HOST = "eef241f4301244258e36d13dc3117297.s1.eu.hivemq.cloud"
MQTT_PORT = 8883
MQTT_USERNAME = "master2"
MQTT_PASSWORD = "your-broker-password"
MQTT_TOPIC_PREFIX = "protonest/gateway001/stream/master"
```

Use the hostname without `mqtt://` or `https://`. The topic prefix must match the
device. Restart the server after changing settings.
`--port 8082` changes the local HTTP port. All MQTT traffic remains server-side.

The HTTP listener binds to 127.0.0.1 only. It is intended for local use, with
same-origin controls and an in-memory anti-CSRF token. There is no account/login
system. Add authentication, HTTPS and a production HTTP server before exposing
this application to other machines.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tests/check_frontend.py
```

Integration tests use a temporary localhost MQTT broker. They exercise real Paho
subscriptions, HTTP DO/relay commands, returned device telemetry, confirmation,
commands without telemetry, retained/stale state, validation and failed commands. They never
send controls to HiveMQ. The parser check verifies JavaScript syntax; interactive
browser QA is a separate check.
