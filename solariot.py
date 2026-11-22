#!/usr/bin/env python3

# Copyright (c) 2017 Dennis Mellican
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from importlib import import_module
from threading import Event, Thread

import datetime
import inspect
import argparse
import logging
import logging.config
import json
import signal
import struct
import time
import sys
import re
import socket


MIN_SIGNED = -2147483648
MAX_UNSIGNED = 4294967295

# Load in the config module
parser = argparse.ArgumentParser()
parser.add_argument(
    "-c", "--config", default="config", help="Python module to load as our config"
)
parser.add_argument(
    "-v",
    "--verbose",
    action="count",
    default=0,
    help="Level of verbosity 0=ERROR 1=INFO 2=DEBUG",
)
parser.add_argument(
    "--one-shot",
    action="store_true",
    help="Run solariot just once then exit, useful for cron based execution",
)
parser.add_argument(
    "--sungrow-reset",
    action="store_true",
    help="After comms fail with the inverter, do a dummy connect to try to reset the inverter's modbus connection",
)

args = parser.parse_args()


try:
    config = import_module(args.config)
    if hasattr(config, "logger_config"):
        # Set up logging based on the config module
        logging.config.dictConfig(config.logger_config)
    else:
        # Set up logging based on verbose runtime argument
        if args.verbose == 0:
            log_level = logging.WARNING
        elif args.verbose == 1:
            log_level = logging.INFO
        else:
            log_level = logging.DEBUG

        logging.basicConfig(
            level=log_level, format="%(asctime)s - %(levelname)s - %(message)s"
        )
        logging.info(
            f"Logger configured at level {log_level} since verbose={args.verbose}"
        )

    logging.info(f"Loaded config {config.model}")
except ModuleNotFoundError:
    parser.error(f"Unable to locate {args.config}.py")

# SMA datatypes and their register lengths
# S = Signed Number, U = Unsigned Number, STR = String
sma_moddatatype = {
    "S16": 1,
    "U16": 1,
    "S32": 2,
    "U32": 2,
    "U64": 4,
    "STR16": 8,
    "STR32": 16,
}

# Load the modbus register map for the inverter
modmap_file = f"modbus-{config.model}"

try:
    modmap = import_module(modmap_file)
except ModuleNotFoundError:
    logging.error(f"Unable to locate {modmap_file}.py")
    sys.exit(1)

# This will try the Sungrow client otherwise will default to pymodbus 3.x.
client_payload = {
    "host": config.inverter_ip,
    "timeout": config.timeout,
    "retries": 3,
    "port": config.inverter_port,
}

if "sungrow-" in config.model:
    if config.inverter_port == 8082:
        from SungrowModbusWebClient import SungrowModbusWebClient

        logging.info("Creating SungrowModbusWebClient")
        client = SungrowModbusWebClient.SungrowModbusWebClient(**client_payload)
    else:
        from sungrow_tcp_client import SungrowModbusTcpClient

        logging.info("Creating SungrowModbusTcpClient")
        client = SungrowModbusTcpClient(**client_payload)
else:
    from pymodbus.client import ModbusTcpClient

    logging.info("Creating ModbusTcpClient")
    client = ModbusTcpClient(**client_payload)


def get_modbus_id_kwarg(modbus_client):
    """Detect whether the Modbus client expects device_id=, slave=, or unit=."""
    params = inspect.signature(modbus_client.read_input_registers).parameters
    for kw in ("device_id", "slave", "unit"):
        if kw in params:
            return kw
    return "device_id"


modbus_id_kwarg = get_modbus_id_kwarg(client)

if args.sungrow_reset:
    logging.info("[reset] sungrow_reset enabled")

logging.info("Connecting")
if client.connect():
    logging.info("Connected")
else:
    logging.warning("Initial connection to inverter failed; will retry in scan loop")
client.close()

# Configure MQTT
mqtt = None
mqtt_connected = Event()
if hasattr(config, "mqtt_server"):
    import paho.mqtt.client as mqtt

    def on_mqtt_connect(client_obj, userdata, flags, reason_code, properties):
        if getattr(reason_code, "is_failure", False):
            mqtt_connected.clear()
            logging.error("MQTT connection failed: %s", reason_code)
        else:
            mqtt_connected.set()
            logging.info("MQTT connected: %s", reason_code)

    def on_mqtt_disconnect(
        client_obj, userdata, disconnect_flags, reason_code, properties
    ):
        mqtt_connected.clear()
        if getattr(reason_code, "is_failure", False):
            logging.warning("MQTT disconnected unexpectedly: %s", reason_code)
        else:
            logging.info("MQTT disconnected: %s", reason_code)

    mqtt_client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=getattr(config, "mqtt_client_name", "pv_data"),
    )
    mqtt_client.on_connect = on_mqtt_connect
    mqtt_client.on_disconnect = on_mqtt_disconnect
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)

    if hasattr(config, "mqtt_username") and hasattr(config, "mqtt_password"):
        mqtt_client.username_pw_set(config.mqtt_username, config.mqtt_password)

    if config.mqtt_port == 8883:
        mqtt_client.tls_set()

    try:
        mqtt_client.connect(config.mqtt_server, port=config.mqtt_port)
    except OSError as err:
        logging.warning(
            "Initial MQTT connection failed (%s); will retry in background", err
        )
        mqtt_client.connect_async(config.mqtt_server, port=config.mqtt_port)

    mqtt_client.loop_start()
    logging.info("Configured MQTT Client")
else:
    mqtt_client = None
    logging.info("No MQTT configuration detected")

if hasattr(config, "prometheus"):
    from prometheus_client import start_http_server, Gauge

    class PrometheusPublisher(object):
        def __init__(self, port):
            self.publishport = port
            self.metric_mappings = {}
            start_http_server(self.publishport)
            logging.info(f"prometheus: http server started on port {self.publishport}")

        def publish_status(self, metrics):
            for key in metrics.keys():
                if isinstance(metrics[key], str):
                    # skipped because gagues dont handle strings
                    logging.debug(f"prometheus: key {key} skipped(was a string)")
                    continue
                elif not key in self.metric_mappings.keys():
                    logging.info(
                        f"prometheus: key {key} doesnt have a gauge. making one now"
                    )
                    self.metric_mappings[key] = Gauge("solar_" + key, key)

                self.metric_mappings[key].set(metrics[key])

        def Clear_status(self):
            for key in self.metric_mappings.keys():
                self.metric_mappings[key].set(0)

    promport = getattr(config, "prometheus_port", 8000)
    prom_client = PrometheusPublisher(promport)
    logging.info("Configured Prometheus Client")
else:
    logging.info("No Prometheus configuration detected")
    prom_client = None

# Configure InfluxDB
if hasattr(config, "influxdb_ip"):
    from influxdb import InfluxDBClient

    flux_client = InfluxDBClient(
        config.influxdb_ip,
        config.influxdb_port,
        config.influxdb_user,
        config.influxdb_password,
        config.influxdb_database,
        ssl=config.influxdb_ssl,
        verify_ssl=config.influxdb_verify_ssl,
    )

    logging.info("Configured InfluxDB Client")
else:
    flux_client = None
    logging.info("No InfluxDB configuration detected")

# Configure PVOutput
if hasattr(config, "pvoutput_api"):
    import requests
    import urllib3

    urllib3.disable_warnings()

    class PVOutputPublisher(object):
        def __init__(
            self,
            api_key,
            system_id,
            metric_mappings,
            rate_limit=60,
            status_url="https://pvoutput.org/service/r2/addstatus.jsp",
        ):
            self.api_key = api_key
            self.system_id = system_id
            self.status_url = status_url
            self.metric_mappings = metric_mappings
            self.rate_limit = rate_limit

            self.latest_run = None

        @property
        def headers(self):
            return {
                "X-Pvoutput-Apikey": self.api_key,
                "X-Pvoutput-SystemId": self.system_id,
                "Content-Type": "application/x-www-form-urlencoded",
                "cache-control": "no-cache",
            }

        def publish_status(self, metrics):
            """
            See https://pvoutput.org/help.html#api-addstatus
            Post the following values:
            * v1  - Energy Generation
            * v2  - Power Generation
            * v3  - Energy Consumption
            * v4  - Power Consumption
            * v5  - Temperature
            * v6  - Voltage
            * c1  - Cumulative Flag
            * n   - Net Flag
            * v7  - Extended Value v7
            * v8  - Extended Value v8
            * v9  - Extended Value v9
            * v10 - Extended Value v10
            * v11 - Extended Value v11
            * v12 - Extended Value v12
            * m1  - Text Message 1
            """
            at_least_one_of = set(["v1", "v2", "v3", "v4"])

            now = datetime.datetime.now()

            if self.latest_run:
                # Spread out our publishes over the hour based on the rate limit
                time_diff = (now - self.latest_run).total_seconds()

                if time_diff < (3600 / self.rate_limit):
                    return "skipped"

            parameters = {"d": now.strftime("%Y%m%d"), "t": now.strftime("%H:%M")}

            # Standard Values

            if getattr(config, "pvoutput_cumulative", True) is True:
                parameters["c1"] = 1

            if (
                self.metric_mappings.get("Energy Generation") in metrics
                and getattr(config, "pvoutput_power_only", False) is False
            ):
                parameters["v1"] = metrics[
                    self.metric_mappings.get("Energy Generation")
                ]

            if self.metric_mappings.get("Power Generation") in metrics:
                parameters["v2"] = metrics[self.metric_mappings.get("Power Generation")]

            if (
                self.metric_mappings.get("Energy Consumption") in metrics
                and getattr(config, "pvoutput_power_only", False) is False
            ):
                parameters["v3"] = metrics[
                    self.metric_mappings.get("Energy Consumption")
                ]

            if self.metric_mappings.get("Power Consumption") in metrics:
                parameters["v4"] = metrics[
                    self.metric_mappings.get("Power Consumption")
                ]

            if self.metric_mappings.get("Temperature") in metrics:
                parameters["v5"] = metrics[self.metric_mappings.get("Temperature")]

            if self.metric_mappings.get("Voltage") in metrics:
                parameters["v6"] = metrics[self.metric_mappings.get("Voltage")]

            # Extended Values
            if self.metric_mappings.get("Extended Value v7") in metrics:
                parameters["v7"] = metrics[
                    self.metric_mappings.get("Extended Value v7")
                ]

            if self.metric_mappings.get("Extended Value v8") in metrics:
                parameters["v8"] = metrics[
                    self.metric_mappings.get("Extended Value v8")
                ]

            if self.metric_mappings.get("Extended Value v9") in metrics:
                parameters["v9"] = metrics[
                    self.metric_mappings.get("Extended Value v9")
                ]

            if self.metric_mappings.get("Extended Value v10") in metrics:
                parameters["v10"] = metrics[
                    self.metric_mappings.get("Extended Value v10")
                ]

            if self.metric_mappings.get("Extended Value v11") in metrics:
                parameters["v11"] = metrics[
                    self.metric_mappings.get("Extended Value v11")
                ]

            if self.metric_mappings.get("Extended Value v12") in metrics:
                parameters["v12"] = metrics[
                    self.metric_mappings.get("Extended Value v12")
                ]

            # Text Message String truncated to PVOutput's max of 30 Characters
            if self.metric_mappings.get("Text Message 1") in metrics:
                parameters["m1"] = str(
                    metrics[self.metric_mappings.get("Text Message 1")]
                )[:30]

            if not at_least_one_of.intersection(parameters.keys()):
                raise RuntimeError(
                    "Metrics => PVOutput mapping failed, please review metric names and update"
                )

            response = requests.post(
                url=self.status_url, headers=self.headers, params=parameters
            )

            if response.status_code != requests.codes.ok:
                raise RuntimeError(response.text)

            logging.debug("Successfully posted status update to PVOutput")
            self.latest_run = now

    pvoutput_client = PVOutputPublisher(
        config.pvoutput_api,
        config.pvoutput_sid,
        modmap.pvoutput,
        rate_limit=config.pvoutput_rate_limit,
    )

    logging.info("Configured PVOutput Client")
else:
    pvoutput_client = None
    logging.info("No PVOutput configuration detected")

# Inverter Scanning
inverter = {}
bus = json.loads(modmap.scan)


def load_registers(register_type, start, count=100):
    id_kwargs = {modbus_id_kwarg: config.slave}
    try:
        if register_type == "read":
            rr = client.read_input_registers(
                int(start),
                count=count,
                **id_kwargs,
            )
        elif register_type == "holding":
            rr = client.read_holding_registers(
                int(start),
                count=count,
                **id_kwargs,
            )
        else:
            raise RuntimeError(f"Unsupported register type: {register_type}")
    except Exception as err:
        logging.warning(
            "Modbus read failed (%s %s..+%s): %s. Try increasing the timeout or scan interval.",
            register_type,
            start,
            count,
            err,
        )
        return False

    if rr.isError():
        logging.warning("Modbus connection/read failed: %s", rr)
        return False

    if not hasattr(rr, "registers"):
        logging.warning("No registers returned")
        return False

    if len(rr.registers) != count:
        logging.warning(
            f"Mismatched number of registers read {len(rr.registers)} != {count}"
        )
        return False

    overflow_regex = re.compile(r"(?P<register_name>[a-zA-Z0-9_\.]+)_overflow$")
    divide_regex = re.compile(
        r"(?P<register_name>[a-zA-Z0-9_]+)_(?P<divide_by>[0-9\.]+)$"
    )

    for num in range(0, count):
        run = int(start) + num + 1

        if register_type == "read" and modmap.read_register.get(str(run)):
            register_name = modmap.read_register.get(str(run))
            register_value = rr.registers[num]

            # Check if the modbus map has an '_overflow' on the end
            # If so the value 'could' be negative (65535 - x) where (-x) is the actual number
            # So a value of '64486' actually represents '-1049'
            # We rely on a second '_indicator' register to tell is if it's actually negative or not, otherwise it's ambigious!
            should_overflow = overflow_regex.match(register_name)

            if should_overflow:
                register_name = should_overflow["register_name"]

                # Find the indicator register value
                indicator_name = f"{register_name}_indicator"

                for reg_num, reg_name in modmap.read_register.items():
                    if reg_name == indicator_name:
                        indicator_register = int(reg_num)
                        break
                else:
                    indicator_register = None

                if indicator_register is not None:
                    # Given register '5084' and knowing start of '5000' we can assume the index
                    # Of our indicator value is 5084 - 5000 - 1 (because of the 'off by 1')
                    indicator_value = rr.registers[indicator_register - int(start) - 1]

                    if indicator_value == 65535:
                        # We are in overflow
                        register_value = -1 * (65535 - register_value)

            # Check if the modbus map has an '_10' or '_100' etc on the end
            # If so, we divide by that and drop it from the name
            should_divide = divide_regex.match(register_name)

            if should_divide:
                register_name = should_divide["register_name"]
                register_value = float(register_value) / float(
                    should_divide["divide_by"]
                )

            # Set the final register name and value, any adjustments above included
            inverter[register_name] = register_value
        elif register_type == "holding" and modmap.holding_register.get(str(run)):
            register_name = modmap.holding_register.get(str(run))
            register_value = rr.registers[num]

            inverter[register_name] = register_value

    return True


# Function for polling data from the target and triggering writing to log file if set


def decode_sma_registers(registers, datatype):
    """Decode big-endian Modbus registers without deprecated BinaryPayloadDecoder."""
    raw = struct.pack(f">{len(registers)}H", *registers)
    if datatype == "S32":
        return struct.unpack(">i", raw[:4])[0]
    if datatype == "U32":
        return struct.unpack(">I", raw[:4])[0]
    if datatype == "U64":
        return struct.unpack(">Q", raw[:8])[0]
    if datatype == "STR16":
        return raw[:16].rstrip(b"\x00").decode("utf-8", errors="replace")
    if datatype == "STR32":
        return raw[:32].rstrip(b"\x00").decode("utf-8", errors="replace")
    if datatype == "S16":
        return struct.unpack(">h", raw[:2])[0]
    return struct.unpack(">H", raw[:2])[0]


def load_sma_register(registers):
    id_kwargs = {modbus_id_kwarg: config.slave}
    # Request each register from datasets, omit first row which contains only column headers
    for thisrow in registers:
        name = thisrow[0]
        startPos = thisrow[1]
        datatype = thisrow[2]
        fmt = thisrow[3]

        # If the connection is somehow not possible (e.g. target not responding)
        # show an error message instead of excepting and stopping
        try:
            received = client.read_input_registers(
                startPos, count=sma_moddatatype[datatype], **id_kwargs
            )
        except Exception as err:
            thisdate = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logging.error(
                f"{thisdate}: Connection not possible, check settings or connection ({err})"
            )
            return

        if received.isError() or not hasattr(received, "registers"):
            logging.error("Failed reading SMA register %s (%s): %s", name, startPos, received)
            return

        interpreted = decode_sma_registers(received.registers, datatype)

        # Check for "None" data before doing anything else
        if (interpreted == MIN_SIGNED) or (interpreted == MAX_UNSIGNED):
            displaydata = None
        else:
            # Put the data with correct formatting into the data table
            if fmt == "FIX3":
                displaydata = float(interpreted) / 1000
            elif fmt == "FIX2":
                displaydata = float(interpreted) / 100
            elif fmt == "FIX1":
                displaydata = float(interpreted) / 10
            else:
                displaydata = interpreted

        logging.debug(f"************** {name} = {displaydata}")
        inverter[name] = displaydata

    # Add timestamp
    inverter["00000 - Timestamp"] = datetime.datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def publish_prometheus(inverter):
    result = prom_client.publish_status(inverter)
    if result:
        logging.info("Published to prometheus")


def publish_influx(metrics):
    target = flux_client.write_points([metrics])
    logging.info("Published to InfluxDB")
    return target


def publish_mqtt(snapshot):
    try:
        if not mqtt_connected.wait(timeout=5) and not mqtt_client.is_connected():
            logging.warning("MQTT client is not connected; skipping publish")
            return None

        result = mqtt_client.publish(config.mqtt_topic, json.dumps(snapshot))
        result.wait_for_publish(timeout=5)

        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logging.error(f"Failed to publish to MQTT with error code: {result.rc}")
        else:
            logging.info("Published to MQTT")

        return result
    except (RuntimeError, ValueError, OSError) as err:
        logging.error("Error publishing to MQTT: %s", err)
        return None


def publish_pvoutput(snapshot):
    try:
        result = pvoutput_client.publish_status(snapshot)
    except Exception as err:
        logging.error("Error publishing to PVOutput: %s", err)
        return None

    if result == "skipped":
        logging.info("Skipping PVOutput to stay under the rate limit")
    else:
        logging.info("Published to PVOutput")
    return result


def save_json(snapshot, append_log):
    try:
        # Default to one line per file.
        # If the config.append_log value is true, then open the file in append mode.
        # We will also need newlines if we are in append mode. Add that as a suffix
        file_mode = "w"
        suffix = ""
        if append_log is True:
            file_mode = "a"
            suffix = "\n"
        with open(config.json_file, file_mode) as f:
            f.write(json.dumps(snapshot) + suffix)
    except Exception as err:
        logging.error("Error writing telemetry to file: %s" % err)
        return
    logging.info("Inverter telemetry written to %s file." % config.json_file)


# Core monitoring loop


def scrape_inverter():
    """Connect to the inverter and scrape the metrics"""
    logging.debug("Begin scraping...")
    if not client.connect():
        logging.warning("Failed to connect to inverter")
        return False

    try:
        if "sungrow-" in config.model:
            for i in bus["read"]:
                if not load_registers("read", i["start"], int(i["range"])):
                    return False

            for i in bus["holding"]:
                if not load_registers("holding", i["start"], int(i["range"])):
                    return False

            # Sungrow inverter specifics:
            # Work out if the grid power is being imported or exported
            if config.model == "sungrow-sh5k":
                try:
                    if inverter["grid_import_or_export"] == 65535:
                        export_power = (65535 - inverter["export_power"]) * -1
                        inverter["export_power"] = export_power
                except Exception:
                    pass

            try:
                inverter["timestamp"] = "%s/%s/%s %s:%02d:%02d" % (
                    inverter["day"],
                    inverter["month"],
                    inverter["year"],
                    inverter["hour"],
                    inverter["minute"],
                    inverter["second"],
                )
            except Exception:
                pass
        elif "sma-" in config.model:
            load_sma_register(modmap.sma_registers)
        else:
            raise RuntimeError(f"Unsupported inverter model detected: {config.model}")
    finally:
        client.close()

    # Filter out the keys we want to log
    # keys_to_log = ['battery_level', 'battery_voltage', 'daily_export_energy', 'daily_charge_energy', 'daily_use_energy', 'grid_voltage_a', 'grid_voltage_b', 'grid_voltage_c']

    # Otherwise log everything
    keys_to_log = []

    if keys_to_log:
        inverter_to_log = {k: inverter[k] for k in keys_to_log if k in inverter}
    else:
        inverter_to_log = inverter

    logging.debug("Done scraping data:\n %s", json.dumps(inverter_to_log, indent=2))
    return True


def reset_connection():
    local_log = logging.getLogger("reset")
    # work around issue with Sungrow SG5KD where once in a while it stops responding to modbus comms
    local_log.info("trying to reset_connection")
    timeout_seconds = 5
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_seconds)
    try:
        result = sock.connect_ex((config.inverter_ip, config.inverter_port))
        if result == 0:
            local_log.info("connection succeeded")
            time.sleep(5)
        else:
            local_log.info("connection failed")
    finally:
        local_log.info("closing socket and sleeping one scan interval")
        sock.close()
    time.sleep(config.scan_interval)
    local_log.info("finished reset_connection")


shutdown_event = Event()


def _handle_shutdown(signum, frame):
    logging.info("Received signal %s; shutting down", signum)
    shutdown_event.set()


signal.signal(signal.SIGINT, _handle_shutdown)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, _handle_shutdown)

failcount = 0
cumulativefailcount = 0
if args.sungrow_reset:
    reset_connection()

threads = []
try:
    while not shutdown_event.is_set():
        # Scrape the inverter
        success = scrape_inverter()

        if success:
            failcount = 0
            cumulativefailcount = 0

        if not success:
            # reset counters otherwise prometheus will keep on reporting whatever was pushed last
            if prom_client is not None:
                prom_client.Clear_status()
            logging.warning("Failed to scrape inverter, sleeping until next scan")
            shutdown_event.wait(config.scan_interval)

            if args.sungrow_reset:
                failcount += 1
                cumulativefailcount += 1

                if cumulativefailcount > 50:
                    logging.error("[reset] too many failures, quitting")
                    sys.exit(1)

                if failcount >= 10:
                    logging.info("[reset] fail count reached 10")
                    reset_connection()
                    failcount = 0

            continue

        # Take an immutable snapshot so publisher threads never race with the next scrape
        snapshot = inverter.copy()
        threads = []

        # Optionally publish the metrics if enabled
        if mqtt_client is not None:
            t = Thread(target=publish_mqtt, args=(snapshot,))
            t.start()
            threads.append(t)

        if prom_client is not None:
            t = Thread(target=publish_prometheus, args=(snapshot,))
            t.start()
            threads.append(t)

        if flux_client is not None:
            metrics = {
                "measurement": "Sungrow",
                "tags": {
                    "location": "Gabba",
                },
                "fields": snapshot,
            }

            t = Thread(target=publish_influx, args=(metrics,))
            t.start()
            threads.append(t)

        if pvoutput_client is not None:
            t = Thread(target=publish_pvoutput, args=(snapshot,))
            t.start()
            threads.append(t)

        if hasattr(config, "json_file"):
            t = Thread(
                target=save_json,
                args=(snapshot, getattr(config, "append_log", False)),
            )
            t.start()
            threads.append(t)

        if args.one_shot:
            logging.info("Exiting due to --one-shot")
            break

        # Sleep until the next scan (wakes immediately on SIGINT/SIGTERM)
        shutdown_event.wait(config.scan_interval)
finally:
    for t in threads:
        t.join(timeout=10)
    if mqtt_client is not None:
        try:
            mqtt_client.disconnect()
        except Exception:
            pass
        mqtt_client.loop_stop()
    client.close()
