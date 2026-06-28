"""
Blined Cloud VPS Manager - Discord Bot
Manages Docker-based VPS instances with SSH key authentication.
"""

import os
import io
import time
import string
import random
import asyncio
import logging
import sqlite3
import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
import docker
import psutil
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

# ─────────────────────────────────────────────
# Environment & Logging
# ─────────────────────────────────────────────
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))
SSH_PORT_START = int(os.getenv("SSH_PORT_START", "2200"))  # host port base for SSH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("blinedcloud.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("BlinedCloud")

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
BRAND_COLOR   = 0x5865F2
SUCCESS_COLOR = 0x57F287
ERROR_COLOR   = 0xED4245
WARN_COLOR    = 0xFEE75C
INFO_COLOR    = 0x5865F2

FOOTER_TEXT = "Powered by Blined Cloud"
FOOTER_ICON = None

# OS → Docker image map
OS_MAP = {
    "ubuntu22": "ubuntu:22.04",
    "debian11": "debian:11",
}

DB_PATH = "blinedcloud.db"
KEYS_DIR = "ssh_keys"          # folder to store generated key pairs
os.makedirs(KEYS_DIR, exist_ok=True)


# ─────────────────────────────────────────────
# SSH Key Generation
# ─────────────────────────────────────────────

def generate_ssh_keypair(vps_id: str) -> tuple[str, str]:
    """
    Generate a fresh 4096-bit RSA keypair.
    Returns (private_key_pem, public_key_openssh) as strings.
    Private key is saved to disk for reference; user receives it via DM.
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=4096,
        backend=default_backend(),
    )

    # PEM private key (no passphrase — user saves this as id_rsa)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    # OpenSSH public key (goes into authorized_keys inside container)
    public_openssh = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()

    # Save private key to disk (chmod 600)
    key_path = os.path.join(KEYS_DIR, f"{vps_id}.pem")
    with open(key_path, "w") as f:
        f.write(private_pem)
    os.chmod(key_path, 0o600)

    return private_pem, public_openssh


def load_private_key(vps_id: str) -> str | None:
    """Load the stored private key for a VPS, or None if missing."""
    key_path = os.path.join(KEYS_DIR, f"{vps_id}.pem")
    if os.path.exists(key_path):
        with open(key_path) as f:
            return f.read()
    return None


def delete_ssh_key(vps_id: str):
    """Remove stored private key when VPS is deleted."""
    key_path = os.path.join(KEYS_DIR, f"{vps_id}.pem")
    if os.path.exists(key_path):
        os.remove(key_path)


# ─────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with db_connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id   INTEGER PRIMARY KEY,
                added_by  INTEGER,
                added_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS vps_instances (
                vps_id        TEXT PRIMARY KEY,
                user_id       INTEGER NOT NULL,
                container_id  TEXT,
                os_image      TEXT,
                memory_mb     INTEGER,
                cpu_period    INTEGER DEFAULT 100000,
                cpu_quota     INTEGER,
                disk_gb       INTEGER,
                ssh_port      INTEGER,
                ssh_pubkey    TEXT,
                status        TEXT DEFAULT 'running',
                created_at    TEXT DEFAULT (datetime('now'))
            );
        """)
    log.info("Database initialised.")


# ─────────────────────────────────────────────
# Embed Factory
# ─────────────────────────────────────────────

def make_embed(
    title: str,
    description: str = "",
    color: int = BRAND_COLOR,
    fields: list[tuple[str, str, bool]] | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.datetime.utcnow(),
    )
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    for name, value, inline in (fields or []):
        embed.add_field(name=name, value=value, inline=inline)
    return embed


# ─────────────────────────────────────────────
# VPS ID & Port Helpers
# ─────────────────────────────────────────────

def next_vps_id() -> str:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT vps_id FROM vps_instances ORDER BY vps_id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return "BC-0001"
    last_num = int(row["vps_id"].split("-")[1])
    return f"BC-{last_num + 1:04d}"


def next_ssh_port() -> int:
    """Assign the next available host SSH port (SSH_PORT_START + offset)."""
    with db_connect() as conn:
        row = conn.execute(
            "SELECT MAX(ssh_port) as mp FROM vps_instances"
        ).fetchone()
    if row["mp"] is None:
        return SSH_PORT_START
    return row["mp"] + 1


def bytes_to_gb(b: int) -> float:
    return round(b / 1024 ** 3, 2)


# ─────────────────────────────────────────────
# Docker Helpers
# ─────────────────────────────────────────────

def get_docker() -> docker.DockerClient:
    return docker.from_env()


def create_container(
    vps_id: str,
    image: str,
    memory_mb: int,
    cpu_cores: float,
    ssh_pubkey: str,
    host_port: int,
) -> docker.models.containers.Container:
    """
    Create a Docker container with:
    - SSH server installed and running on port 22 (mapped to host_port)
    - Public key injected into root's authorized_keys
    - Memory & CPU limits applied
    """
    client = get_docker()

    cpu_period = 100_000
    cpu_quota  = int(cpu_period * cpu_cores)

    # Startup script: install SSH, inject pubkey, start sshd
    startup = (
        "apt-get update -qq && "
        "apt-get install -y -qq openssh-server > /dev/null 2>&1 && "
        "mkdir -p /root/.ssh && "
        "chmod 700 /root/.ssh && "
        f"echo '{ssh_pubkey}' > /root/.ssh/authorized_keys && "
        "chmod 600 /root/.ssh/authorized_keys && "
        "sed -i 's/#PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config && "
        "sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config && "
        "sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config && "
        "service ssh start && "
        "tail -f /dev/null"
    )

    container = client.containers.run(
        image,
        name=vps_id,
        detach=True,
        mem_limit=f"{memory_mb}m",
        cpu_period=cpu_period,
        cpu_quota=cpu_quota,
        ports={"22/tcp": host_port},          # map container :22 → host :host_port
        environment={"VPS_ID": vps_id},
        command=f"bash -c \"{startup}\"",
        labels={"managed-by": "blined-cloud", "vps-id": vps_id},
    )
    return container


def container_stats(container) -> dict:
    raw = container.stats(stream=False)

    cpu_delta = (
        raw["cpu_stats"]["cpu_usage"]["total_usage"]
        - raw["precpu_stats"]["cpu_usage"]["total_usage"]
    )
    sys_delta = raw["cpu_stats"]["system_cpu_usage"] - raw["precpu_stats"]["system_cpu_usage"]
    num_cpus  = raw["cpu_stats"].get(
        "online_cpus",
        len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [1])),
    )
    cpu_pct = (cpu_delta / sys_delta) * num_cpus * 100.0 if sys_delta else 0.0

    mem_usage = raw["memory_stats"].get("usage", 0)
    mem_limit = raw["memory_stats"].get("limit", 1)
    mem_pct   = (mem_usage / mem_limit) * 100.0

    net_rx = net_tx = 0
    for iface in raw.get("networks", {}).values():
        net_rx += iface.get("rx_bytes", 0)
        net_tx += iface.get("tx_bytes", 0)

    started_at = container.attrs["State"].get("StartedAt", "")
    uptime_str = "N/A"
    if started_at and started_at != "0001-01-01T00:00:00Z":
        try:
            start = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            delta = datetime.datetime.now(datetime.timezone.utc) - start
            h, rem = divmod(int(delta.total_seconds()), 3600)
            m, s   = divmod(rem, 60)
            uptime_str = f"{h}h {m}m {s}s"
        except Exception:
            pass

    return {
        "cpu_pct":     round(cpu_pct, 2),
        "mem_usage_mb": round(mem_usage / 1024 / 1024, 1),
        "mem_limit_mb": round(mem_limit / 1024 / 1024, 1),
        "mem_pct":     round(mem_pct, 2),
        "net_rx_mb":   round(net_rx / 1024 / 1024, 2),
        "net_tx_mb":   round(net_tx / 1024 / 1024, 2),
        "uptime":      uptime_str,
    }


# ─────────────────────────────────────────────
# Permission Helpers
# ─────────────────────────────────────────────

def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    return any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles)


def owns_vps(user_id: int, vps_id: str) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM vps_instances WHERE vps_id=? AND user_id=?",
            (vps_id, user_id),
        ).fetchone()
    return row is not None


# ─────────────────────────────────────────────
# Bot
# ─────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True


class BlinedCloudBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        log.info("Slash commands synced.")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Powered by Blined Cloud",
            )
        )


bot = BlinedCloudBot()


# ══════════════════════════════════════════════
# USER COMMANDS
# ══════════════════════════════════════════════

@bot.tree.command(name="start", description="Start your VPS instance.")
@app_commands.describe(vps_id="Your VPS ID (e.g. BC-0001)")
async def cmd_start(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=make_embed(
            "❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
    if row["status"] == "suspended":
        return await interaction.followup.send(embed=make_embed(
            "⛔ Suspended", "This VPS is suspended. Contact an admin.", WARN_COLOR))
    try:
        client = get_docker()
        client.containers.get(row["container_id"]).start()
        with db_connect() as conn:
            conn.execute("UPDATE vps_instances SET status='running' WHERE vps_id=?", (vps_id,))
        await interaction.followup.send(embed=make_embed(
            "✅ VPS Started", f"**{vps_id}** is now running.", SUCCESS_COLOR))
    except Exception as e:
        log.error(f"start: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="stop", description="Stop your VPS instance.")
@app_commands.describe(vps_id="Your VPS ID (e.g. BC-0001)")
async def cmd_stop(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=make_embed(
            "❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))
    try:
        with db_connect() as conn:
            row = conn.execute("SELECT container_id FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
        get_docker().containers.get(row["container_id"]).stop()
        with db_connect() as conn:
            conn.execute("UPDATE vps_instances SET status='stopped' WHERE vps_id=?", (vps_id,))
        await interaction.followup.send(embed=make_embed(
            "🛑 VPS Stopped", f"**{vps_id}** has been stopped.", WARN_COLOR))
    except Exception as e:
        log.error(f"stop: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="restart", description="Restart your VPS instance.")
@app_commands.describe(vps_id="Your VPS ID (e.g. BC-0001)")
async def cmd_restart(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=make_embed(
            "❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))
    try:
        with db_connect() as conn:
            row = conn.execute("SELECT container_id, status FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
        if row["status"] == "suspended":
            return await interaction.followup.send(embed=make_embed(
                "⛔ Suspended", "This VPS is suspended. Contact an admin.", WARN_COLOR))
        get_docker().containers.get(row["container_id"]).restart()
        with db_connect() as conn:
            conn.execute("UPDATE vps_instances SET status='running' WHERE vps_id=?", (vps_id,))
        await interaction.followup.send(embed=make_embed(
            "🔄 VPS Restarted", f"**{vps_id}** has been restarted.", SUCCESS_COLOR))
    except Exception as e:
        log.error(f"restart: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="reinstall", description="Reinstall your VPS (same specs, new SSH key, data wiped).")
@app_commands.describe(vps_id="Your VPS ID (e.g. BC-0001)")
async def cmd_reinstall(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=make_embed(
            "❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))
    try:
        with db_connect() as conn:
            row = conn.execute("SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
        # Remove old container
        try:
            get_docker().containers.get(row["container_id"]).remove(force=True)
        except Exception:
            pass

        # Generate a fresh SSH keypair
        private_key, public_key = generate_ssh_keypair(vps_id)
        cpu_cores = row["cpu_quota"] / row["cpu_period"]

        new_container = create_container(
            vps_id, row["os_image"], row["memory_mb"],
            cpu_cores, public_key, row["ssh_port"],
        )
        with db_connect() as conn:
            conn.execute(
                "UPDATE vps_instances SET container_id=?, ssh_pubkey=?, status='running' WHERE vps_id=?",
                (new_container.id, public_key, vps_id),
            )

        # Send new private key via DM as a file
        key_file = discord.File(
            io.BytesIO(private_key.encode()),
            filename=f"{vps_id}_id_rsa.pem",
        )
        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                embed=make_embed(
                    "🔄 VPS Reinstalled",
                    f"**{vps_id}** has been reinstalled with a **new SSH key**.\n"
                    f"Save the attached private key and connect with:\n"
                    f"```ssh -i {vps_id}_id_rsa.pem -p {row['ssh_port']} root@YOUR_VPS_IP```",
                    SUCCESS_COLOR,
                    fields=[
                        ("VPS ID",    vps_id,               True),
                        ("OS",        row["os_image"],       True),
                        ("SSH Port",  str(row["ssh_port"]),  True),
                    ],
                ),
                file=key_file,
            )
        except discord.Forbidden:
            pass

        await interaction.followup.send(embed=make_embed(
            "✅ Reinstalled",
            f"**{vps_id}** reinstalled. New SSH key sent to your DMs.",
            SUCCESS_COLOR,
        ))
    except Exception as e:
        log.error(f"reinstall: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="regen-ssh", description="Generate a new SSH key for your VPS.")
@app_commands.describe(vps_id="Your VPS ID (e.g. BC-0001)")
async def cmd_regen_ssh(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=make_embed(
            "❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT container_id, ssh_port FROM vps_instances WHERE vps_id=?", (vps_id,)
            ).fetchone()

        # Generate new keypair
        private_key, public_key = generate_ssh_keypair(vps_id)

        # Inject new public key into the running container
        try:
            container = get_docker().containers.get(row["container_id"])
            container.exec_run(
                f"bash -c \"echo '{public_key}' > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys\""
            )
        except Exception as ex:
            log.warning(f"regen-ssh exec failed: {ex}")

        with db_connect() as conn:
            conn.execute("UPDATE vps_instances SET ssh_pubkey=? WHERE vps_id=?", (public_key, vps_id))

        # DM the new private key as a file
        key_file = discord.File(
            io.BytesIO(private_key.encode()),
            filename=f"{vps_id}_id_rsa.pem",
        )
        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                embed=make_embed(
                    "🔑 New SSH Key Generated",
                    f"Your new private key for **{vps_id}** is attached.\n"
                    f"Connect with:\n"
                    f"```ssh -i {vps_id}_id_rsa.pem -p {row['ssh_port']} root@YOUR_VPS_IP```",
                    INFO_COLOR,
                    fields=[("SSH Port", str(row["ssh_port"]), True)],
                ),
                file=key_file,
            )
        except discord.Forbidden:
            pass

        await interaction.followup.send(embed=make_embed(
            "✅ SSH Key Regenerated",
            "Your new private key has been sent to your DMs.",
            SUCCESS_COLOR,
        ))
    except Exception as e:
        log.error(f"regen-ssh: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="vps-performance", description="Show live performance stats for your VPS.")
@app_commands.describe(vps_id="Your VPS ID (e.g. BC-0001)")
async def cmd_vps_performance(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=make_embed(
            "❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))
    try:
        with db_connect() as conn:
            row = conn.execute("SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
        container = get_docker().containers.get(row["container_id"])
        container.reload()
        status = container.status

        if status != "running":
            return await interaction.followup.send(embed=make_embed(
                "📊 VPS Performance",
                f"Container is **{status}** — start it first to view live stats.",
                WARN_COLOR,
                fields=[("VPS ID", vps_id, True), ("Status", status.capitalize(), True)],
            ))

        stats = container_stats(container)
        disk_result = container.exec_run("df -BG / --output=used | tail -1", tty=False)
        disk_used = disk_result.output.decode().strip().replace("G", "") if disk_result.exit_code == 0 else "N/A"

        await interaction.followup.send(embed=make_embed(
            "📊 VPS Performance", "", INFO_COLOR,
            fields=[
                ("🆔 VPS ID",    vps_id,                                                         True),
                ("📌 Status",    status.capitalize(),                                             True),
                ("🔌 SSH Port",  str(row["ssh_port"]),                                           True),
                ("💻 CPU",       f"{stats['cpu_pct']}%",                                         True),
                ("🧠 RAM",       f"{stats['mem_usage_mb']} / {stats['mem_limit_mb']} MB ({stats['mem_pct']}%)", True),
                ("💾 Disk",      f"{disk_used} GB / {row['disk_gb']} GB",                        True),
                ("⏱ Uptime",    stats["uptime"],                                                 True),
                ("🌐 Net RX",    f"{stats['net_rx_mb']} MB",                                     True),
                ("🌐 Net TX",    f"{stats['net_tx_mb']} MB",                                     True),
            ],
        ))
    except Exception as e:
        log.error(f"vps-performance: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="my-vps", description="List all your VPS instances.")
async def cmd_my_vps(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM vps_instances WHERE user_id=? ORDER BY vps_id",
            (interaction.user.id,),
        ).fetchall()
    if not rows:
        return await interaction.followup.send(embed=make_embed(
            "📋 My VPS", "You have no VPS instances.", WARN_COLOR))
    fields = [
        (
            r["vps_id"],
            f"OS: `{r['os_image']}` | RAM: `{r['memory_mb']}MB` | SSH Port: `{r['ssh_port']}` | Status: `{r['status']}`",
            False,
        )
        for r in rows
    ]
    await interaction.followup.send(embed=make_embed(
        f"📋 My VPS ({len(rows)} instance{'s' if len(rows) != 1 else ''})",
        "", INFO_COLOR, fields=fields,
    ))


# ══════════════════════════════════════════════
# ADMIN COMMANDS
# ══════════════════════════════════════════════

def admin_check(interaction: discord.Interaction) -> bool:
    return is_admin(interaction)


@bot.tree.command(name="admin-add-user", description="[Admin] Grant a user hosting access.")
@app_commands.describe(user="Discord user to grant access")
async def cmd_admin_add_user(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))
    with db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO allowed_users (user_id, added_by) VALUES (?, ?)",
            (user.id, interaction.user.id),
        )
    log.info(f"{interaction.user} added {user} to allowed_users.")
    await interaction.followup.send(embed=make_embed(
        "✅ User Added", f"{user.mention} can now access hosting services.", SUCCESS_COLOR))


@bot.tree.command(name="admin-remove-user", description="[Admin] Revoke a user's hosting access.")
@app_commands.describe(user="Discord user to revoke access from")
async def cmd_admin_remove_user(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))
    with db_connect() as conn:
        conn.execute("DELETE FROM allowed_users WHERE user_id=?", (user.id,))
    log.info(f"{interaction.user} removed {user} from allowed_users.")
    await interaction.followup.send(embed=make_embed(
        "🗑 User Removed", f"{user.mention}'s hosting access has been revoked.", WARN_COLOR))


@bot.tree.command(name="create", description="[Admin] Create a new VPS for a user.")
@app_commands.describe(
    user="Target Discord user",
    memory="RAM in MB (e.g. 512)",
    cpu="CPU cores (e.g. 1.0)",
    disk="Disk size in GB (recorded; use volumes for enforcement)",
    os="OS template: ubuntu22 | debian11",
)
async def cmd_create(
    interaction: discord.Interaction,
    user: discord.Member,
    memory: int,
    cpu: float,
    disk: int,
    os: str,
):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))

    os_lower = os.lower()
    if os_lower not in OS_MAP:
        return await interaction.followup.send(embed=make_embed(
            "❌ Invalid OS",
            f"Supported templates: {', '.join(OS_MAP.keys())}",
            ERROR_COLOR,
        ))

    image     = OS_MAP[os_lower]
    vps_id    = next_vps_id()
    host_port = next_ssh_port()

    # Generate SSH keypair for this VPS
    private_key, public_key = generate_ssh_keypair(vps_id)

    try:
        container = create_container(vps_id, image, memory, cpu, public_key, host_port)
    except Exception as e:
        log.error(f"create container: {e}")
        delete_ssh_key(vps_id)
        return await interaction.followup.send(embed=make_embed("❌ Docker Error", str(e), ERROR_COLOR))

    cpu_period = 100_000
    cpu_quota  = int(cpu_period * cpu)

    with db_connect() as conn:
        conn.execute("""
            INSERT INTO vps_instances
              (vps_id, user_id, container_id, os_image, memory_mb, cpu_period, cpu_quota, disk_gb, ssh_port, ssh_pubkey, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
        """, (vps_id, user.id, container.id, image, memory, cpu_period, cpu_quota, disk, host_port, public_key))

    log.info(f"{interaction.user} created VPS {vps_id} for {user} on SSH port {host_port}.")

    # DM private key as a file attachment to the new owner
    key_file = discord.File(
        io.BytesIO(private_key.encode()),
        filename=f"{vps_id}_id_rsa.pem",
    )
    try:
        dm = await user.create_dm()
        await dm.send(
            embed=make_embed(
                "🎉 Your New VPS is Ready!",
                "Your private SSH key is attached below. **Save it — it won't be shown again.**\n\n"
                "Connect to your VPS with:\n"
                f"```ssh -i {vps_id}_id_rsa.pem -p {host_port} root@YOUR_VPS_IP```\n"
                "On Linux/macOS first run:\n"
                f"```chmod 400 {vps_id}_id_rsa.pem```",
                SUCCESS_COLOR,
                fields=[
                    ("VPS ID",   vps_id,          True),
                    ("OS",       image,            True),
                    ("RAM",      f"{memory} MB",   True),
                    ("CPU",      f"{cpu} core(s)", True),
                    ("Disk",     f"{disk} GB",     True),
                    ("SSH Port", str(host_port),   True),
                ],
            ),
            file=key_file,
        )
    except discord.Forbidden:
        log.warning(f"Could not DM {user} — DMs may be disabled.")

    await interaction.followup.send(embed=make_embed(
        "✅ VPS Created",
        f"VPS **{vps_id}** provisioned for {user.mention}.\nSSH key sent via DM.",
        SUCCESS_COLOR,
        fields=[
            ("VPS ID",   vps_id,          True),
            ("OS",       image,            True),
            ("RAM",      f"{memory} MB",   True),
            ("CPU",      f"{cpu} core(s)", True),
            ("SSH Port", str(host_port),   True),
        ],
    ))


@bot.tree.command(name="suspend-vps", description="[Admin] Suspend a VPS instance.")
@app_commands.describe(vps_id="VPS ID to suspend (e.g. BC-0001)")
async def cmd_suspend_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))
    vps_id = vps_id.upper()
    with db_connect() as conn:
        row = conn.execute("SELECT container_id FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
    if not row:
        return await interaction.followup.send(embed=make_embed(
            "❌ Not Found", f"VPS **{vps_id}** not found.", ERROR_COLOR))
    try:
        get_docker().containers.get(row["container_id"]).stop()
    except Exception:
        pass
    with db_connect() as conn:
        conn.execute("UPDATE vps_instances SET status='suspended' WHERE vps_id=?", (vps_id,))
    log.info(f"{interaction.user} suspended VPS {vps_id}.")
    await interaction.followup.send(embed=make_embed(
        "⛔ VPS Suspended", f"**{vps_id}** has been suspended.", WARN_COLOR))


@bot.tree.command(name="unsuspend-vps", description="[Admin] Reactivate a suspended VPS.")
@app_commands.describe(vps_id="VPS ID to unsuspend (e.g. BC-0001)")
async def cmd_unsuspend_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))
    vps_id = vps_id.upper()
    with db_connect() as conn:
        row = conn.execute("SELECT container_id FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
    if not row:
        return await interaction.followup.send(embed=make_embed(
            "❌ Not Found", f"VPS **{vps_id}** not found.", ERROR_COLOR))
    try:
        get_docker().containers.get(row["container_id"]).start()
        with db_connect() as conn:
            conn.execute("UPDATE vps_instances SET status='running' WHERE vps_id=?", (vps_id,))
        await interaction.followup.send(embed=make_embed(
            "✅ VPS Unsuspended", f"**{vps_id}** has been reactivated.", SUCCESS_COLOR))
    except Exception as e:
        log.error(f"unsuspend: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))
    log.info(f"{interaction.user} unsuspended VPS {vps_id}.")


@bot.tree.command(name="remove-vps", description="[Admin] Permanently delete a VPS instance.")
@app_commands.describe(vps_id="VPS ID to remove (e.g. BC-0001)")
async def cmd_remove_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))
    vps_id = vps_id.upper()
    with db_connect() as conn:
        row = conn.execute("SELECT container_id FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
    if not row:
        return await interaction.followup.send(embed=make_embed(
            "❌ Not Found", f"VPS **{vps_id}** not found.", ERROR_COLOR))
    try:
        get_docker().containers.get(row["container_id"]).remove(force=True)
    except Exception:
        pass
    delete_ssh_key(vps_id)
    with db_connect() as conn:
        conn.execute("DELETE FROM vps_instances WHERE vps_id=?", (vps_id,))
    log.info(f"{interaction.user} permanently removed VPS {vps_id}.")
    await interaction.followup.send(embed=make_embed(
        "🗑 VPS Removed", f"**{vps_id}** has been permanently deleted.", WARN_COLOR))


@bot.tree.command(name="list-vps", description="[Admin] List all VPS instances on the node.")
async def cmd_list_vps(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM vps_instances ORDER BY vps_id").fetchall()
    if not rows:
        return await interaction.followup.send(embed=make_embed(
            "📋 All VPS Instances", "No VPS instances found.", WARN_COLOR))
    fields = [
        (
            r["vps_id"],
            f"User: <@{r['user_id']}> | OS: `{r['os_image']}` | RAM: `{r['memory_mb']}MB` | SSH: `:{r['ssh_port']}` | Status: `{r['status']}`",
            False,
        )
        for r in rows
    ]
    for i in range(0, len(fields), 25):
        await interaction.followup.send(embed=make_embed(
            f"📋 All VPS ({len(rows)} total) — Page {i // 25 + 1}",
            "", INFO_COLOR, fields=fields[i:i + 25],
        ))


@bot.tree.command(name="node-stats", description="[Admin] Show host node resource usage.")
async def cmd_node_stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))
    cpu_pct = psutil.cpu_percent(interval=1)
    mem     = psutil.virtual_memory()
    disk    = psutil.disk_usage("/")
    try:
        client  = get_docker()
        running = len([c for c in client.containers.list() if c.status == "running"])
        total   = len(client.containers.list(all=True))
    except Exception:
        running = total = 0
    await interaction.followup.send(embed=make_embed(
        "🖥 Node Statistics", "", INFO_COLOR,
        fields=[
            ("🖥 Host CPU",           f"{cpu_pct}%",                                                                    True),
            ("🧠 Host RAM",           f"{round(mem.used/1024**3,2)} / {round(mem.total/1024**3,2)} GB ({mem.percent}%)", True),
            ("💾 Host Disk",          f"{bytes_to_gb(disk.used)} / {bytes_to_gb(disk.total)} GB ({disk.percent}%)",      True),
            ("🐳 Running Containers", str(running),                                                                      True),
            ("📦 Total Containers",   str(total),                                                                        True),
        ],
    ))


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.critical("DISCORD_TOKEN not set. Check your .env file.")
        raise SystemExit(1)
    db_init()
    log.info("Starting Blined Cloud VPS Manager…")
    bot.run(DISCORD_TOKEN, log_handler=None)
