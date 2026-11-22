"""Unit tests for SolarIoT (Sungrow AES client, pymodbus 3.x, paho-mqtt 2.x, SG5KD)."""

import json
import logging
import os
import runpy
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class FakeECB:
    """Reversible 16-byte block XOR cipher stub for deterministic AES-ECB framing tests."""

    def __init__(self, key):
        if len(key) != 16:
            raise ValueError(f"Expected 16-byte key, got {len(key)}")
        self.key = key

    def _xor(self, data):
        if len(data) % 16 != 0:
            raise ValueError(f"Data length {len(data)} is not a multiple of 16")
        return bytes(b ^ self.key[i % 16] for i, b in enumerate(data))

    encrypt = _xor
    decrypt = _xor


class FakeSocket:
    """In-memory TCP socket returning small chunks to exercise exact-byte reads."""

    def __init__(self, handler, max_chunk=5):
        self._handler = handler
        self._max_chunk = max_chunk
        self._inbuf = bytearray()
        self.closed = False

    def settimeout(self, _timeout):
        pass

    def sendall(self, data):
        resp = self._handler(data)
        if resp:
            self._inbuf.extend(resp)

    def recv(self, n):
        if not self._inbuf:
            return b""
        chunk = bytes(self._inbuf[: min(n, self._max_chunk)])
        del self._inbuf[: len(chunk)]
        return chunk

    def close(self):
        self.closed = True


def _import_sungrow_client_with_fake_aes():
    fake_aes = types.SimpleNamespace(MODE_ECB=1, new=lambda key, mode: FakeECB(key))
    fake_pkg = types.ModuleType("Cryptodome")
    fake_cipher = types.ModuleType("Cryptodome.Cipher")
    fake_cipher.AES = fake_aes
    prev_modules = {
        name: sys.modules.get(name)
        for name in ("Cryptodome", "Cryptodome.Cipher", "sungrow_tcp_client")
    }
    sys.modules["Cryptodome"] = fake_pkg
    sys.modules["Cryptodome.Cipher"] = fake_cipher
    sys.modules.pop("sungrow_tcp_client", None)
    import sungrow_tcp_client as stc

    return stc, prev_modules


def _restore_modules(prev_modules):
    for name, mod in prev_modules.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


class TestSungrowModbusTcpClient(unittest.TestCase):
    def setUp(self):
        self.stc, self._prev_mods = _import_sungrow_client_with_fake_aes()

    def tearDown(self):
        _restore_modules(self._prev_mods)

    def _build_encrypted_handler(self, pub_key):
        derived = bytes(a ^ b for a, b in zip(pub_key, self.stc.PRIV_KEY))
        cipher = FakeECB(derived)

        def handler(data):
            if data == self.stc.GET_KEY:
                return b"\x68\x68\x00\x00\x00\x13\xf7\x04\x10" + pub_key
            self.assertEqual(data[0], 1)
            self.assertEqual(data[1], 0)
            pkt_len, pad = data[2], data[3]
            self.assertEqual(len(data) - 4, pkt_len + pad)
            plain = cipher.decrypt(data[4:])[:pkt_len]
            self.assertEqual(plain[:2], self.stc.HEADER)
            proto, length, unit, fc, start, cnt = struct.unpack(">HHBBHH", plain[2:12])
            self.assertEqual(proto, 0)
            self.assertEqual(length, 6)
            if start == 9999:
                resp = self.stc.HEADER + struct.pack(">HHBBB", 0, 3, unit, fc | 0x80, 0x02)
            else:
                regs = [(start + i) & 0xFFFF for i in range(cnt)]
                pdu = struct.pack(">BB", fc, cnt * 2) + struct.pack(f">{cnt}H", *regs)
                resp = self.stc.HEADER + struct.pack(">HHB", 0, len(pdu) + 1, unit) + pdu
            rpad = 16 - (len(resp) % 16)
            return bytes([1, 0, len(resp), rpad]) + cipher.encrypt(resp + b"\xff" * rpad)

        return handler

    def test_encrypted_handshake_fc04_fc03_and_fragmented_reads(self):
        pub_key = bytes(range(1, 17))
        handler = self._build_encrypted_handler(pub_key)
        conn_count = 0

        def fake_connect(addr, timeout=3):
            nonlocal conn_count
            conn_count += 1
            return FakeSocket(handler, max_chunk=5)

        with patch.object(self.stc.socket, "create_connection", side_effect=fake_connect):
            client = self.stc.SungrowModbusTcpClient("192.168.1.23", port=502, timeout=3, retries=2)
            self.assertTrue(client.connect())
            # 1st connect: GET_KEY socket + fresh encrypted socket
            self.assertEqual(conn_count, 2)

            rr1 = client.read_input_registers(5000, count=100, device_id=1)
            self.assertFalse(rr1.isError())
            self.assertEqual(len(rr1.registers), 100)
            self.assertEqual(rr1.registers[0], 5000)
            self.assertEqual(rr1.registers[99], 5099)

            rr2 = client.read_holding_registers(4999, count=12, device_id=1)
            self.assertFalse(rr2.isError())
            self.assertEqual(len(rr2.registers), 12)
            self.assertEqual(rr2.registers[0], 4999)
            client.close()

            # 2nd connect same day reuses cached daily key -> single TCP connect
            self.assertTrue(client.connect())
            self.assertEqual(conn_count, 3)
            rr3 = client.read_input_registers(5100, count=100, device_id=1)
            self.assertFalse(rr3.isError())
            self.assertEqual(rr3.registers[0], 5100)
            client.close()

    def test_modbus_exception_response(self):
        handler = self._build_encrypted_handler(bytes(range(1, 17)))
        with patch.object(
            self.stc.socket,
            "create_connection",
            side_effect=lambda addr, timeout=3: FakeSocket(handler),
        ):
            client = self.stc.SungrowModbusTcpClient("192.168.1.23", port=502, retries=2)
            self.assertTrue(client.connect())
            rr = client.read_input_registers(9999, count=10, device_id=1)
            self.assertTrue(rr.isError())
            self.assertEqual(rr.exception_code, 0x02)
            client.close()

    def test_key_invalidation_and_renegotiation_on_stale_key(self):
        # Inverter starts with key1, then rotates to key2 (simulating a mid-day reboot)
        key1 = bytes(range(1, 17))
        key2 = bytes(range(17, 33))
        state = {"active_key": key1, "key_requests": 0}

        def make_socket(addr, timeout=3):
            active_handler = self._build_encrypted_handler(state["active_key"])

            def wrapped_handler(data):
                if data == self.stc.GET_KEY:
                    state["key_requests"] += 1
                    return active_handler(data)
                # If client encrypted with stale key1 while inverter now uses key2,
                # decrypted protocol header won't match -> return garbage crypto header
                derived = bytes(a ^ b for a, b in zip(state["active_key"], self.stc.PRIV_KEY))
                plain = FakeECB(derived).decrypt(data[4:])[: data[2]]
                if plain[:2] != self.stc.HEADER:
                    return b"\x00\x00\x00\x00"  # forces invalid header -> key invalidation
                return active_handler(data)

            return FakeSocket(wrapped_handler)

        with patch.object(self.stc.socket, "create_connection", side_effect=make_socket):
            client = self.stc.SungrowModbusTcpClient("192.168.1.23", port=502, retries=3)
            self.assertTrue(client.connect())
            self.assertEqual(state["key_requests"], 1)

            # Simulate inverter reboot changing its daily pub_key
            state["active_key"] = key2
            rr = client.read_input_registers(5000, count=10, device_id=1)
            self.assertFalse(rr.isError())
            self.assertEqual(rr.registers, list(range(5000, 5010)))
            self.assertEqual(state["key_requests"], 2)
            client.close()

    def test_unencrypted_fallback(self):
        def plain_handler(data):
            if data == self.stc.GET_KEY:
                return b"\x68\x68\x00\x00\x00\x13\xf7\x04\x10" + self.stc.NO_CRYPTO1
            tx_id, proto, length, unit, fc, start, cnt = struct.unpack(">HHHBBHH", data)
            regs = [start + i for i in range(cnt)]
            pdu = struct.pack(">BB", fc, cnt * 2) + struct.pack(f">{cnt}H", *regs)
            return struct.pack(">HHHB", tx_id, 0, len(pdu) + 1, unit) + pdu

        with patch.object(
            self.stc.socket,
            "create_connection",
            side_effect=lambda addr, timeout=3: FakeSocket(plain_handler),
        ):
            client = self.stc.SungrowModbusTcpClient("192.168.1.23", port=502, retries=2)
            self.assertTrue(client.connect())
            self.assertIsNone(client._aes_ecb)
            rr = client.read_input_registers(5000, count=10, device_id=1)
            self.assertFalse(rr.isError())
            self.assertEqual(rr.registers, list(range(5000, 5010)))
            client.close()


class TestSolarIoTEndToEnd(unittest.TestCase):
    def _run_solariot(self, cfg_module, argv, extra_modules=None):
        """Execute solariot.py in isolation with mocked dependencies."""
        extra_modules = extra_modules or {}
        # Ensure optional unused packages are NOT imported unless explicitly mocked
        managed_names = set(extra_modules.keys()) | {
            "test_cfg",
            "influxdb",
            "prometheus_client",
            "SungrowModbusWebClient",
            "pymodbus",
            "pymodbus.client",
            "paho",
            "paho.mqtt",
            "paho.mqtt.client",
            "requests",
            "urllib3",
            "sungrow_tcp_client",
        }
        saved = {name: sys.modules.get(name) for name in managed_names}
        saved_argv = sys.argv[:]
        prev_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            for name in managed_names:
                sys.modules.pop(name, None)
            sys.modules["test_cfg"] = cfg_module
            for name, mod in extra_modules.items():
                sys.modules[name] = mod
            sys.argv = argv
            return runpy.run_path(str(REPO_ROOT / "solariot.py"), run_name="__main__")
        finally:
            logging.disable(prev_disable)
            sys.argv = saved_argv
            _restore_modules(saved)

    def _make_fake_mqtt(self):
        published = []
        events = []

        class FakeInfo:
            rc = 0

            def wait_for_publish(self, timeout=None):
                events.append(("wait_for_publish", timeout))

        class FakeMqttClient:
            def __init__(self, callback_api_version=None, client_id=None):
                events.append(("init", callback_api_version, client_id))
                self.on_connect = None
                self.on_disconnect = None
                self._connected = False

            def reconnect_delay_set(self, min_delay=1, max_delay=60):
                events.append(("reconnect_delay_set", min_delay, max_delay))

            def username_pw_set(self, u, p):
                events.append(("auth", u))

            def tls_set(self):
                events.append(("tls",))

            def connect(self, host, port=1883):
                events.append(("connect", host, port))
                self._connected = True

            def connect_async(self, host, port=1883):
                events.append(("connect_async", host, port))

            def loop_start(self):
                events.append(("loop_start",))
                if self.on_connect:
                    self.on_connect(
                        self, None, None, types.SimpleNamespace(is_failure=False), None
                    )

            def loop_stop(self):
                events.append(("loop_stop",))

            def disconnect(self):
                events.append(("disconnect",))
                self._connected = False

            def is_connected(self):
                return self._connected

            def reconnect(self):
                raise AssertionError("reconnect() must not be called per publish")

            def publish(self, topic, payload):
                published.append((topic, json.loads(payload)))
                return FakeInfo()

        fake_paho = types.ModuleType("paho")
        fake_mqtt = types.ModuleType("paho.mqtt")
        fake_client_mod = types.ModuleType("paho.mqtt.client")
        fake_client_mod.Client = FakeMqttClient
        fake_client_mod.CallbackAPIVersion = types.SimpleNamespace(VERSION2="VERSION2")
        fake_client_mod.MQTT_ERR_SUCCESS = 0
        modules = {
            "paho": fake_paho,
            "paho.mqtt": fake_mqtt,
            "paho.mqtt.client": fake_client_mod,
        }
        return modules, published, events

    def _make_sg5kd_sungrow_module(self, fail_on_read=False):
        instances = []

        class FakeSungrowClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.connect_calls = 0
                self.close_calls = 0
                instances.append(self)

            def connect(self):
                self.connect_calls += 1
                return True

            def close(self):
                self.close_calls += 1

            def read_input_registers(self, address, count=1, device_id=1):
                if fail_on_read:
                    return types.SimpleNamespace(isError=lambda: True)
                regs = [0] * count
                if address == 5000:
                    regs[2] = 123      # 5003 daily_power_yield_0.01 -> 12300.0
                    regs[3] = 450      # 5004 total_power_yield_100 -> 4.5
                    regs[7] = 355      # 5008 internal_temp_10 -> 35.5
                    regs[18] = 2401    # 5019 grid_voltage_10 -> 240.1
                    regs[30] = 2500    # 5031 total_active_power -> 2500
                    regs[34] = 995     # 5035 power_factor_1000 -> 0.995
                    regs[35] = 500     # 5036 grid_frequency_10 -> 50.0
                    regs[82] = 64486   # 5083 export_power_overflow -> -1049
                    regs[83] = 65535   # 5084 export_power_indicator
                    regs[90] = 1451    # 5091 power_meter -> 1451
                    regs[92] = 85      # 5093 daily_export_energy_10 -> 8.5
                elif address == 5100:
                    regs[0] = 50       # 5101 daily_energy_consumption_pv_0.01 -> 5000.0
                    regs[2] = 1234     # 5103 total_energy_consumption_pv_10 -> 123.4
                    regs[12] = 420     # 5113 daily_running_time -> 420
                return types.SimpleNamespace(isError=lambda: False, registers=regs)

            def read_holding_registers(self, address, count=1, device_id=1):
                regs = [0] * count
                regs[0:6] = [2026, 11, 22, 9, 45, 5]
                regs[6] = 0xCF   # 5006 start_stop
                regs[7] = 0xAA   # 5007 power_limit
                regs[10] = 5000  # 5010 export_limit
                return types.SimpleNamespace(isError=lambda: False, registers=regs)

        mod = types.SimpleNamespace(SungrowModbusTcpClient=FakeSungrowClient)
        return mod, instances

    def test_sg5kd_decoding_mqtt_pvoutput_json_and_lazy_imports(self):
        mqtt_mods, published, mqtt_events = self._make_fake_mqtt()
        sg_mod, sg_instances = self._make_sg5kd_sungrow_module()
        pv_posts = []
        urllib3_disabled = []

        fake_requests = types.SimpleNamespace(
            codes=types.SimpleNamespace(ok=200),
            post=lambda url, headers, params: (
                pv_posts.append(params),
                types.SimpleNamespace(status_code=200, text="OK"),
            )[1],
        )
        fake_urllib3 = types.SimpleNamespace(
            disable_warnings=lambda: urllib3_disabled.append(True)
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = os.path.join(tmpdir, "telemetry.json")
            cfg = types.ModuleType("test_cfg")
            cfg.inverter_ip = "192.168.1.23"
            cfg.inverter_port = 502
            cfg.slave = 1
            cfg.model = "sungrow-sg5kd"
            cfg.timeout = 3
            cfg.scan_interval = 1
            cfg.mqtt_server = "192.168.1.10"
            cfg.mqtt_port = 1883
            cfg.mqtt_topic = "inverter/stats"
            cfg.pvoutput_api = "api-key"
            cfg.pvoutput_sid = "12345"
            cfg.pvoutput_rate_limit = 60
            cfg.json_file = json_path
            # Intentionally omit append_log to test default fallback

            extra = {
                **mqtt_mods,
                "sungrow_tcp_client": sg_mod,
                "requests": fake_requests,
                "urllib3": fake_urllib3,
            }
            self._run_solariot(cfg, ["solariot.py", "-c", "test_cfg", "--one-shot"], extra)

            # Verify lazy-import guarantee: unused optional modules were never required
            self.assertEqual(len(sg_instances), 1)
            self.assertEqual(urllib3_disabled, [True])

            # Verify MQTT 2.x behavior & exact SG5KD payload keys
            self.assertIn(("init", "VERSION2", "pv_data"), mqtt_events)
            self.assertIn(("loop_start",), mqtt_events)
            self.assertIn(("wait_for_publish", 5), mqtt_events)
            self.assertEqual(mqtt_events[-2:], [("disconnect",), ("loop_stop",)])

            self.assertEqual(len(published), 1)
            topic, payload = published[0]
            self.assertEqual(topic, "inverter/stats")
            self.assertEqual(payload["daily_power_yield"], 12300.0)
            self.assertEqual(payload["total_power_yield"], 4.5)
            self.assertEqual(payload["internal_temp"], 35.5)
            self.assertEqual(payload["grid_voltage"], 240.1)
            self.assertEqual(payload["total_active_power"], 2500)
            self.assertEqual(payload["power_factor"], 0.995)
            self.assertEqual(payload["grid_frequency"], 50.0)
            self.assertEqual(payload["export_power"], -1049)
            self.assertEqual(payload["power_meter"], 1451)
            self.assertEqual(payload["daily_export_energy"], 8.5)
            self.assertEqual(payload["daily_energy_consumption_pv"], 5000.0)
            self.assertEqual(payload["total_energy_consumption_pv"], 123.4)
            self.assertEqual(payload["daily_running_time"], 420)
            self.assertEqual(payload["export_limit"], 5000)
            self.assertEqual(payload["timestamp"], "22/11/2026 9:45:05")

            # Verify PVOutput payload (omits v4 per Dave's SG5KD map)
            self.assertEqual(len(pv_posts), 1)
            self.assertEqual(pv_posts[0]["v1"], 12300.0)
            self.assertEqual(pv_posts[0]["v2"], 2500)
            self.assertEqual(pv_posts[0]["v3"], 5000.0)
            self.assertNotIn("v4", pv_posts[0])
            self.assertEqual(pv_posts[0]["v5"], 35.5)
            self.assertEqual(pv_posts[0]["v6"], 240.1)

            # Verify JSON file output
            with open(json_path) as f:
                saved_json = json.load(f)
            self.assertEqual(saved_json["export_power"], -1049)

    def test_json_file_append_log_mode(self):
        sg_mod, _ = self._make_sg5kd_sungrow_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = os.path.join(tmpdir, "telemetry.jsonl")
            cfg = types.ModuleType("test_cfg")
            cfg.inverter_ip = "192.168.1.23"
            cfg.inverter_port = 502
            cfg.slave = 1
            cfg.model = "sungrow-sg5kd"
            cfg.timeout = 3
            cfg.scan_interval = 1
            cfg.json_file = json_path
            cfg.append_log = True

            for _ in range(2):
                self._run_solariot(
                    cfg,
                    ["solariot.py", "-c", "test_cfg", "--one-shot"],
                    {"sungrow_tcp_client": sg_mod},
                )

            with open(json_path) as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["total_active_power"], 2500)
            self.assertEqual(json.loads(lines[1])["total_active_power"], 2500)

    def test_scrape_closes_socket_on_read_failure(self):
        sg_mod, sg_instances = self._make_sg5kd_sungrow_module(fail_on_read=True)
        cfg = types.ModuleType("test_cfg")
        cfg.inverter_ip = "192.168.1.23"
        cfg.inverter_port = 502
        cfg.slave = 1
        cfg.model = "sungrow-sg5kd"
        cfg.timeout = 3
        cfg.scan_interval = 0

        # Stop after 1 failed iteration by triggering shutdown_event inside wait()
        orig_wait = __import__("threading").Event.wait

        def stop_after_first_wait(self_ev, timeout=None):
            self_ev.set()
            return True

        with patch("threading.Event.wait", new=stop_after_first_wait):
            self._run_solariot(
                cfg,
                ["solariot.py", "-c", "test_cfg"],
                {"sungrow_tcp_client": sg_mod},
            )

        client = sg_instances[0]
        # Startup connect+close (1), failed scrape connect+close (1), finally close (1)
        self.assertEqual(client.connect_calls, 2)
        self.assertGreaterEqual(client.close_calls, 2)

    def test_pymodbus3_id_kwarg_and_sma_struct_decoding(self):
        calls = []

        class FakePymodbus316Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def connect(self):
                return True

            def close(self):
                pass

            def read_input_registers(self, address, *, count=1, device_id=1):
                calls.append((address, count, device_id))
                regs = [0] * count
                return types.SimpleNamespace(isError=lambda: False, registers=regs)

        fake_pymodbus = types.ModuleType("pymodbus")
        fake_pymodbus_client = types.ModuleType("pymodbus.client")
        fake_pymodbus_client.ModbusTcpClient = FakePymodbus316Client

        cfg = types.ModuleType("test_cfg")
        cfg.inverter_ip = "192.168.1.50"
        cfg.inverter_port = 502
        cfg.slave = 3
        cfg.model = "sma-SBn_n-1AV-40"
        cfg.timeout = 3
        cfg.scan_interval = 1

        ns = self._run_solariot(
            cfg,
            ["solariot.py", "-c", "test_cfg", "--one-shot"],
            {"pymodbus": fake_pymodbus, "pymodbus.client": fake_pymodbus_client},
        )
        # Verify RetryOnEmpty was NOT passed and device_id=3 was used
        self.assertNotIn("RetryOnEmpty", ns["client"].kwargs)
        self.assertGreater(len(calls), 0)
        self.assertTrue(all(dev_id == 3 for _, _, dev_id in calls))

        # Verify get_modbus_id_kwarg across pymodbus 3.10+, 3.0-3.9, and legacy 2.x
        get_kw = ns["get_modbus_id_kwarg"]
        self.assertEqual(
            get_kw(types.SimpleNamespace(read_input_registers=lambda a, count=1, device_id=1: None)),
            "device_id",
        )
        self.assertEqual(
            get_kw(types.SimpleNamespace(read_input_registers=lambda a, count=1, slave=1: None)),
            "slave",
        )
        self.assertEqual(
            get_kw(types.SimpleNamespace(read_input_registers=lambda a, count=1, unit=1: None)),
            "unit",
        )

        # Verify decode_sma_registers across all SMA datatypes
        decode = ns["decode_sma_registers"]
        self.assertEqual(decode([0xFFFF], "S16"), -1)
        self.assertEqual(decode([0xFFFF], "U16"), 65535)
        self.assertEqual(decode([0xFFFF, 0xFFF6], "S32"), -10)
        self.assertEqual(decode([0x0001, 0x0002], "U32"), 65538)
        self.assertEqual(decode([0, 0, 0, 42], "U64"), 42)
        str_regs = list(struct.unpack(">8H", b"SunnyBoy\x00\x00\x00\x00\x00\x00\x00\x00"))
        self.assertEqual(decode(str_regs, "STR16"), "SunnyBoy")

    def test_sungrow_reset_thresholds_and_reset_on_success(self):
        # Sequence: 9 failures, 1 success (resets counters!), then 51 failures -> sys.exit(1)
        scrape_results = ([False] * 9) + [True] + ([False] * 55)
        call_idx = {"n": 0}
        reset_connects = []

        class SequenceClient:
            def __init__(self, **kwargs):
                pass

            def connect(self):
                return True

            def close(self):
                pass

            def read_input_registers(self, address, count=1, device_id=1):
                # Only advance once per scrape (on first block address 5000)
                if address == 5000:
                    ok = scrape_results[call_idx["n"]]
                    call_idx["n"] += 1
                    self._ok = ok
                if not self._ok:
                    return types.SimpleNamespace(isError=lambda: True)
                return types.SimpleNamespace(isError=lambda: False, registers=[0] * count)

            def read_holding_registers(self, address, count=1, device_id=1):
                return types.SimpleNamespace(isError=lambda: False, registers=[0] * count)

        class FakeResetSocket:
            def settimeout(self, t):
                pass

            def connect_ex(self, addr):
                reset_connects.append(addr)
                return 0

            def close(self):
                pass

        cfg = types.ModuleType("test_cfg")
        cfg.inverter_ip = "192.168.1.23"
        cfg.inverter_port = 502
        cfg.slave = 1
        cfg.model = "sungrow-sg5kd"
        cfg.timeout = 1
        cfg.scan_interval = 0

        sg_mod = types.SimpleNamespace(SungrowModbusTcpClient=SequenceClient)

        with (
            patch("socket.socket", return_value=FakeResetSocket()),
            patch("time.sleep", return_value=None),
            patch("threading.Event.wait", return_value=False),
        ):
            with self.assertRaises(SystemExit) as cm:
                self._run_solariot(
                    cfg,
                    ["solariot.py", "-c", "test_cfg", "--sungrow-reset"],
                    {"sungrow_tcp_client": sg_mod},
                )

        self.assertEqual(cm.exception.code, 1)
        # 1 startup reset + 5 resets during the 51 consecutive failures (at 10, 20, 30, 40, 50)
        # Note: the first 9 failures did NOT trigger a reset, and the 10th scrape succeeded,
        # resetting cumulativefailcount to 0 so all 51 subsequent failures ran before exit!
        self.assertEqual(len(reset_connects), 6)
        self.assertEqual(call_idx["n"], 9 + 1 + 51)


if __name__ == "__main__":
    unittest.main()
