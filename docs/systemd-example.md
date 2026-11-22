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

## 2. Create the systemd Unit File

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

> **Notes on CLI flags:**
> - `-v` enables `INFO` logging (captured by `journald`); use `-vv` temporarily for `DEBUG` wire-level logs, or omit it for `WARNING` and above.
> - `--sungrow-reset` is intentionally omitted by default: now that `sungrow_tcp_client.py` avoids double-connecting on cached-key polls and `solariot.py` always closes sockets on error, most setups should not need it. If your inverter still occasionally stops responding, you can append `--sungrow-reset` (or `--exit-on-error` to let `systemd` restart the service immediately on error) to `ExecStart`.

## 3. Enable and Start the Service

Reload `systemd`, enable `solariot` to start automatically on boot, and start it now:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now solariot
```

## 4. Check Status and Logs

```bash
# Check service status
sudo systemctl status solariot

# Follow live logs
journalctl -u solariot -f

# View recent logs from the current boot
journalctl -u solariot -b -n 100
```

Common management commands:

```bash
sudo systemctl restart solariot
sudo systemctl stop solariot
sudo systemctl disable solariot
```

## 5. Recreating `.venv` After an Ubuntu LTS Upgrade

When upgrading Ubuntu (e.g., 22.04 LTS → 24.04 LTS), the system Python minor version changes (3.10 → 3.12), so the virtual environment should be recreated:

```bash
sudo systemctl stop solariot
cd /path/to/solariot
rm -rf .venv
sudo apt install -y python3-venv
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
sudo systemctl start solariot
```
