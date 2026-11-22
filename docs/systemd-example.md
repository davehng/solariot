# Running SolarIoT as a systemd Service

This guide shows how to run `solariot.py` inside a Python virtual environment (`.venv`) as a managed `systemd` service on Ubuntu (22.04 LTS / 24.04 LTS).

## 1. Set Up the Virtual Environment

In your SolarIoT directory (referred to below as `/path/to/solariot`):

```bash
cd /path/to/solariot
sudo apt update && sudo apt install -y python3-venv
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Verify your `config.py` works with a quick one-shot test before enabling the service:

```bash
.venv/bin/python solariot.py --one-shot -v
```

## 2. Option A: System-Wide Service (`/etc/systemd/system/`)

Create `/etc/systemd/system/solariot.service` (replace `<user>` and `/path/to/solariot` with your local account and checkout path):

```bash
sudo nano /etc/systemd/system/solariot.service
```

```ini
[Unit]
Description=SolarIoT Inverter Collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<user>
WorkingDirectory=/path/to/solariot
ExecStart=/path/to/solariot/.venv/bin/python /path/to/solariot/solariot.py -v
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
```

Enable, start, and monitor:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now solariot

# Status and live logs
sudo systemctl status solariot
journalctl -u solariot -f
journalctl -u solariot -b -n 100

# Common management commands
sudo systemctl restart solariot
sudo systemctl stop solariot
sudo systemctl disable solariot
```

## 3. Option B: User Mode (`systemctl --user`)

User mode lets you manage the service without `sudo` once linger is enabled.

### Create the user unit file

```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/solariot.service
```

```ini
[Unit]
Description=SolarIoT Inverter Collector

[Service]
Type=simple
WorkingDirectory=%h/path/to/solariot
ExecStart=%h/path/to/solariot/.venv/bin/python %h/path/to/solariot/solariot.py -v
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
```

*(In user units, omit `User=`, use `WantedBy=default.target`, and optionally use `%h` as shorthand for `$HOME`.)*

### Enable linger so the service starts at boot without an SSH login

By default, user services only run while logged in and stop on logout. Enable linger once:

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger  # expect Linger=yes
```

### Enable, start, and monitor

```bash
systemctl --user daemon-reload
systemctl --user enable --now solariot

# Status and live logs (no sudo required)
systemctl --user status solariot
journalctl --user -u solariot -f
journalctl --user -u solariot -b -n 100

# Common management commands
systemctl --user restart solariot
systemctl --user stop solariot
systemctl --user disable solariot
```

> **LXC tip:** Run `systemctl --user` from a direct SSH session as your user (not via `su -` from `root`). If you ever see `Failed to connect to bus`, run `export XDG_RUNTIME_DIR=/run/user/$(id -u)` or log in directly via SSH.

## 4. Notes on CLI Flags

- `-v` enables `INFO` logging (captured by `journald`); use `-vv` temporarily for `DEBUG` wire-level logs, or omit it for `WARNING` and above.
- `--sungrow-reset` is intentionally omitted by default: now that `sungrow_tcp_client.py` avoids double-connecting on cached-key polls and `solariot.py` always closes sockets on error, most setups should not need it. If your inverter still occasionally stops responding, you can append `--sungrow-reset` (or `--exit-on-error` to let `systemd` restart the service immediately on error) to `ExecStart`.

## 5. Recreating `.venv` After an Ubuntu LTS Upgrade

When upgrading Ubuntu (e.g., 22.04 LTS → 24.04 LTS), the system Python minor version changes (3.10 → 3.12), so the virtual environment should be recreated (use `systemctl --user stop/start` if running in user mode):

```bash
sudo systemctl stop solariot   # or: systemctl --user stop solariot
cd /path/to/solariot
rm -rf .venv
sudo apt install -y python3-venv
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
sudo systemctl start solariot  # or: systemctl --user start solariot
```
