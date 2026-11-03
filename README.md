# PN Master Connect

A local web dashboard for the Protonest master/virtual master MQTT protocol.
Python subscribes to HiveMQ over verified TLS and streams telemetry to the browser.
The browser never receives MQTT credentials and requires no CDN or frontend build.

## Start on Windows

Open this folder in a terminal and run:

```powershell
.\start.bat
```

The launcher creates `.venv`, installs the two Python dependencies and starts the
server. Enter the supplied MQTT password when prompted for user **master2**.
Passwords are not echoed, written to disk, or sent to the browser.

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
Its default prefix matches this dashboard:

```text
protonest/virtual-master-001/stream/master
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
that does not publish it will leave this relay control unavailable.

| Control topic | Payload |
| --- | --- |
| `<prefix>/do/set` | `{"channel":1,"state":true}` (channels 1..3) |
| `<prefix>/relay/set` | `{"state":true}` |

Commands use QoS 0, non-retained, to avoid offline command queues and replay.
`sent` means handed to the MQTT transport, not proof of actuation. A matching,
subsequent, non-retained output state changes it to `confirmed`; this is observed
telemetry, not a correlated command ID from the firmware. No state within ten
seconds is shown as unconfirmed/timeout. Commands are never automatically retried.
Fresh telemetry (less than 20 seconds old) is required for each controlled output.
Retained messages can be displayed but never enable controls or confirm commands.

## Settings

Public defaults are in `config.py`: HiveMQ hostname, TLS port 8883, username
master2 and the virtual-master prefix. Environment overrides:
`MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TOPIC_PREFIX`.
Set credentials only in your runtime environment; `.env` files are not loaded.
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
freshness/retained-message guards, validation and failed commands. They never
send controls to HiveMQ. The parser check verifies JavaScript syntax; interactive
browser QA is a separate check.
