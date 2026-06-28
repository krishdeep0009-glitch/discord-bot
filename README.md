[README.md](https://github.com/user-attachments/files/29435003/README.md)
# 🌩️ Blined Cloud VPS Manager

A professional Discord bot for managing Docker-based VPS instances via slash commands — with **SSH key authentication** (no passwords).

> 📦 **Repository:** [github.com/krishdeep0009-glitch/discord-bot](https://github.com/krishdeep0009-glitch/discord-bot)

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)
![discord.py](https://img.shields.io/badge/discord.py-2.x-5865F2?logo=discord)
![Docker](https://img.shields.io/badge/Docker-SDK-2496ED?logo=docker)
![SQLite](https://img.shields.io/badge/Database-SQLite-003B57?logo=sqlite)
![License](https://img.shields.io/badge/License-MIT-green)

---

## ✨ Features

- 🐳 Docker-backed VPS instances with memory & CPU limits
- 🔑 **SSH key authentication** — 4096-bit RSA keypair generated per VPS (no passwords)
- 📬 Private key delivered as a file attachment via Discord DM
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

> If prompted for a password, use a **Personal Access Token** from [github.com/settings/tokens](https://github.com/settings/tokens) — never your real password.

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
SSH_PORT_START=2200
```

### 5 — Open the SSH port range on your firewall

Each VPS gets a unique host port starting from `SSH_PORT_START` (default 2200).

```bash
# UFW example — allow ports 2200–2300 for SSH
sudo ufw allow 2200:2300/tcp
sudo ufw reload
```

### 6 — Run the bot

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
2. Click **New Application** → give it a name (`Blined Cloud`)
3. Go to **Bot** tab → **Add Bot**
4. Copy the **Token** → paste into `.env` as `DISCORD_TOKEN`
5. Under **Privileged Gateway Intents**, enable:
   - ✅ Server Members Intent
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Send Messages`, `Embed Links`, `Read Message History`, `Attach Files`
7. Open the generated URL → invite the bot to your server
8. Right-click your **Admin role** in Discord → **Copy ID** → paste as `ADMIN_ROLE_ID`

> Enable **Developer Mode**: Discord Settings → Advanced → Developer Mode ✅

---

## 📖 Command Reference

### 👤 User Commands

| Command | Description |
|---|---|
| `/start <vps_id>` | Start your VPS |
| `/stop <vps_id>` | Stop your VPS |
| `/restart <vps_id>` | Restart your VPS |
| `/reinstall <vps_id>` | Wipe & recreate VPS — new SSH key sent via DM |
| `/regen-ssh <vps_id>` | Generate a new SSH keypair — private key sent via DM |
| `/vps-performance <vps_id>` | Live CPU, RAM, disk, uptime, network stats |
| `/my-vps` | List all your VPS instances |

### 🛡️ Admin Commands

| Command | Description |
|---|---|
| `/admin-add-user <user>` | Grant a user hosting access |
| `/admin-remove-user <user>` | Revoke a user's hosting access |
| `/create <user> <memory> <cpu> <disk> <os>` | Provision a new VPS (SSH key sent to user via DM) |
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

## 🔑 How SSH Access Works

When a VPS is created, the bot automatically:

1. Generates a **4096-bit RSA keypair**
2. Injects the **public key** into the container's `/root/.ssh/authorized_keys`
3. Sends the **private key** as a `.pem` file attachment via Discord DM

The user connects like this:

```bash
# Set correct permissions on the key file
chmod 400 BC-0001_id_rsa.pem

# Connect to your VPS
ssh -i BC-0001_id_rsa.pem -p 2200 root@YOUR_VPS_IP
```

Password authentication is **disabled** inside the container — SSH key only.

---

## 🗂️ Project Structure

```
discord-bot/
├── blinedcloud_bot.py   # Main bot — commands, DB, Docker, SSH key logic
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
├── .env                 # Your secrets (git-ignored)
├── .gitignore           # Keeps secrets & venv out of git
├── ssh_keys/            # Generated private keys per VPS (git-ignored)
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
- SSH keys are 4096-bit RSA — cryptographically strong
- Password authentication is **disabled** in all containers
- `.env`, `.db`, `.log`, and `ssh_keys/` are all git-ignored

---

## 🛠️ Production Tips

- Point users to your VPS's public IP for the SSH connection command
- Open the SSH port range in your firewall before creating VPS instances
- For real disk enforcement use Docker volumes with overlay2 quotas
- The bot itself needs no inbound ports — safe behind any firewall

---

## 📄 License

MIT — free to use, modify, and distribute.

---

<div align="center">
  Made with ❤️ by <a href="https://github.com/krishdeep0009-glitch">krishdeep0009-glitch</a>
</div>
