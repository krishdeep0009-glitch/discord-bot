# 🌩️ Blined Cloud VPS Manager

A professional Discord bot for managing Docker-based VPS instances via slash commands — powered by **tmate** for instant terminal access.

> 📦 **Repository:** [github.com/krishdeep0009-glitch/discord-bot](https://github.com/krishdeep0009-glitch/discord-bot)

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)
![discord.py](https://img.shields.io/badge/discord.py-2.x-5865F2?logo=discord)
![Docker](https://img.shields.io/badge/Docker-SDK-2496ED?logo=docker)
![tmate](https://img.shields.io/badge/Terminal-tmate-orange)
![SQLite](https://img.shields.io/badge/Database-SQLite-003B57?logo=sqlite)
![License](https://img.shields.io/badge/License-MIT-green)

---

## ✨ Features

- 🐳 Docker-backed VPS instances with memory & CPU limits
- 🖥️ **tmate terminal access** — SSH command + web browser terminal per VPS
- 📬 Session links delivered privately via Discord DM
- 🔐 Per-user access control + admin role enforcement
- 📊 Live performance stats (CPU, RAM, disk, uptime, network)
- 🗄️ SQLite database — zero external dependencies
- 🎨 Professional branded embeds on every response

---

## 📋 Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11+ |
| Docker Engine | 24+ (running on host) |
| Git | Any recent version |

> Add your Linux user to the Docker group (one-time):
> ```bash
> sudo usermod -aG docker $USER && newgrp docker
> ```

---

## 🚀 Deploy on your VPS

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
nano .env
```

Fill in your values:

```env
DISCORD_TOKEN=your_discord_bot_token_here
ADMIN_ROLE_ID=123456789012345678
```

### 5 — Run the bot

```bash
python blinedcloud_bot.py
```

---

## 🔧 Run as a systemd Service (keep alive on VPS)

```bash
sudo nano /etc/systemd/system/blinedcloud.service
```

Paste this (replace `YOUR_LINUX_USER`):

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

# View live logs
sudo journalctl -u blinedcloud -f
```

---

## 🤖 Discord Bot Setup

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → name it `Blined Cloud`
3. Go to **Bot** tab → **Add Bot**
4. Copy the **Token** → paste into `.env` as `DISCORD_TOKEN`
5. Under **Privileged Gateway Intents**, enable:
   - ✅ Server Members Intent
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Send Messages`, `Embed Links`, `Read Message History`
7. Open the generated URL → invite the bot to your server
8. Right-click your **Admin role** → **Copy ID** → paste as `ADMIN_ROLE_ID`

> Enable **Developer Mode**: Discord Settings → Advanced → Developer Mode ✅

---

## 📖 Command Reference

### 👤 User Commands

| Command | Description |
|---|---|
| `/start <vps_id>` | Start your VPS |
| `/stop <vps_id>` | Stop your VPS |
| `/restart <vps_id>` | Restart your VPS |
| `/reinstall <vps_id>` | Wipe & recreate VPS — new tmate session sent via DM |
| `/regen-ssh <vps_id>` | Get a fresh tmate session link (sent via DM) |
| `/vps-performance <vps_id>` | Live CPU, RAM, disk, uptime, network stats |
| `/my-vps` | List all your VPS instances |

### 🛡️ Admin Commands

| Command | Description |
|---|---|
| `/admin-add-user <user>` | Grant a user hosting access |
| `/admin-remove-user <user>` | Revoke a user's hosting access |
| `/create <user> <memory> <cpu> <disk> <os>` | Provision a new VPS |
| `/suspend-vps <vps_id>` | Stop & lock a VPS |
| `/unsuspend-vps <vps_id>` | Reactivate a suspended VPS |
| `/remove-vps <vps_id>` | Permanently delete a VPS |
| `/list-vps` | List all VPS instances on the node |
| `/node-stats` | Show host CPU, RAM, disk & container counts |

### 🖥️ Supported OS Templates

| Template | Docker Image |
|---|---|
| `ubuntu22` | `ubuntu:22.04` |
| `debian11` | `debian:11` |

---

## 🖥️ How tmate Access Works

When a VPS is created, the bot automatically:

1. Spins up a Docker container with the chosen OS
2. Installs **tmate** inside the container
3. Starts a tmate session and captures the SSH + web links
4. Sends both links privately via **Discord DM**

The user gets two ways to connect:

```bash
# SSH terminal (paste the command from your DM)
ssh abc123@sgp1.tmate.io

# Or open the web terminal link in any browser
https://tmate.io/t/abc123
```

> ⚠️ tmate links give **full root access** — keep them private.
> Use `/regen-ssh` anytime to invalidate the old session and get a new one.

---

## 🗂️ Project Structure

```
discord-bot/
├── blinedcloud_bot.py   # Main bot — all commands, DB, Docker, tmate logic
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
├── .env                 # Your secrets (git-ignored)
├── .gitignore           # Keeps secrets & venv out of git
├── blinedcloud.db       # SQLite DB (auto-created, git-ignored)
├── blinedcloud.log      # Log file (auto-created, git-ignored)
└── README.md            # This file
```

---

## 🔄 Update the Bot

```bash
cd discord-bot
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart blinedcloud
```

---

## 🔒 Security Notes

- Users can **only** manage VPS instances they own
- Admin commands require the configured Discord role
- tmate session links are sent via **private DM only**
- Use `/regen-ssh` to rotate a session if it's been compromised
- `.env`, `.db`, and `.log` are all git-ignored

---

## 📄 License

MIT — free to use, modify, and distribute.

---

<div align="center">
  Made with ❤️ by <a href="https://github.com/krishdeep0009-glitch">krishdeep0009-glitch</a>
</div>
