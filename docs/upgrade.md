# SolarIoT Upgrade Path (Ubuntu 24.04 LTS, paho-mqtt 2.x, pymodbus 3.x)

## 1. Executive Summary & Recommendation

After reviewing all Python code, git history, and dependencies across both `solariot` (upstream) and `solariot-dave` (your fork), the cleanest and lowest-risk upgrade path is:

1. **Start from upstream `solariot` (`beb63a7`) and re-apply your two fork features** (`--sungrow-reset` and your expanded `modbus-sungrow-sg5kd.py`). Your fork is only **2 commits / ~95 lines** ahead of the common ancestor (`ab33810`), while upstream has absorbed 3+ years of fixes—including **`paho-mqtt >= 2.0.0`** support and removal of the dead **`dweepy`** dependency.
2. **Harden the `paho-mqtt` 2.x integration** beyond upstream's minimal constructor fix: switch to `loop_start()`, remove per-scan `reconnect()`, add a publish timeout, and fix thread-safety bugs on the shared `inverter` dictionary.
3. **Migrate to `pymodbus` 3.x (Pre-Flight Completed — AES Encryption Confirmed):** Live testing against your SG5K-D with `pymodbus 3.16.2` confirmed that your inverter requires **Sungrow AES-128-ECB encryption** (`_aes_ecb` active, plain `ModbusTcpClient` times out). We therefore recommend **Option B1**: a self-contained (~100-line) Sungrow AES-128-ECB Modbus TCP client in-repo (stdlib `socket` + `pycryptodomex`/`pycryptodome`) for Sungrow TCP, while upgrading standard/SMA Modbus support to `pymodbus` 3.x. This removes the unmaintained `SungrowModbusTcpClient` package and insulates your SG5K-D polling from future `pymodbus` internal API churn.
4. **Target Ubuntu 24.04 LTS (Python 3.12) inside a dedicated `venv`** (or container) to comply with PEP 668, lazily import optional output backends (`influxdb`, `prometheus_client`, `SungrowModbusWebClient`) so you aren't forced to install legacy/unused packages, and eliminate legacy version pins (`pymodbus==2.4.0`, `paho-mqtt==1.6.1`).

> **Status:** Plan decisions recorded in [Section 8](#8-recorded-decisions--caveats). No source files have been modified yet—ready to proceed once you provide `SungrowModbusTcpClient.py` (for Option B1 byte-framing accuracy) and give the final go-ahead.

---

## 2. Codebase Audit: `solariot` vs `solariot-dave`

Both repositories branch from the same commit: `ab33810` (*Merge pull request #62*, 2021). All `modbus-*.py` register maps are pure Python data modules (`dict`/JSON string definitions) with no external imports and no Python 3.12 compatibility issues. All runtime logic lives in `solariot.py`.

### 2.1 What `solariot-dave` Added (2 commits, 2 files)

| Commit | File | Changes |
|---|---|---|
| `8704db6`, `b73df6f` | `solariot-dave/solariot.py` | Added `--sungrow-reset` CLI flag (`L55`), `reset_connection()` helper opening a raw TCP socket to `(inverter_ip, inverter_port)` with a 5s timeout (`L521–537`), startup reset (`L541–542`), failure tracking (`failcount > 10` triggers reset; `cumulativefailcount > 50` calls `quit()`) (`L548–570`), and dedicated `logging.getLogger('reset')`. |
| `b73df6f` | `solariot-dave/modbus-sungrow-sg5kd.py` | Expanded `read_register` from 18 to 44 registers (power factor, work/fault states, export/import/self-consumption energy counters, high-word `*B` registers, running time, PID state); added `start_stop`, `power_limit`, `export_limit` holding registers; expanded read scan range `5100` (`50` → `100`) and holding range `4999` (`10` → `12`); updated PVOutput mapping (`daily_energy_consumption_pv`, commented out `Power Consumption` / `power_meter`). |

### 2.2 What Upstream `solariot` Gained Since the Fork (PRs #66–#108)

| Upstream Change | Benefit |
|---|---|
| **PR #108 (`beb63a7`)** | Upgraded `requirements.txt` to `paho-mqtt>=2.0.0`, updated `mqtt.Client(...)` to pass `mqtt.CallbackAPIVersion.VERSION2`, removed dead `dweepy` dependency/code, and Black-formatted `solariot.py`. |
| **PR #105 (`9e931c3`)** | Added optional `config.logger_config` (`logging.config.dictConfig`) and timestamps in the default log format. |
| **PR #67 (`2058e4d`)** | Fixed `PrometheusPublisher.Clear_status()` crash (`key.set(0)` → `self.metric_mappings[key].set(0)`) and `prometheus_port` default (`"8000"` → `8000`). |
| **PR #66 (`50dc69c`)** | Added `SungrowModbusWebClient` support for WiNet-S HTTP/WebSocket dongles on port `8082`. |
| **PRs #76, #77, #78, #89** | Added PVOutput extended parameters (`v7`–`v12`, `m1`), optional `pvoutput_cumulative` & `pvoutput_power_only`, and `append_log` mode for `save_json`. |
| **PR #103 & others** | Added register maps (`sh10rs`, `sg7rt`, `scanall`) and updated `sg8kd`, `sh10rt`, `sh5k`. |
| **PRs #69, #73** | Switched `Dockerfile` to Alpine with a non-root `solariot` user (though still on EOL `python:3.9-alpine`). |

### 2.3 What Upstream `solariot` Still Hasn't Solved

Even at `HEAD` (`beb63a7`), upstream `solariot`:
- **Is still pinned to `pymodbus==2.5.3`** (an automated Dependabot branch `dependabot-pip-pymodbus-3.2.2` was never merged because of breaking changes and `SungrowModbusTcpClient`).
- **Still has MQTT reliability and thread-safety issues** in `publish_mqtt()` (see [Section 4](#4-goal-2-paho-mqtt-2x-upgrade--reliability-fixes)).
- **Still leaks open Modbus TCP sockets** when `load_registers()` fails mid-scrape (`scrape_inverter()` returns `False` without calling `client.close()`).
- **Unconditionally imports every optional backend** (`influxdb`, `prometheus_client`, `SungrowModbusWebClient`, `SungrowModbusTcpClient`) at startup, even when unused.
- **Introduced a minor config regression** at `solariot/solariot.py:700`: `save_json(inverter, config.append_log)` raises `AttributeError` if `json_file` is configured without `append_log`.

### 2.4 Goal 4 Verdict: Rebase on Upstream vs Patch `solariot-dave`

| Criterion | Rebase onto Upstream `solariot` (Recommended) | Patch `solariot-dave` In Place |
|---|---|---|
| Effort to carry forward your custom features | Very low — 2 small, self-contained changes | Zero |
| Effort to bring in upstream fixes (`paho-mqtt` 2.x, `dweepy` removal, logging config, Prometheus fix, PVOutput updates) | Free / automatic | Manual cherry-picking across Black-reformatted diffs |
| Ongoing maintenance & readability | Clean single history on top of latest upstream | Permanently diverged from 2021 codebase |
| Risk to existing MQTT consumers | None — we preserve your exact `modbus-sungrow-sg5kd.py` keys | None |

**Recommendation:** Start from upstream `solariot` (`beb63a7`) on a clean branch (or rebase `solariot-dave` onto upstream `master`), re-apply your `--sungrow-reset` feature and `modbus-sungrow-sg5kd.py` verbatim, and layer the `pymodbus` 3.x / MQTT / Python 3.12 upgrades on top.

---

## 3. Goal 1: Ubuntu 24.04 LTS (Python 3.12) & Dependency Strategy

### 3.1 Environment Changes in Ubuntu 24.04 LTS
- **System Python:** Python **3.12** (`/usr/bin/python3`).
- **PEP 668 Enforcement:** Ubuntu 24.04 ships `/usr/lib/python3.12/EXTERNALLY-MANAGED`, which blocks `pip install` into system or `~/.local` site-packages by default. Running inside a dedicated virtual environment (e.g., `/home/dhng/source/solariot/.venv`) is now mandatory rather than optional.
- **Python 3.12 Removals:** `distutils` was removed from the standard library (PEP 632), and newly created `venv`s no longer pre-install `setuptools` / `pkg_resources`. Legacy packages that import `distutils` or `pkg_resources` at runtime will fail unless replaced or avoided.

### 3.2 Clarifying "Without Legacy Pinning" vs Environment Isolation
There are two distinct concerns in your notes:
1. **Removing broken legacy pins** (`pymodbus==2.4.0`, `paho-mqtt==1.6.1`) so the code works with modern, maintained library releases on Python 3.12.
2. **Isolating the runtime** in a `venv` (or container) so future Ubuntu LTS upgrades (24.04 → 26.04) never silently break packages again.

**Recommended approach:**
- Use a `venv` (or Docker) managed via `pyproject.toml` (or clean `requirements.txt`) with **broad major-version bounds** rather than exact legacy pins—e.g. `paho-mqtt>=2.0,<3`, `pymodbus>=3.7,<4` (or `>=3.10,<4`).
- Make non-core outputs **lazy-imported** in `solariot.py` and grouped as optional extras (e.g., `[mqtt]`, `[influxdb]`, `[prometheus]`, `[pvoutput]`, `[webclient]`) or clearly separated in `requirements.txt`. If you only use MQTT (and optionally PVOutput), you will never need to install the legacy `influxdb` 1.x package or `SungrowModbusWebClient`.

### 3.3 Dependency-by-Dependency Audit for Python 3.12

| Package | Current (`solariot-dave`) | Status on Python 3.12 / Ubuntu 24.04 | Planned Action |
|---|---|---|---|
| `paho-mqtt` | `>=1.5.1` (downgraded to `1.6.1`) | `2.x` fully supports Python 3.12 | Upgrade to `paho-mqtt>=2.0,<3` and modernize client usage |
| `pymodbus` | `==2.4.0` (upstream: `==2.5.3`) | `3.x` fully supports Python 3.12 | Upgrade to `pymodbus>=3.7,<4` (see Section 5) |
| `SungrowModbusTcpClient` | `>=0.1.5` (latest `0.1.6`, Oct 2021) | **Broken on `pymodbus` 3.x** (imports removed `pymodbus.client.sync`) | Drop if SG5K-D is unencrypted, OR replace with in-repo `pymodbus` 3.x wrapper (see Section 5) |
| `SungrowModbusWebClient` | Not in fork (upstream: `>=0.2.4`) | Only needed for WiNet-S on port `8082` | Lazy-import only when `inverter_port == 8082`; optional dependency |
| `dweepy` | `>=0.3.0` | Dead service / unmaintained | **Remove** (already removed in upstream PR #108) |
| `influxdb` | `>=5.3.1` | InfluxDB 1.x client is EOL; `5.3.2` still works on 3.12 if needed, but shouldn't be mandatory | Lazy-import only when `influxdb_ip` is configured; optional dependency |
| `prometheus-client` | `>=0.9` | Actively maintained, Python 3.12 compatible | Lazy-import only when `prometheus` is configured; optional dependency |
| `requests` | Implicitly installed via `dweepy`/`influxdb`! | Actively maintained, Python 3.12 compatible | **Explicitly declare** (or lazy-import only when `pvoutput_api` is configured) — note that once `dweepy` and `influxdb` are removed/optional, `requests` would otherwise go missing! |

> **Important catch:** Neither `solariot-dave/requirements.txt` nor upstream `solariot/requirements.txt` explicitly lists `requests`—it was only available transitively via `influxdb` (and previously `dweepy`). If `influxdb` becomes optional, `requests` must be explicitly listed (or scoped to PVOutput).

### 3.4 Python 3.12 Code Hygiene
- Replace `requests.packages.urllib3.disable_warnings()` with `urllib3.disable_warnings()` (only when `requests`/PVOutput is enabled).
- Replace `quit()` (`solariot-dave/solariot.py:564`) with `sys.exit(1)` so `systemd` (`Restart=on-failure`) detects abnormal termination and restarts cleanly.
- Remove the broken/no-op `.replace('"', '\"')` (upstream: `.replace('"', '"')`) in `publish_mqtt()` and pass `json.dumps(inverter)` directly.
- If Docker is used in the future, bump `Dockerfile` from EOL `python:3.9-alpine` to `python:3.12-slim` (or `python:3.12-alpine`).
- Provide a clean `systemd` unit example pointing at `/home/dhng/source/solariot/.venv/bin/python` for running in your Ubuntu 24.04 LXC.

---

## 4. Goal 2: `paho-mqtt` 2.x Upgrade & Reliability Fixes

### 4.1 Breaking Change in `paho-mqtt` 2.x
- `mqtt.Client(...)` now requires `callback_api_version` as the first parameter:
  ```python
  mqtt_client = mqtt.Client(
      callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
      client_id=getattr(config, "mqtt_client_name", "pv_data"),
  )
  ```
- Upstream PR #108 already applied this minimal constructor fix.

### 4.2 Existing MQTT Bugs in Both Repos That Should Be Fixed Together
Reviewing `publish_mqtt()` (`solariot-dave/solariot.py:442–455`, `solariot/solariot.py:555–570`) revealed several latent issues that become more pronounced under `paho-mqtt` 2.x:

1. **No network loop (`loop_start()`) + unbounded `wait_for_publish()`:**
   - Neither repo ever calls `mqtt_client.loop_start()`. Calling `result.wait_for_publish()` without a running network loop can block indefinitely if the socket drops, the send buffer fills, or QoS ≥ 1 is ever used (and in `paho-mqtt` 2.x can raise `RuntimeError`/`ValueError` when disconnected).
2. **Forced `mqtt_client.reconnect()` on every scan interval:**
   - Reconnecting before every publish tears down and rebuilds TCP/MQTT (and TLS on port 8883) every 10 seconds, spams broker logs, and can race if a previous thread is still mid-publish.
3. **Thread-safety bug with shared `inverter` dict:**
   - Main loop spawns `Thread(target=publish_mqtt, args=(inverter,))` while mutating the same global `inverter` dict on the next scan. Passing `inverter.copy()` prevents `RuntimeError: dictionary changed size during iteration` and torn reads.
4. **Unhandled exceptions in background threads:**
   - If the MQTT broker restarts, `reconnect()`/`publish()` raises an unhandled `OSError` inside the worker thread.
5. **`--one-shot` exit race:**
   - With `--one-shot`, the main thread breaks and exits immediately without joining publisher threads, potentially dropping the final MQTT publish.

### 4.3 Proposed `paho-mqtt` 2.x Implementation
- Instantiate `mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=...)`.
- Attach lightweight `VERSION2` `on_connect(client, userdata, flags, reason_code, properties)` and `on_disconnect(client, userdata, disconnect_flags, reason_code, properties)` callbacks for clear connection logging.
- Call `mqtt_client.connect(...)` followed by `mqtt_client.loop_start()` once at startup so paho manages keepalives and automatic reconnection in the background.
- In `publish_mqtt(snapshot)`:
  - Do **not** call `reconnect()` on every publish.
  - Check `mqtt_client.is_connected()`, publish `json.dumps(snapshot)`, and call `result.wait_for_publish(timeout=5)` wrapped in `try/except`.
- Join publisher threads on `--one-shot` and register `SIGINT`/`SIGTERM` cleanup to call `mqtt_client.loop_stop()` and `mqtt_client.disconnect()`.

---

## 5. Goal 3: `pymodbus` 3.x Migration Analysis

### 5.1 The Core Blocker: `SungrowModbusTcpClient`
The PyPI package `SungrowModbusTcpClient` (last released as `0.1.6` in October 2021, from `rpvelloso/Sungrow-Modbus`) is unmaintained and breaks under `pymodbus` 3.x because:
- It imports `from pymodbus.client.sync import ModbusTcpClient` (removed in `pymodbus` 3.0).
- It subclasses and overrides low-level `pymodbus` 2.x transport internals (`_send`, `_recv`) that changed across `pymodbus` 3.x.

**What `SungrowModbusTcpClient` actually does:**
- On `connect()`, it attempts a Sungrow-specific key exchange to check whether the inverter wraps Modbus TCP frames in **AES-128-ECB encryption** (used by certain Sungrow WiFi modules / newer firmware).
- If the inverter does **not** use encryption, `SungrowModbusTcpClient` silently falls back to **standard unencrypted Modbus TCP**!

### 5.2 Pre-Flight Check Results (Completed)

Testing on your LXC against your SG5K-D confirmed:
- **Installed `pymodbus` version:** `3.16.2` (uses `device_id=`, refactored `pymodbus.transaction.transaction.sync_execute` internals).
- **Plain `ModbusTcpClient` test:** `Connected: True` on port `502`, followed by `ModbusIOException: No response received after 3 retries` (inverter ignores unencrypted frames).
- **Existing `SungrowModbusTcpClient` inspection:** Confirmed active **AES-128-ECB** encryption (`_priv_key`, `_pub_key`, `_key`, `_key_date`, `_key_packet`, and `Cryptodome.Cipher._mode_ecb.EcbMode` all populated on connect).

**Conclusion:** Option A (plain `ModbusTcpClient` alone) is ruled out for your SG5K-D. We need **Option B**.

### 5.3 Options for Handling Sungrow AES + `pymodbus` 3.x

| Option | Description | Effort | Pros | Cons |
|---|---|---|---|---|
| **Option B1: Self-contained Sungrow AES Modbus TCP client in-repo (Recommended)** | Implement a ~100–120 line in-repo `SungrowModbusTcpClient` using stdlib `socket` + `struct` + `pycryptodomex` (or `pycryptodome`) covering the Sungrow AES handshake + Modbus FC03/FC04 (`read_holding_registers` / `read_input_registers`). Keep `pymodbus` 3.x upgraded for standard/SMA Modbus TCP. | **Low–Medium** (~2–3 hrs + live inverter test) | **100% immune to `pymodbus` internal transport refactors** (like 3.8–3.16's `TransactionManager` rewrite); tiny, easy-to-audit code; only depends on actively maintained `pycryptodomex`/`pycryptodome` (Python 3.12+); drops dead PyPI wrapper. | Implements Modbus TCP FC03/FC04 framing directly (though FC03/FC04 is only ~20 lines of `struct.pack`/`unpack`). |
| **Option B2: Subclass `pymodbus` 3.x internals (`ModbusTcpClient` / `Framer`)** | Port `SungrowModbusTcpClient` to subclass `pymodbus` 3.16's internal transport/framer layer. | **Medium** (~3–5 hrs + live inverter test) | Reuses `pymodbus` PDU encoding/decoding. | `pymodbus` 3.x has repeatedly refactored private transport/framer internals across minor releases (3.0 → 3.7 → 3.10 → 3.16); high risk of breaking again without tight version pins. |
| **Option C: Pin `pymodbus==2.5.3` inside a Python 3.12 `venv`** | Keep legacy `SungrowModbusTcpClient` + `pymodbus==2.5.3` isolated in a `venv`. | **Very Low** (~30 min) | Zero Modbus code changes. | Does **not** meet Goal 1 ("without legacy pinning") or Goal 3 (`pymodbus` 3.x); leaves you on a 2021-era Modbus stack. |

*(Side note: If you ever wanted to replace `solariot` altogether, actively maintained community tools like `SunGather` exist for Sungrow → MQTT. However, keeping your lean `solariot` script preserves your exact MQTT topic/JSON key schema without reconfiguring downstream Home Assistant/Grafana consumers.)*

### 5.4 Exact `pymodbus` 2.x → 3.x Code Changes Required in `solariot.py`

Every place `solariot.py` interacts with `pymodbus` and what changes in `pymodbus` 3.x:

1. **Client import (`solariot.py:26`):**
   - Old: `from pymodbus.client.sync import ModbusTcpClient`
   - New: `from pymodbus.client import ModbusTcpClient`

2. **Client constructor kwargs (`solariot.py:119–125`):**
   - Old: `{"host": ..., "port": ..., "timeout": ..., "retries": 3, "RetryOnEmpty": True}`
   - Breaking change: `RetryOnEmpty` was renamed to `retry_on_empty` in `3.0` and **removed in `3.7+`** (raises `TypeError`).
   - New: Pass only `host`, `port`, `timeout`, `retries`.

3. **Register read calls & `unit=` → `slave=` → `device_id=` (`solariot.py:381–391`, `487–489`):**
   - `pymodbus` 2.x used `unit=config.slave`.
   - `pymodbus` 3.0–3.9 renamed it to `slave=config.slave` (and made `count` keyword-only in 3.7+).
   - **`pymodbus` 3.10+ renamed `slave=` to `device_id=config.slave`!**
   - **Recommended handling:** Use a small 5-line compatibility helper (inspecting `read_input_registers`'s signature once at startup to pick `"device_id"` vs `"slave"`), OR target `pymodbus>=3.10,<4` directly with `device_id=`. The signature helper means the code works seamlessly across **all** `pymodbus` 3.x versions (3.7 through 3.12+) without fragile minor-version pinning.

4. **SMA payload decoder (`BinaryPayloadDecoder` / `Endian`, `solariot.py:25, 27, 497–518`):**
   - `BinaryPayloadDecoder` and `Endian` were deprecated in `3.7` and **removed in `3.9+`**.
   - Only used in `load_sma_register()` (not used for Sungrow).
   - Replace with Python's stdlib `struct.unpack` (or `client.convert_from_registers(...)`), which has zero `pymodbus` version drift and keeps SMA support working cleanly if retained.

5. **Connection & error-handling fixes (`solariot.py:139–140`, `394–410`, `608–645`):**
   - Check the boolean return value of `client.connect()` before issuing reads.
   - Wrap `scrape_inverter()` in `try ... finally: client.close()` so failed scrapes **always close the TCP socket** (see Section 6.1).
   - Log the actual exception `err` in `load_registers()` (`logging.warning("Modbus read failed: %s", err)`) instead of silently discarding it.
   - Fix `raise RuntimeError(f"Unsupported register type: {type}")` → `{register_type}`, and change bare `return` statements in `load_registers()` to explicit `return False`.

---

## 6. Additional Bugs & Improvements Found During Review

While reviewing both codebases, several other bugs worth addressing during the rework stood out:

1. **Socket leak on failed scrape (likely related to your SG5K-D lockups!):**
   - In both `solariot-dave/solariot.py:479–516` and upstream `solariot/solariot.py:608–645`, `scrape_inverter()` calls `client.connect()`, then returns `False` early if any `load_registers()` call fails—**skipping `client.close()`**.
   - Sungrow inverters have very low concurrent TCP connection limits (often 1–2 slots). Leaking an unclosed socket on a transient timeout can leave the inverter wedged until the stale socket times out or is reset. Wrapping the scrape in `try ... finally: client.close()` should make communication noticeably more stable alongside `--sungrow-reset`.
2. **Upstream `config.append_log` regression (`solariot/solariot.py:700`):**
   - Upstream passes `config.append_log` directly instead of `getattr(config, "append_log", False)`, which crashes if `json_file` is enabled in an older `config.py`. We will fix this with `getattr`.
3. **`--sungrow-reset` polish (`solariot-dave/solariot.py:521–570`):**
   - Change `failcount > 10` (triggers on 11th failure) to `failcount >= 10` (or keep `>= 10` and document clearly).
   - Change `quit()` to `sys.exit(1)` for proper exit code propagation under `systemd`.
   - Confirm whether `cumulativefailcount` should remain a lifetime counter (current behavior: exits after 50 total failures ever, even across weeks) or only count failures within a window / since last successful reset.
   - Note: With upstream's `config.logger_config`, the `'reset'` child logger will inherit root handlers automatically as long as `disable_existing_loggers` is `False`.
4. **Register map backward compatibility (`modbus-sungrow-sg5kd.py`):**
   - We will keep **every existing MQTT JSON key name and scaling factor identical** so your existing Home Assistant / Grafana / MQTT dashboards continue working without changes.
   - *(Optional future enhancement, out of scope for initial upgrade):* Several 32-bit Sungrow registers are currently emitted as separate 16-bit low/high keys (e.g., `total_power_yield` + `total_power_yieldB`, `total_active_power` + `total_active_powerB`) because `load_registers()` decodes single 16-bit registers. Leaving these untouched avoids breaking downstream consumers; we can add proper 32-bit decoding later if you ever want it.

---

## 7. Phased Execution Plan

Once you endorse this plan, work will proceed in clear, testable phases:

### Phase 0 — Pre-Flight Verification (Complete)
- [x] Verified SG5K-D uses **AES-128-ECB encryption** (rules out plain `ModbusTcpClient`; selects **Option B1**).
- [x] Confirmed active outputs: **MQTT**, **PVOutput**, and **JSON file** (Prometheus, InfluxDB, and dweepy unused).
- [x] Reviewed reference `SungrowModbusTcpClient.py` (97 lines): confirmed `GET_KEY` handshake (`0x6868` header, 25-byte key response, XOR with `PRIV_KEY`, daily key rotation), 4-byte `[1, 0, length, padding]` crypto header with `0xff` AES-ECB padding, and post-key-exchange reconnect. Also noted two improvements for our Option B1 client: (1) use a proper exact-byte `recv` loop so partial TCP reads don't drop frames, and (2) only perform the second TCP connect when a new key was actually negotiated rather than opening two connections on every poll.

### Phase 1 — Establish Clean Codebase on `solariot-dave` `master` (Complete — commit `d33eb52`)
- [x] Created safety backup tag `backup-2021-master` on `solariot-dave` pointing to `b73df6f`.
- [x] Reset `solariot-dave` `master` to upstream `solariot` (`beb63a7`).
- [x] Re-applied your custom `modbus-sungrow-sg5kd.py` verbatim.
- [x] Re-applied `--sungrow-reset` + `reset_connection()` with `sys.exit(1)`, `failcount >= 10`, clean `try/finally` socket close, and resetting both `failcount` and `cumulativefailcount` to `0` after any successful scrape.
- [x] Fixed the upstream `getattr(config, "append_log", False)` bug so your existing `json_file` config does not crash.

### Phase 2 — `pymodbus` 3.x Migration & Option B1 Sungrow AES Client (Complete — commit `cbab7d5`)
- [x] Implemented **Option B1** in [sungrow_tcp_client.py](../sungrow_tcp_client.py): self-contained Sungrow AES-128-ECB Modbus TCP client using stdlib `socket` + `struct` + `pycryptodomex`, supporting FC03/FC04, exact-byte socket reads, post-key-exchange reconnect (without double-connecting on cached-key polls), automatic key invalidation on framing/crypto error, and automatic fallback to unencrypted Modbus TCP.
- [x] Upgraded standard/SMA Modbus TCP code path in [solariot.py](../solariot.py) to `pymodbus` 3.x (`pymodbus.client.ModbusTcpClient`, removed `RetryOnEmpty`, replaced `BinaryPayloadDecoder`/`Endian` with stdlib `struct` in `decode_sma_registers()`, added dynamic `device_id=`/`slave=`/`unit=` detection).
- [x] Lazy-imported `pymodbus`, `SungrowModbusWebClient`, and `sungrow_tcp_client` so Sungrow port `502` has zero runtime dependency on `pymodbus` internals.
- [x] Fixed `scrape_inverter()` to check `client.connect()` and always close the socket via `try ... finally: client.close()`.
- [x] Added actual exception/error logging in `load_registers()` and `load_sma_register()`, and fixed bare `return` / `{type}` bugs.

### Phase 3 — `paho-mqtt` 2.x Hardening & Thread Safety (Complete)
- [x] Kept `CallbackAPIVersion.VERSION2` with explicit keyword arguments and added v2 `on_mqtt_connect` / `on_mqtt_disconnect` logging callbacks + `reconnect_delay_set(min_delay=1, max_delay=60)`.
- [x] Started `loop_start()` once at startup (falling back to `connect_async()` if broker is unreachable at boot), removed per-scan `reconnect()`, and removed the no-op `.replace('"', '"')`.
- [x] Added `mqtt_connected` event + bounded `wait_for_publish(timeout=5)` with exception handling in `publish_mqtt()`, plus exception handling in `publish_pvoutput()` and `with open(...)` in `save_json()`.
- [x] Passed `inverter.copy()` snapshots to all publisher threads, joined publisher threads on exit/`--one-shot`, and added `SIGINT`/`SIGTERM` signal handlers for clean shutdown (`mqtt_client.disconnect()`, `mqtt_client.loop_stop()`, `client.close()`).

### Phase 4 — Ubuntu 24.04 / Python 3.12 `requirements.txt` & Tests (Goal 1)
- Lazy-import optional backends (`influxdb`, `prometheus_client`, `requests`/`urllib3`, `paho.mqtt.client`).
- Update `requirements.txt` to modern major-bounded dependencies for your active stack (`paho-mqtt>=2.0,<3`, `pycryptodomex>=3.20`, `requests>=2.31`, `pymodbus>=3.7,<4`), with unused/legacy backends (`prometheus-client`, `influxdb`, `SungrowModbusWebClient`) commented out or documented in `requirements-optional.txt` so `pip install -r requirements.txt` stays clean on Python 3.12.
- Update `Dockerfile` to Python 3.12.
- Add lightweight unit tests (mocked Sungrow AES TCP server/socket + mocked MQTT/PVOutput/JSON outputs) verifying:
  - Sungrow AES key exchange, encryption/decryption, and FC03/FC04 framing.
  - `modbus-sungrow-sg5kd.py` register decoding (including `_overflow`/`_indicator` and `_0.01`/`_10`/`_100`/`_1000` scaling) produces identical JSON output to `solariot-dave`.
  - `pymodbus` 3.x SMA/standard client compatibility and socket close-on-error handling.
  - `paho-mqtt` 2.x publish path, PVOutput request payload, and `save_json` (with and without `append_log`).
  - `--sungrow-reset` failure/reset thresholds and reset of `cumulativefailcount` on success.

### Phase 5 — Deployment to Ubuntu 24.04 LXC
- Push rewritten `solariot-dave` `master` with `git push --force-with-lease` (you'll handle remote pushes if/when ready) and update the LXC checkout with `git fetch && git reset --hard origin/master`.
- Create a fresh `venv` (`python3 -m venv .venv`), run `pip install -r requirements.txt`, verify with `--one-shot -v` against the live SG5K-D and MQTT broker, and diff the MQTT JSON payload and `telemetry.json` output against current production before cutting over (and optionally switching to a `systemd` service).

---

## 8. Recorded Decisions & Caveats

| # | Decision | Status / Notes & Caveats |
|---|---|---|
| 1 | **Rebase/reset `solariot-dave` `master` onto upstream `solariot` (`beb63a7`) and re-apply custom features directly on `master`** | **Good — 1 git caveat:** Because `solariot-dave` tracks `origin` (`github.com/davehng/solariot.git`), resetting `master` rewrites history vs `origin/master`. We will create a local backup tag (`backup-2021-master`) first; when pushing to GitHub later you'll need `git push --force-with-lease`, and on your LXC you'll sync once via `git fetch && git reset --hard origin/master` (normal `git pull` works again afterward). |
| 2 | **Option B1 — self-contained Sungrow AES-128-ECB Modbus TCP client in-repo (`socket` + `pycryptodomex`), plus `pymodbus` 3.x for standard/SMA** | **Good — 1 prerequisite:** Cleanest long-term choice; immune to future `pymodbus` internal rewrites, and `pycryptodomex` ships Python 3.12 wheels. Before coding it, please share `/home/dhng/.local/lib/python3.10/site-packages/SungrowModbusTcpClient/SungrowModbusTcpClient.py` so we match the exact framing/padding your SG5K-D accepts. |
| 3 | **Active outputs: MQTT, PVOutput, JSON file (not using Prometheus, InfluxDB, or dweepy)** | **Good — 2 catches covered:** (a) Upstream `solariot.py:700` has a bug passing `config.append_log` without a default, which would break your existing `json_file` config—we will fix it with `getattr(config, "append_log", False)`. (b) `requests` (used by PVOutput) was previously only installed transitively via `influxdb`/`dweepy`—we will declare it explicitly in `requirements.txt`. |
| 4 | **Reset `cumulativefailcount` to `0` after any successful scrape** | **Good — no issues:** `cumulativefailcount` becomes consecutive failures across reset cycles (e.g., resets at 10, 20, 30, 40, 50, then exits cleanly via `sys.exit(1)` if 5 consecutive reset attempts fail), instead of exiting after 50 scattered blips over weeks/months. |
| 5 | **Keep `requirements.txt` (matching upstream convention)** | **Good — 1 nuance:** Keep `requirements.txt` focused on the core Python 3.12–clean stack (`paho-mqtt>=2.0,<3`, `pycryptodomex>=3.20`, `requests>=2.31`, `pymodbus>=3.7,<4`) and comment out optional/legacy packages (`influxdb`, `prometheus-client`, `SungrowModbusWebClient`) so `pip install -r requirements.txt` doesn't pull EOL `influxdb` v1 or unused packages on Ubuntu 24.04. |
