"""
Record events from MQTT to SQLite DB
"""

import argparse
import signal
import sqlite3
import sys
import threading
from datetime import datetime
from json import dumps

import paho.mqtt.client as mqtt
import pandas as pd
import yaml
from sklearn.svm import SVC

SERVICE_NAME = "presence-ml"

parser = argparse.ArgumentParser(
    description="Presence detection using k-nearest neighbours"
)
parser.add_argument(
    "-c",
    "--config",
    default="config.yaml",
    help="Configuration yaml file, defaults to `config.yaml`",
    dest="config_file",
)
parser.add_argument(
    "-t",
    "--train",
    help="Train the model by recording signal strength by moving around your current LOCATION",
    dest="location",
)
args = parser.parse_args()

with open(args.config_file, "r") as f:
    config = yaml.safe_load(f)

default_config = {
    "mqtt": {"broker": "127.0.0.1", "port": 1883, "username": None, "password": None},
    "devices": [],
    "sensors": [],
    "homeassistant": {"topic": "homeassistant", "device_tracker": True, "sensor": True},
    "database": "rssi.sqlite",
    "knn": {"neighbours": 7, "min_confidence": 0.8},
    "debounce_length": 3,
}

config = {**default_config, **config}


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
    def __init__(self):
        self._debounce = Debounce()
        self.home_state = "not_home"

    def update(self, rssi_results):
        """If any of the stations have a valid RSSI value then the devie is home."""
        is_home = False
        for station in rssi_results.values():
            if station["rssi"]:
                is_home = True
                break
        is_home, has_changed = self._debounce.vote(is_home)
        self.home_state = "home" if is_home else "not_home"
        return self.home_state, has_changed


def mqtt_on_connect(client, userdata, flags, rc):
    """Renew subscriptions and set Last Will message when we connect to broker."""
    # Set up Last Will, and then set our status to 'online'
    client.will_set(f"{SERVICE_NAME}/status", payload="offline", qos=1, retain=True)
    client.publish(f"{SERVICE_NAME}/status", payload="online", qos=1, retain=True)

    for device in config["devices"]:
        device_id = device["id"]
        sensor_topics = []
        for sensor_id in config["sensors"]:
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
    """The callback for when a PUBLISH message is received from the server."""
    global results
    station = msg.topic.split("/")[0]
    rssi = int(msg.payload)
    results[station]["rssi"] = rssi
    results[station]["time"] = datetime.utcnow()


def save_results(location):
    """Save latest results in to the DB."""
    conn = sqlite3.connect(config["database"])
    conn.execute(
        "INSERT INTO rssi (homepi, upylounge, upybedroom, location) VALUES (?, ?, ?, ?)",
        (
            results["homepi"]["rssi"],
            results["upylounge"]["rssi"],
            results["upybedroom"]["rssi"],
            location,
        ),
    )
    conn.commit()
    conn.close()
    print(f"Saving to --> {location}")


def materialise_rssi_aggregation():
    """Take the raw results and group them in to a table for faster querying."""
    conn = sqlite3.connect(config["database"])
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS rssi_agg;")
    cur.execute(
        """
        CREATE TABLE rssi_agg AS SELECT homepi,
                                        upylounge,
                                        upybedroom,
                                        batch.location,
                                        count( * ) weight
                                  FROM rssi
                                  join batch on batch.id = rssi.batch_id
                                  GROUP BY homepi,
                                           upylounge,
                                           upybedroom,
                                           location;
        """
    )
    conn.commit()
    conn.close()


def knn(neighbours):
    """Run k-nearest neighbours on the dataset."""
    conn = sqlite3.connect(config["database"])
    cur = conn.cursor()
    cur.execute(
        """
          SELECT location,
                 sum(1.0*weight/dist) AS total_weight
          FROM (SELECT ( ( ? - upybedroom) * ( ? - upybedroom) +
                         ( ? - upylounge) * ( ? - upylounge) +
                         ( ? - homepi) * ( ? - homepi) ) AS dist,
                       location,
                       weight
                 FROM rssi_agg
                 WHERE dist IS NOT NULL
                 ORDER BY dist ASC
                 LIMIT ?)
          GROUP BY location
          ORDER BY total_weight DESC, location ASC;
        """,
        (
            results["upybedroom"]["rssi"],
            results["upybedroom"]["rssi"],
            results["upylounge"]["rssi"],
            results["upylounge"]["rssi"],
            results["homepi"]["rssi"],
            results["homepi"]["rssi"],
            neighbours,
        ),
    )
    rows = cur.fetchall()
    conn.close()
    if rows and rows[0][0]:
        location = rows[0][0]
        weight = rows[0][1]
        total_weight = 0
        for row in rows:
            print(f"Room: {row[0]}, Weight: {row[1]}")
            total_weight += row[1]
        return location, weight / total_weight
    else:
        return None, None


def load_data():
    # TODO generalise
    con = sqlite3.connect("rssi.sqlite")
    df = pd.read_sql_query(
        """select homepi, upylounge, upybedroom, batch.location
           from rssi
           join batch on rssi.batch_id = batch.id
           where homepi is not null and upylounge is not null and upybedroom is not null
                 and batch.is_enabled = 1""",
        con,
    )
    X = df[["homepi", "upylounge", "upybedroom"]]
    y = df["location"]
    return X, y


class SVM:
    def __init__(self, rssis, locations):
        self.svm = SVC(kernel="linear", C=0.25, random_state=1, probability=True)
        self.svm.fit(rssis, locations)
        self.unique_locations = sorted(set(locations))

    def predict(self, rssi):
        if None in rssi:
            return None, None
        location = self.svm.predict([rssi])[0]
        probability = self.svm.predict_proba([rssi])[
            0
        ]  # list of probabilities for all locations
        index = self.unique_locations.index(
            location
        )  # find probability for predicted location
        probability = probability[index]
        return location, probability


def on_exit(signum, frame):
    """Called when program exit is received."""
    print("Exiting...")
    client.publish("presence-ml/status", payload="offline", qos=1, retain=True)
    sys.exit(0)


def background_timer():
    """Loop timer for recording or predicting"""
    global results
    global room_debounce
    threading.Timer(0.5, background_timer).start()
    print(results)
    # Remove stale data
    for station in results:
        if (
            results[station]["time"]
            and (datetime.utcnow() - results[station]["time"]).total_seconds() > 10
        ):
            results[station]["rssi"] = None

    if args.location:
        # Train the model
        save_results(args.location)
    else:
        # Update home/not_home status
        home_state, has_changed = device_tracker.update(results)
        if has_changed:
            client.publish(
                f"{SERVICE_NAME}/device_tracker/dale_apple_watch/state",
                home_state,
                qos=1,
                retain=True,
            )

        # Predict location
        # location, confidence = knn(config["knn"]["neighbours"])
        rssi = [
            results["homepi"]["rssi"],
            results["upylounge"]["rssi"],
            results["upybedroom"]["rssi"],
        ]
        location, confidence = svm_predictor.predict(rssi)

        if location:
            print(
                f"{location}: {int(confidence*100)}% ->",
            )
            if confidence > config["knn"]["min_confidence"]:
                location, has_changed = room_debounce.vote(location)
                print(f"{room_debounce._history} -> {location}")
                # TODO unsure whether should publish if there is no location or not...
                if location and has_changed:
                    # TODO remove hardcoded device ID
                    client.publish(
                        f"{SERVICE_NAME}/sensor/dale_apple_watch/state",
                        location,
                        qos=1,
                        retain=True,
                    )


def publish_timer():
    """Publish the results to MQTT at least once per minute."""
    threading.Timer(60, publish_timer).start()
    client.publish(
        f"{SERVICE_NAME}/sensor/dale_apple_watch/state",
        room_debounce.state,
        retain=True,
    )
    client.publish(
        f"{SERVICE_NAME}/device_tracker/dale_apple_watch/state",
        device_tracker.home_state,
        retain=True,
    )


results = {
    "upybedroom": {"rssi": None, "time": None},
    "upylounge": {"rssi": None, "time": None},
    "homepi": {"rssi": None, "time": None},
}

room_debounce = Debounce(length=config["debounce_length"])
device_tracker = DeviceTracker()

X, y = load_data()
svm_predictor = SVM(X, y)

if __name__ == "__main__":
    materialise_rssi_aggregation()

    client = mqtt.Client()
    client.on_connect = mqtt_on_connect
    client.on_message = mqtt_on_message
    client.username_pw_set(config["mqtt"]["username"], config["mqtt"]["password"])
    client.connect(config["mqtt"]["broker"], config["mqtt"]["port"], 60)

    signal.signal(signal.SIGINT, on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    background_timer()
    publish_timer()

    # Blocking call that processes network traffic, dispatches callbacks and handles reconnecting.
    client.loop_forever()

# TODO
# figure out how to generalise this... maybe using the DB? for topics? or the yaml file
# what to do if get nulls? in db or no signal
# log events or pubish if the status changes rapidly in less than 10?
