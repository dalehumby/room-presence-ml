"""
Record events from MQTT to SQLite DB
"""

import argparse
import re
import signal
import sys
import threading
from datetime import datetime
from json import dumps

import paho.mqtt.client as mqtt
import yaml

SERVICE_NAME = "presence-ml"
RSSI_STALE_TIMEOUT = 10
mqtt_rssi_topic = re.compile(r"(\w+)\/sensor\/(\w+)_rssi\/state")

parser = argparse.ArgumentParser(description="Presence detection")
parser.add_argument(
    "-c",
    "--config",
    default="config.yaml",
    help="Configuration yaml file, defaults to `config.yaml`",
    dest="config_file",
)
args = parser.parse_args()


class Debounce:
    """Implement a low pass filter to reduce suprious room changes."""

    def __init__(self, init_state=None, length=3):
        """
        Initialise the filter with the starting state and the length of the filter.
        The longer the length the longer it takes to change state.
        """
        self.state = init_state
        self._previous_state = init_state
        self._history = [init_state] * length

    def vote(self, value):
        """
        Update the sliding window: add latest value, remove oldest value.
        If all items in the history are the same then change the state.
        """
        self._history = self._history[1:] + [value]
        if all([v == value for v in self._history]):
            self._previous_state = self.state
            self.state = value
        has_changed = self.state != self._previous_state
        return self.state, has_changed


class DeviceTracker:
    """Home Assistant Device Tracker for home/not_home detection."""

    def __init__(self, device_id):
        self.device_id = device_id
        self.home_state = "not_home"
        self._debounce = Debounce()

    def update(self, rssi_sensors):
        """
        Update the devices home/not_home status.

        If any of the sensors have a valid RSSI value then the devie is home,
        otherwise it is not_home.
        """
        is_home = False
        for sensor in rssi_sensors.values():
            if sensor["rssi"]:
                is_home = True
                break
        is_home, has_changed = self._debounce.vote(is_home)
        self.home_state = "home" if is_home else "not_home"
        return self.home_state, has_changed


def load_config(config_file):
    """Load the configuration from config yaml file and use it to override the defaults."""
    with open(config_file, "r") as f:
        config = yaml.safe_load(f)

    default_config = {
        "mqtt": {
            "broker": "127.0.0.1",
            "port": 1883,
            "username": None,
            "password": None,
        },
        "devices": [],
        "sensors": [],
        "homeassistant": {
            "topic": "homeassistant",
            "device_tracker": True,
            "sensor": True,
        },
        "debounce_length": 3,
        "hysteresis": 5,
    }

    config = {**default_config, **config}
    return config


def mqtt_on_connect(client, userdata, flags, rc):
    """Renew subscriptions and set Last Will message when we connect to broker."""
    # Set up Last Will, and then set status to 'online'
    client.will_set(f"{SERVICE_NAME}/status", payload="offline", qos=1, retain=True)
    client.publish(f"{SERVICE_NAME}/status", payload="online", qos=1, retain=True)

    for device_id, device in state.items():
        sensor_topics = []
        for sensor_id in device["sensors"]:
            sensor_topics.append((f"{sensor_id}/sensor/{device_id}_rssi/state", 0))
        client.subscribe(sensor_topics)

        # Home Assistant MQTT autoconfig
        if config["homeassistant"]["sensor"]:
            payload = {
                "payload_available": "online",
                "payload_not_available": "offline",
                "name": f"{device['name']} Presence",
                "device": {
                    "identifiers": f"{device_id}",
                    "name": f"{device['name']}",
                    "via_device": f"{SERVICE_NAME}",
                },
                "unique_id": f"{SERVICE_NAME}-ble-{device_id}",
                "state_topic": f"{SERVICE_NAME}/sensor/{device_id}/state",
                "json_attributes_topic": f"{SERVICE_NAME}/sensor/{device_id}/attributes",
                "availability_topic": f"{SERVICE_NAME}/status",
                "icon": "mdi:home-map-marker",
            }
            client.publish(
                f"{config['homeassistant']['topic']}/sensor/{SERVICE_NAME}/{device_id}/config",
                dumps(payload),
                qos=1,
                retain=True,
            )

        # Home Assistant MQTT autoconfig
        if config["homeassistant"]["device_tracker"]:
            payload = {
                "payload_available": "online",
                "payload_not_available": "offline",
                "name": f"{device['name']} Tracker",
                "device": {
                    "identifiers": f"{device_id}",
                    "name": f"{device['name']}",
                    "via_device": f"{SERVICE_NAME}",
                },
                "unique_id": f"{SERVICE_NAME}-ble-{device_id}-tracker",
                "state_topic": f"{SERVICE_NAME}/device_tracker/{device_id}/state",
                "json_attributes_topic": f"{SERVICE_NAME}/device_tracker/{device_id}/attributes",
                "availability_topic": f"{SERVICE_NAME}/status",
                "icon": "hass:account",
            }
            client.publish(
                f"{config['homeassistant']['topic']}/device_tracker/{SERVICE_NAME}/{device_id}/config",
                dumps(payload),
                qos=1,
                retain=True,
            )
            attributes = {"source_type": "ble"}
            client.publish(
                f"{SERVICE_NAME}/device_tracker/{device_id}/attributes",
                dumps(attributes),
                qos=1,
                retain=True,
            )


def mqtt_on_message(client, userdata, msg):
    """
    Handle incomming MQTT messages.

    Match against the rssi sensor topic, and update latest rssi values for the sensor.
    """
    global state
    topic_match = mqtt_rssi_topic.match(msg.topic)
    if topic_match:
        sensor_id = topic_match[1]
        device_id = topic_match[2]
        state[device_id]["sensors"][sensor_id]["rssi"] = int(msg.payload)
        state[device_id]["sensors"][sensor_id]["time"] = datetime.utcnow()


def on_exit(signum, frame):
    """
    Update MQTT status to `offline`

    Called when program exit is received.
    """
    print("Exiting...")
    client.publish(f"{SERVICE_NAME}/status", payload="offline", qos=1, retain=True)
    sys.exit(0)


def get_best_rssi_per_location(sensors):
    """Find the strongest signal in each location."""
    best_rssi = {}
    for sensor in sensors.values():
        location = sensor["location"]
        rssi = sensor["rssi"]
        if rssi and (location not in best_rssi or rssi > best_rssi[location]):
            best_rssi[location] = rssi
    return best_rssi


def get_location(current_location, rssi_per_location):
    print(f"  Current location: {current_location}, Best RSSI: {rssi_per_location}")
    try:
        current_rssi = rssi_per_location[current_location]
    except KeyError:
        current_rssi = -110
    max_rssi = current_rssi
    for location, rssi in rssi_per_location.items():
        print(f"  {location}, {rssi}, {max_rssi}")
        if rssi > max_rssi:
            max_rssi = rssi
            new_location = location
    if max_rssi - current_rssi > config["hysteresis"]:
        print(f"  New location {new_location}")
        return new_location
    else:
        return current_location


def remove_stale_rssi(state):
    """Set rssi to None if no update has been received recently."""
    for device_id in state:
        for sensor_id, sensor in state[device_id]["sensors"].items():
            if (
                sensor["time"]
                and (datetime.utcnow() - sensor["time"]).total_seconds()
                > RSSI_STALE_TIMEOUT
            ):
                state[device_id]["sensors"][sensor_id]["rssi"] = None
    return state


def timer_background():
    """Loop timer for recording or predicting"""
    global state
    threading.Timer(0.5, timer_background).start()
    state = remove_stale_rssi(state)
    print(state)

    for device_id, device in state.items():
        # Update home/not_home status
        home_state, has_changed = device["device_tracker"].update(device["sensors"])
        if has_changed:
            client.publish(
                f"{SERVICE_NAME}/device_tracker/{device_id}/state",
                home_state,
                qos=1,
                retain=True,
            )

        print(f"\nDevice: {device_id}")
        best_rssi_per_location = get_best_rssi_per_location(device["sensors"])
        location = get_location(device["debounce"].state, best_rssi_per_location)
        location, has_changed = device["debounce"].vote(location)
        print(f"  -> Location: {location}")
        if location and has_changed:
            client.publish(
                f"{SERVICE_NAME}/sensor/{device_id}/state",
                location,
                qos=1,
                retain=True,
            )
        print("----\n")


def timer_publish():
    """Publish the results to MQTT at least once per minute."""
    threading.Timer(60, timer_publish).start()
    for device_id, device in state.items():
        # print(device_id, device["debounce"].state, device["device_tracker"].home_state)
        client.publish(
            f"{SERVICE_NAME}/sensor/{device_id}/state",
            device["debounce"].state,
            retain=True,
        )
        client.publish(
            f"{SERVICE_NAME}/device_tracker/{device_id}/state",
            device["device_tracker"].home_state,
            retain=True,
        )


def init_state(config):
    """Initiate the global state machine."""
    state = {}
    for device in config["devices"]:
        device_id = device["id"]
        state[device_id] = {
            "name": device["name"],
            "debounce": Debounce(length=config["debounce_length"]),
            "device_tracker": DeviceTracker(device_id),
            "sensors": {},
        }
        for location in config["sensors"]:
            for sensor_id in config["sensors"][location]:
                state[device_id]["sensors"][sensor_id] = {
                    "rssi": None,
                    "time": None,
                    "location": location,
                }
    return state


config = load_config(args.config_file)
state = init_state(config)


if __name__ == "__main__":
    client = mqtt.Client()
    client.on_connect = mqtt_on_connect
    client.on_message = mqtt_on_message
    client.username_pw_set(config["mqtt"]["username"], config["mqtt"]["password"])
    client.connect(config["mqtt"]["broker"], config["mqtt"]["port"], 60)

    signal.signal(signal.SIGINT, on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    timer_background()
    timer_publish()

    # Blocking call that processes network traffic, dispatches callbacks and handles reconnecting.
    client.loop_forever()

# TODO
# figure out how to generalise this... maybe using the DB? for topics? or the yaml file
# what to do if get nulls? in db or no signal
# log events or pubish if the status changes rapidly in less than 10?
