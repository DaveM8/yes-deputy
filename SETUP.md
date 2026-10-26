# Server Setup Guide

## Steps 1–3 — nginx, HTTPS (already done)

Steps 1–3 (nginx install, nginx config, certbot certificate) are complete.
The site is live at https://yesdeputy.com.

---

## Step 4 — systemd service (start on boot, survive terminal close)

This replaces running uvicorn manually in a terminal.

### 4a — Stop the manually running server first

```bash
# If started with nohup:
cat /data/members_questions/uvicorn.pid | xargs kill 2>/dev/null

# If still running in a terminal, find and kill it:
pkill -f "uvicorn app.main:app"
```

### 4b — Create the service file

```bash
sudo nano /etc/systemd/system/yesdeputy.service
```

Paste this exactly:

```ini
[Unit]
Description=Yes Deputy — Oireachtas Q&A
After=network.target

[Service]
User=dave
WorkingDirectory=/data/members_questions
EnvironmentFile=/data/members_questions/.env
ExecStart=/data/members_questions/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> `EnvironmentFile` loads credentials from `.env` so the app can start without a terminal session.
> `--host 127.0.0.1` means uvicorn only accepts connections from nginx (not directly from the internet).
> `--reload` is removed — that is for development only.

### 4c — Enable and start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable yesdeputy
sudo systemctl start yesdeputy
```

### 4d — Check it is running

```bash
sudo systemctl status yesdeputy
```

You should see `Active: active (running)`. Then open https://yesdeputy.com to confirm.

### Useful commands

```bash
# View live logs
sudo journalctl -u yesdeputy -f

# Restart after code changes
sudo systemctl restart yesdeputy

# Stop the service
sudo systemctl stop yesdeputy
```

---

## Step 5 — Verify auto-restart

Test that the service recovers automatically if the process dies:

```bash
# Get the process ID
sudo systemctl status yesdeputy | grep "Main PID"

# Kill it — systemd should restart it within 5 seconds
sudo kill <PID>

# Confirm it came back
sudo systemctl status yesdeputy
```
