"""Public connection defaults. Supply MQTT_PASSWORD at runtime, never to the UI."""
import os

MQTT_HOST = os.getenv("MQTT_HOST", "eef241f4301244258e36d13dc3117297.s1.eu.hivemq.cloud")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "master2")
MQTT_TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "protonest/virtual-master-001/stream/master").rstrip("/")
STALE_AFTER_S = 20
COMMAND_TIMEOUT_S = 10
HISTORY_LIMIT = 360
