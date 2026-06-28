# 🌩️ Blined Cloud VPS Manager

A professional Discord bot for managing Docker-based VPS instances via slash commands.

> **Repository:** [github.com/krishdeep0009-glitch/discord-bot](https://github.com/krishdeep0009-glitch/discord-bot)

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)
![discord.py](https://img.shields.io/badge/discord.py-2.x-5865F2?logo=discord)
![Docker](https://img.shields.io/badge/Docker-SDK-2496ED?logo=docker)
![SQLite](https://img.shields.io/badge/Database-SQLite-003B57?logo=sqlite)
![License](https://img.shields.io/badge/License-MIT-green)

---

## ✨ Features

- 🐳 Docker-backed VPS instances with memory & CPU limits
- 🔐 Per-user access control + admin role enforcement
- 📊 Live performance stats (CPU, RAM, disk, uptime, network)
- 🔑 Auto-generated root passwords, regeneratable on demand
- 📬 Credentials delivered via Discord DM
- 🗄️ SQLite database — zero external dependencies
- 🎨 Professional branded embeds on every response

---

## 📋 Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11+ |
| Docker Engine | 24+ (running on the host) |
| Git | Any recent version |

> The user running the bot must be in the `docker` group:
> ```bash
> sudo usermod -aG docker $USER && newgrp docker
> ```

---

## 🚀 Quick Start (VPS / Linux)

### 1 — Clone the repository

```bash
git clone https://github.com/krishdeep0009-glitch/discord-bot.git
cd discord-bot
```

### 2 — Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3 — Install dependencies

```bash
pip install -r requirements.txt
```

### 4 — Configure environment variables

```bash
cp .env.example .env
nano .env          # or: vim .env
```

Fill in the two values:

```env
DISCORD_TOKEN=your_discord_bot_token_here
ADMIN_ROLE_ID=123456789012345678
```

### 5 — Run the bot

```bash
python blinedcloud_bot.py
```

---

## 🔧 Running as a systemd Service (recommended for VPS)

Keep the bot alive across reboots:

```bash
sudo nano /etc/systemd/system/blinedcloud.service
```

Paste the following (adjust paths):

```ini
[Unit]
Description=Blined Cloud VPS Manager Bot
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=YOUR_LINUX_USER
WorkingDirectory=/home/YOUR_LINUX_USER/discord-bot
ExecStart=/home/YOUR_LINUX_USER/discord-bot/venv/bin/python blinedcloud_bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable blinedcloud
sudo systemctl start blinedcloud

# Check logs
sudo journalctl -u blinedcloud -f
```

---

## 🤖 Discord Bot Setup

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. **New Application** → give it a name
3. Go to **Bot** tab → **Add Bot**
4. Copy the **Token** → paste into `.env` as `DISCORD_TOKEN`
5. Under **Privileged Gateway Intents**, enable:
   - ✅ Server Members Intent
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`
7. Open the generated URL and invite the bot to your server
8. Right-click your **Admin role** in Discord → **Copy ID** (requires Developer Mode) → paste as `ADMIN_ROLE_ID`

---

## 📖 Command Reference

### 👤 User Commands

| Command | Description |
|---|---|
| `/start <vps_id>` | Start your VPS |
| `/stop <vps_id>` | Stop your VPS |
| `/restart <vps_id>` | Restart your VPS |
| `/reinstall <vps_id>` | Wipe and recreate VPS with same specs |
| `/regen-ssh <vps_id>` | Generate a new root password |
| `/vps-performance <vps_id>` | Live CPU, RAM, disk, uptime, network stats |
| `/my-vps` | List all VPS instances you own |

### 🛡️ Admin Commands

| Command | Description |
|---|---|
| `/admin-add-user <user>` | Grant hosting access to a user |
| `/admin-remove-user <user>` | Revoke hosting access |
| `/create <user> <memory> <cpu> <disk> <os>` | Provision a new VPS |
| `/suspend-vps <vps_id>` | Stop VPS and lock it |
| `/unsuspend-vps <vps_id>` | Reactivate a suspended VPS |
| `/remove-vps <vps_id>` | Permanently delete a VPS |
| `/list-vps` | List every VPS on the node |
| `/node-stats` | Host CPU, RAM, disk, container counts |

### Supported OS Templates

| Template | Docker Image |
|---|---|
| `ubuntu22` | `ubuntu:22.04` |
| `debian11` | `debian:11` |

---

## 🗂️ Project Structure

```
discord-bot/
├── blinedcloud_bot.py   # Main bot — all commands, DB logic, Docker integration
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
├── .env                 # Your secrets (git-ignored)
├── .gitignore           # Keeps secrets and venv out of git
├── blinedcloud.db       # SQLite DB (auto-created on first run, git-ignored)
├── blinedcloud.log      # Log file (auto-created, git-ignored)
└── README.md            # This file
```

---

## 🔒 Security Notes

- Users can **only** manage VPS instances they own
- Admin commands require the configured Discord role
- Passwords are generated with `random.SystemRandom()` (cryptographically secure)
- `.env`, `.db`, and `.log` files are git-ignored by default

---

## 🛠️ Production Recommendations

- Replace the `tail -f /dev/null` container command with a real SSH image (e.g. `linuxserver/openssh-server`) and expose a dynamic host port per container
- Use Docker volumes with overlay2 quota support for actual disk enforcement
- Put the bot behind a reverse proxy or firewall — the bot process itself needs no inbound ports

---

## 📄 License

MIT — free to use, modify, and distribute.
