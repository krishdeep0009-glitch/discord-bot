"""
Blined Cloud VPS Manager - Discord Bot
A professional Discord bot for managing Docker-based VPS instances.
"""

import os
import re
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
from discord.ext import commands, tasks
import docker
import psutil
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# Environment & Logging Setup
# ─────────────────────────────────────────────
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))

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
BRAND_COLOR  = 0x5865F2          # Blurple
SUCCESS_COLOR = 0x57F287         # Green
ERROR_COLOR   = 0xED4245         # Red
WARN_COLOR    = 0xFEE75C         # Yellow
INFO_COLOR    = 0x5865F2         # Blurple

FOOTER_TEXT = "Powered by Blined Cloud"
FOOTER_ICON = None               # Set a URL string for a custom footer icon

OS_MAP = {
    "ubuntu22": "ubuntu:22.04",
    "debian11": "debian:11",
}

DB_PATH = "blinedcloud.db"

# ─────────────────────────────────────────────
# Database Helpers
# ─────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with row_factory set."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    """Create all required tables on first run."""
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
                root_password TEXT,
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
    """Return a branded Discord embed."""
    embed = discord.Embed(title=title, description=description, color=color,
                          timestamp=datetime.datetime.utcnow())
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    for name, value, inline in (fields or []):
        embed.add_field(name=name, value=value, inline=inline)
    return embed


# ─────────────────────────────────────────────
# VPS ID Generator
# ─────────────────────────────────────────────

def next_vps_id() -> str:
    """Return the next sequential VPS ID in BC-XXXX format."""
    with db_connect() as conn:
        row = conn.execute(
            "SELECT vps_id FROM vps_instances ORDER BY vps_id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return "BC-0001"
    last_num = int(row["vps_id"].split("-")[1])
    return f"BC-{last_num + 1:04d}"


def random_password(length: int = 16) -> str:
    """Generate a secure random root password."""
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(random.SystemRandom().choices(chars, k=length))


# ─────────────────────────────────────────────
# Docker Helpers
# ─────────────────────────────────────────────

def get_docker() -> docker.DockerClient:
    return docker.from_env()


def create_container(vps_id: str, image: str, memory_mb: int,
                     cpu_cores: float, password: str) -> docker.models.containers.Container:
    """
    Pull image (if needed) and create a Docker container simulating a VPS.
    The container runs an SSH-capable shell via /bin/bash in a keep-alive loop.
    In a real deployment you would install openssh-server and expose port 22.
    """
    client = get_docker()

    # CPU quota: cpu_period * cpu_cores (100 000 µs period by default)
    cpu_period = 100_000
    cpu_quota  = int(cpu_period * cpu_cores)

    # Environment injects the root password so init scripts can set it
    container = client.containers.run(
        image,
        name=vps_id,
        detach=True,
        mem_limit=f"{memory_mb}m",
        cpu_period=cpu_period,
        cpu_quota=cpu_quota,
        environment={"ROOT_PASSWORD": password, "VPS_ID": vps_id},
        # Keep container alive (replace with real SSH entrypoint in production)
        command="bash -c 'echo root:$ROOT_PASSWORD | chpasswd 2>/dev/null || true; tail -f /dev/null'",
        labels={"managed-by": "blined-cloud", "vps-id": vps_id},
    )
    return container


def container_stats(container) -> dict:
    """Return a snapshot of CPU / RAM / net stats for a running container."""
    raw = container.stats(stream=False)

    # CPU %
    cpu_delta   = raw["cpu_stats"]["cpu_usage"]["total_usage"] - \
                  raw["precpu_stats"]["cpu_usage"]["total_usage"]
    sys_delta   = raw["cpu_stats"]["system_cpu_usage"] - \
                  raw["precpu_stats"]["system_cpu_usage"]
    num_cpus    = raw["cpu_stats"].get("online_cpus",
                  len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [1])))
    cpu_pct     = (cpu_delta / sys_delta) * num_cpus * 100.0 if sys_delta else 0.0

    # RAM
    mem_usage   = raw["memory_stats"].get("usage", 0)
    mem_limit   = raw["memory_stats"].get("limit", 1)
    mem_pct     = (mem_usage / mem_limit) * 100.0

    # Network
    net_rx = net_tx = 0
    for iface_stats in raw.get("networks", {}).values():
        net_rx += iface_stats.get("rx_bytes", 0)
        net_tx += iface_stats.get("tx_bytes", 0)

    # Uptime from container attrs
    started_at = container.attrs["State"].get("StartedAt", "")
    uptime_str = "N/A"
    if started_at and started_at != "0001-01-01T00:00:00Z":
        try:
            start = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            delta = datetime.datetime.now(datetime.timezone.utc) - start
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            mins,  secs = divmod(rem, 60)
            uptime_str = f"{hours}h {mins}m {secs}s"
        except Exception:
            pass

    return {
        "cpu_pct": round(cpu_pct, 2),
        "mem_usage_mb": round(mem_usage / 1024 / 1024, 1),
        "mem_limit_mb": round(mem_limit / 1024 / 1024, 1),
        "mem_pct": round(mem_pct, 2),
        "net_rx_mb": round(net_rx / 1024 / 1024, 2),
        "net_tx_mb": round(net_tx / 1024 / 1024, 2),
        "uptime": uptime_str,
    }


def bytes_to_gb(b: int) -> float:
    return round(b / 1024 ** 3, 2)


# ─────────────────────────────────────────────
# Permission Checks
# ─────────────────────────────────────────────

def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    return any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles)


def is_allowed_user(user_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def owns_vps(user_id: int, vps_id: str) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM vps_instances WHERE vps_id = ? AND user_id = ?",
            (vps_id, user_id),
        ).fetchone()
    return row is not None


# ─────────────────────────────────────────────
# Bot Setup
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
        log.info("Bot is ready.")


bot = BlinedCloudBot()


# ─────────────────────────────────────────────────────────────────────────────
# USER COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

@bot.tree.command(name="start", description="Start your VPS instance.")
@app_commands.describe(vps_id="Your VPS ID (e.g. BC-0001)")
async def cmd_start(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()

    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=make_embed(
            "❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))

    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM vps_instances WHERE vps_id = ?", (vps_id,)
        ).fetchone()

    if row["status"] == "suspended":
        return await interaction.followup.send(embed=make_embed(
            "⛔ Suspended", "This VPS is suspended. Contact an admin.", WARN_COLOR))

    try:
        client = get_docker()
        container = client.containers.get(row["container_id"])
        container.start()
        with db_connect() as conn:
            conn.execute("UPDATE vps_instances SET status='running' WHERE vps_id=?", (vps_id,))
        await interaction.followup.send(embed=make_embed(
            "✅ VPS Started", f"**{vps_id}** has been started successfully.", SUCCESS_COLOR))
    except Exception as e:
        log.error(f"start error: {e}")
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
            row = conn.execute(
                "SELECT container_id FROM vps_instances WHERE vps_id=?", (vps_id,)
            ).fetchone()
        client = get_docker()
        container = client.containers.get(row["container_id"])
        container.stop()
        with db_connect() as conn:
            conn.execute("UPDATE vps_instances SET status='stopped' WHERE vps_id=?", (vps_id,))
        await interaction.followup.send(embed=make_embed(
            "🛑 VPS Stopped", f"**{vps_id}** has been stopped.", WARN_COLOR))
    except Exception as e:
        log.error(f"stop error: {e}")
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
            row = conn.execute(
                "SELECT container_id, status FROM vps_instances WHERE vps_id=?", (vps_id,)
            ).fetchone()

        if row["status"] == "suspended":
            return await interaction.followup.send(embed=make_embed(
                "⛔ Suspended", "This VPS is suspended. Contact an admin.", WARN_COLOR))

        client = get_docker()
        container = client.containers.get(row["container_id"])
        container.restart()
        with db_connect() as conn:
            conn.execute("UPDATE vps_instances SET status='running' WHERE vps_id=?", (vps_id,))
        await interaction.followup.send(embed=make_embed(
            "🔄 VPS Restarted", f"**{vps_id}** has been restarted.", SUCCESS_COLOR))
    except Exception as e:
        log.error(f"restart error: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="reinstall", description="Reinstall your VPS (keeps same specs, resets data).")
@app_commands.describe(vps_id="Your VPS ID (e.g. BC-0001)")
async def cmd_reinstall(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()

    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=make_embed(
            "❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))

    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,)
            ).fetchone()

        client = get_docker()
        # Remove old container
        try:
            old = client.containers.get(row["container_id"])
            old.remove(force=True)
        except Exception:
            pass

        new_password = random_password()
        cpu_cores = row["cpu_quota"] / row["cpu_period"]
        new_container = create_container(
            vps_id, row["os_image"], row["memory_mb"], cpu_cores, new_password
        )

        with db_connect() as conn:
            conn.execute("""
                UPDATE vps_instances
                SET container_id=?, root_password=?, status='running'
                WHERE vps_id=?
            """, (new_container.id, new_password, vps_id))

        # DM credentials
        try:
            dm = await interaction.user.create_dm()
            await dm.send(embed=make_embed(
                "🔄 VPS Reinstalled",
                f"Your VPS **{vps_id}** has been reinstalled.",
                SUCCESS_COLOR,
                fields=[
                    ("VPS ID", vps_id, True),
                    ("OS Image", row["os_image"], True),
                    ("New Root Password", f"||{new_password}||", False),
                ],
            ))
        except discord.Forbidden:
            pass

        await interaction.followup.send(embed=make_embed(
            "✅ Reinstalled",
            f"**{vps_id}** has been reinstalled. Check your DMs for credentials.",
            SUCCESS_COLOR,
        ))
    except Exception as e:
        log.error(f"reinstall error: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="regen-ssh", description="Regenerate the root password for your VPS.")
@app_commands.describe(vps_id="Your VPS ID (e.g. BC-0001)")
async def cmd_regen_ssh(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()

    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=make_embed(
            "❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))

    try:
        new_password = random_password()
        with db_connect() as conn:
            row = conn.execute(
                "SELECT container_id FROM vps_instances WHERE vps_id=?", (vps_id,)
            ).fetchone()
            conn.execute(
                "UPDATE vps_instances SET root_password=? WHERE vps_id=?",
                (new_password, vps_id),
            )

        # Apply new password inside running container
        client = get_docker()
        try:
            container = client.containers.get(row["container_id"])
            container.exec_run(f"bash -c 'echo root:{new_password} | chpasswd'")
        except Exception:
            pass

        try:
            dm = await interaction.user.create_dm()
            await dm.send(embed=make_embed(
                "🔑 SSH Credentials Updated",
                f"New root password for **{vps_id}**:",
                INFO_COLOR,
                fields=[("New Root Password", f"||{new_password}||", False)],
            ))
        except discord.Forbidden:
            pass

        await interaction.followup.send(embed=make_embed(
            "✅ Password Regenerated",
            "New credentials have been sent to your DMs.",
            SUCCESS_COLOR,
        ))
    except Exception as e:
        log.error(f"regen-ssh error: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="vps-performance", description="Show performance stats for your VPS.")
@app_commands.describe(vps_id="Your VPS ID (e.g. BC-0001)")
async def cmd_vps_performance(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()

    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=make_embed(
            "❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))

    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,)
            ).fetchone()

        client = get_docker()
        container = client.containers.get(row["container_id"])
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

        # Disk: report allocated disk_gb as limit; actual usage needs du inside container
        disk_limit = row["disk_gb"]
        disk_used_result = container.exec_run(
            "df -BG / --output=used | tail -1", tty=False
        )
        disk_used = disk_used_result.output.decode().strip().replace("G", "") if disk_used_result.exit_code == 0 else "N/A"

        fields = [
            ("🆔 VPS ID",          vps_id,                                      True),
            ("📌 Status",           status.capitalize(),                         True),
            ("💻 CPU Usage",        f"{stats['cpu_pct']}%",                      True),
            ("🧠 RAM Usage",        f"{stats['mem_usage_mb']} / {stats['mem_limit_mb']} MB ({stats['mem_pct']}%)", True),
            ("💾 Disk Used",        f"{disk_used} GB / {disk_limit} GB allocated", True),
            ("⏱ Uptime",           stats["uptime"],                             True),
            ("🌐 Net RX",           f"{stats['net_rx_mb']} MB",                  True),
            ("🌐 Net TX",           f"{stats['net_tx_mb']} MB",                  True),
        ]

        await interaction.followup.send(embed=make_embed(
            "📊 VPS Performance", "", INFO_COLOR, fields=fields
        ))
    except Exception as e:
        log.error(f"vps-performance error: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="my-vps", description="List all VPS instances you own.")
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

    fields = []
    for r in rows:
        fields.append((
            r["vps_id"],
            f"OS: `{r['os_image']}` | RAM: `{r['memory_mb']}MB` | Status: `{r['status']}`",
            False,
        ))

    await interaction.followup.send(embed=make_embed(
        f"📋 My VPS ({len(rows)} instance{'s' if len(rows) != 1 else ''})",
        "",
        INFO_COLOR,
        fields=fields,
    ))


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

def admin_check(interaction: discord.Interaction) -> bool:
    return is_admin(interaction)


@bot.tree.command(name="admin-add-user", description="[Admin] Grant a user access to create VPS instances.")
@app_commands.describe(user="The Discord user to grant access")
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

    log.info(f"Admin {interaction.user} added user {user} to allowed_users.")
    await interaction.followup.send(embed=make_embed(
        "✅ User Added",
        f"{user.mention} can now access hosting services.",
        SUCCESS_COLOR,
    ))


@bot.tree.command(name="admin-remove-user", description="[Admin] Revoke a user's hosting access.")
@app_commands.describe(user="The Discord user to revoke access from")
async def cmd_admin_remove_user(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))

    with db_connect() as conn:
        conn.execute("DELETE FROM allowed_users WHERE user_id=?", (user.id,))

    log.info(f"Admin {interaction.user} removed user {user} from allowed_users.")
    await interaction.followup.send(embed=make_embed(
        "🗑 User Removed",
        f"{user.mention}'s hosting access has been revoked.",
        WARN_COLOR,
    ))


@bot.tree.command(name="create", description="[Admin] Create a new VPS for a user.")
@app_commands.describe(
    user="Target Discord user",
    memory="RAM in MB (e.g. 512)",
    cpu="CPU cores (e.g. 1.0)",
    disk="Disk size in GB (recorded only; use Docker volumes for enforcement)",
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

    image = OS_MAP[os_lower]
    vps_id = next_vps_id()
    password = random_password()

    try:
        container = create_container(vps_id, image, memory, cpu, password)
    except Exception as e:
        log.error(f"create container error: {e}")
        return await interaction.followup.send(embed=make_embed("❌ Docker Error", str(e), ERROR_COLOR))

    cpu_period = 100_000
    cpu_quota  = int(cpu_period * cpu)

    with db_connect() as conn:
        conn.execute("""
            INSERT INTO vps_instances
              (vps_id, user_id, container_id, os_image, memory_mb, cpu_period, cpu_quota, disk_gb, root_password, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
        """, (vps_id, user.id, container.id, image, memory, cpu_period, cpu_quota, disk, password))

    log.info(f"Admin {interaction.user} created VPS {vps_id} for {user}.")

    # DM credentials to the new VPS owner
    try:
        dm = await user.create_dm()
        await dm.send(embed=make_embed(
            "🎉 Your New VPS is Ready!",
            "Here are your VPS credentials. Keep them safe!",
            SUCCESS_COLOR,
            fields=[
                ("VPS ID",        vps_id,             True),
                ("OS",            image,               True),
                ("RAM",           f"{memory} MB",      True),
                ("CPU",           f"{cpu} core(s)",    True),
                ("Disk",          f"{disk} GB",        True),
                ("Root Password", f"||{password}||",   False),
            ],
        ))
    except discord.Forbidden:
        pass

    await interaction.followup.send(embed=make_embed(
        "✅ VPS Created",
        f"VPS **{vps_id}** created for {user.mention}. Credentials sent via DM.",
        SUCCESS_COLOR,
        fields=[
            ("VPS ID", vps_id, True),
            ("OS",     image,  True),
            ("RAM",    f"{memory} MB", True),
            ("CPU",    f"{cpu} core(s)", True),
        ],
    ))


@bot.tree.command(name="suspend-vps", description="[Admin] Suspend a VPS instance.")
@app_commands.describe(vps_id="The VPS ID to suspend (e.g. BC-0001)")
async def cmd_suspend_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))

    vps_id = vps_id.upper()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT container_id FROM vps_instances WHERE vps_id=?", (vps_id,)
        ).fetchone()

    if not row:
        return await interaction.followup.send(embed=make_embed(
            "❌ Not Found", f"VPS **{vps_id}** not found.", ERROR_COLOR))

    try:
        client = get_docker()
        container = client.containers.get(row["container_id"])
        container.stop()
    except Exception:
        pass

    with db_connect() as conn:
        conn.execute("UPDATE vps_instances SET status='suspended' WHERE vps_id=?", (vps_id,))

    log.info(f"Admin {interaction.user} suspended VPS {vps_id}.")
    await interaction.followup.send(embed=make_embed(
        "⛔ VPS Suspended", f"**{vps_id}** has been suspended.", WARN_COLOR))


@bot.tree.command(name="unsuspend-vps", description="[Admin] Reactivate a suspended VPS.")
@app_commands.describe(vps_id="The VPS ID to unsuspend (e.g. BC-0001)")
async def cmd_unsuspend_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))

    vps_id = vps_id.upper()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT container_id FROM vps_instances WHERE vps_id=?", (vps_id,)
        ).fetchone()

    if not row:
        return await interaction.followup.send(embed=make_embed(
            "❌ Not Found", f"VPS **{vps_id}** not found.", ERROR_COLOR))

    try:
        client = get_docker()
        container = client.containers.get(row["container_id"])
        container.start()
        with db_connect() as conn:
            conn.execute("UPDATE vps_instances SET status='running' WHERE vps_id=?", (vps_id,))
        await interaction.followup.send(embed=make_embed(
            "✅ VPS Unsuspended", f"**{vps_id}** has been reactivated.", SUCCESS_COLOR))
    except Exception as e:
        log.error(f"unsuspend error: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))

    log.info(f"Admin {interaction.user} unsuspended VPS {vps_id}.")


@bot.tree.command(name="remove-vps", description="[Admin] Permanently delete a VPS instance.")
@app_commands.describe(vps_id="The VPS ID to remove (e.g. BC-0001)")
async def cmd_remove_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))

    vps_id = vps_id.upper()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT container_id FROM vps_instances WHERE vps_id=?", (vps_id,)
        ).fetchone()

    if not row:
        return await interaction.followup.send(embed=make_embed(
            "❌ Not Found", f"VPS **{vps_id}** not found.", ERROR_COLOR))

    try:
        client = get_docker()
        container = client.containers.get(row["container_id"])
        container.remove(force=True)
    except Exception:
        pass

    with db_connect() as conn:
        conn.execute("DELETE FROM vps_instances WHERE vps_id=?", (vps_id,))

    log.info(f"Admin {interaction.user} permanently removed VPS {vps_id}.")
    await interaction.followup.send(embed=make_embed(
        "🗑 VPS Removed",
        f"**{vps_id}** has been permanently deleted.",
        WARN_COLOR,
    ))


@bot.tree.command(name="list-vps", description="[Admin] List all VPS instances on the node.")
async def cmd_list_vps(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))

    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM vps_instances ORDER BY vps_id"
        ).fetchall()

    if not rows:
        return await interaction.followup.send(embed=make_embed(
            "📋 All VPS Instances", "No VPS instances found.", WARN_COLOR))

    fields = []
    for r in rows:
        fields.append((
            r["vps_id"],
            f"User: <@{r['user_id']}> | OS: `{r['os_image']}` | RAM: `{r['memory_mb']}MB` | Status: `{r['status']}`",
            False,
        ))

    # Discord embeds have a 25-field limit; split if needed
    embeds = []
    for i in range(0, len(fields), 25):
        chunk = fields[i:i + 25]
        embeds.append(make_embed(
            f"📋 All VPS Instances ({len(rows)} total)",
            f"Page {i // 25 + 1}",
            INFO_COLOR,
            fields=chunk,
        ))

    for embed in embeds:
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="node-stats", description="[Admin] Show host node resource usage.")
async def cmd_node_stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not admin_check(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))

    cpu_pct  = psutil.cpu_percent(interval=1)
    mem      = psutil.virtual_memory()
    disk     = psutil.disk_usage("/")

    try:
        client = get_docker()
        running = len([c for c in client.containers.list() if c.status == "running"])
        total   = len(client.containers.list(all=True))
    except Exception:
        running = total = 0

    fields = [
        ("🖥 Host CPU",              f"{cpu_pct}%",                                           True),
        ("🧠 Host RAM",              f"{round(mem.used/1024**3,2)} / {round(mem.total/1024**3,2)} GB ({mem.percent}%)", True),
        ("💾 Host Disk",             f"{bytes_to_gb(disk.used)} / {bytes_to_gb(disk.total)} GB ({disk.percent}%)", True),
        ("🐳 Running Containers",    str(running),                                            True),
        ("📦 Total Containers",      str(total),                                              True),
    ]

    await interaction.followup.send(embed=make_embed("🖥 Node Statistics", "", INFO_COLOR, fields=fields))


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.critical("DISCORD_TOKEN not set. Check your .env file.")
        raise SystemExit(1)

    db_init()
    log.info("Starting Blined Cloud VPS Manager…")
    bot.run(DISCORD_TOKEN, log_handler=None)   # log_handler=None uses our own logger
