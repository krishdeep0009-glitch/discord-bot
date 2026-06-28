"""
Blined Cloud VPS Manager - Discord Bot
Flow on VPS creation:
  1. apt update
  2. apt install tmate
  3. tmate  (run it, capture the SSH line)
  4. Send SSH line directly in the Discord channel
"""

import os
import time
import asyncio
import logging
import sqlite3
import datetime

import discord
from discord import app_commands
from discord.ext import commands
import docker
import psutil
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# Environment & Logging
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
BRAND_COLOR   = 0x5865F2
SUCCESS_COLOR = 0x57F287
ERROR_COLOR   = 0xED4245
WARN_COLOR    = 0xFEE75C
INFO_COLOR    = 0x5865F2

FOOTER_TEXT = "Powered by Blined Cloud"
FOOTER_ICON = None

OS_MAP = {
    "ubuntu22": "ubuntu:22.04",
    "debian11":  "debian:11",
}

DB_PATH = "blinedcloud.db"


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
                vps_id       TEXT PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                container_id TEXT,
                os_image     TEXT,
                memory_mb    INTEGER,
                cpu_period   INTEGER DEFAULT 100000,
                cpu_quota    INTEGER,
                disk_gb      INTEGER,
                tmate_ssh    TEXT,
                tmate_web    TEXT,
                status       TEXT DEFAULT 'running',
                created_at   TEXT DEFAULT (datetime('now'))
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
# Helpers
# ─────────────────────────────────────────────

def next_vps_id() -> str:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT vps_id FROM vps_instances ORDER BY vps_id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return "BC-0001"
    return f"BC-{int(row['vps_id'].split('-')[1]) + 1:04d}"


def bytes_to_gb(b: int) -> float:
    return round(b / 1024 ** 3, 2)


def get_docker() -> docker.DockerClient:
    return docker.from_env()


# ─────────────────────────────────────────────
# Core VPS / tmate Logic
# ─────────────────────────────────────────────

def provision_vps(vps_id: str, image: str, memory_mb: int, cpu_cores: float) -> tuple[docker.models.containers.Container, str, str]:
    """
    1. Pull image & start container (kept alive with tail -f /dev/null)
    2. apt update
    3. apt install -y tmate
    4. Run tmate, wait for it to print the SSH line
    Returns (container, ssh_cmd, web_url)
    """
    client     = get_docker()
    cpu_period = 100_000
    cpu_quota  = int(cpu_period * cpu_cores)

    log.info(f"[{vps_id}] Starting container from {image}...")
    container = client.containers.run(
        image,
        name=vps_id,
        detach=True,
        mem_limit=f"{memory_mb}m",
        cpu_period=cpu_period,
        cpu_quota=cpu_quota,
        environment={"TERM": "xterm-256color", "VPS_ID": vps_id},
        command="tail -f /dev/null",   # keep alive; we exec commands below
        labels={"managed-by": "blined-cloud", "vps-id": vps_id},
    )

    # ── Step 1: apt update ──────────────────────────────────────────
    log.info(f"[{vps_id}] Running apt update...")
    result = container.exec_run(
        "bash -c 'apt update -y 2>&1'",
        tty=False,
    )
    log.info(f"[{vps_id}] apt update exit={result.exit_code}")

    # ── Step 2: apt install tmate ───────────────────────────────────
    log.info(f"[{vps_id}] Installing tmate...")
    result = container.exec_run(
        "bash -c 'apt install -y tmate 2>&1'",
        tty=False,
    )
    log.info(f"[{vps_id}] apt install tmate exit={result.exit_code}")

    # ── Step 3: Run tmate and capture SSH + web lines ───────────────
    log.info(f"[{vps_id}] Starting tmate session...")
    # tmate -F keeps running in foreground; we use a socket approach instead
    # so we can query the links without blocking.
    sock = "/tmp/tmate.sock"
    container.exec_run(
        f"bash -c 'rm -f {sock}; tmate -S {sock} new-session -d 2>&1'",
        tty=False,
    )
    time.sleep(3)

    # Wait for tmate-ready signal (up to 30 s)
    container.exec_run(
        f"bash -c 'tmate -S {sock} wait tmate-ready 2>&1'",
        tty=False,
    )

    # Extract lines
    ssh_result = container.exec_run(
        f"bash -c \"tmate -S {sock} display -p '#{{tmate_ssh}}' 2>/dev/null\"",
        tty=False,
    )
    web_result = container.exec_run(
        f"bash -c \"tmate -S {sock} display -p '#{{tmate_web}}' 2>/dev/null\"",
        tty=False,
    )

    ssh_cmd = ssh_result.output.decode(errors="ignore").strip() if ssh_result.output else ""
    web_url = web_result.output.decode(errors="ignore").strip() if web_result.output else ""

    log.info(f"[{vps_id}] tmate SSH: {ssh_cmd}")
    log.info(f"[{vps_id}] tmate Web: {web_url}")

    return container, ssh_cmd, web_url


def regen_tmate(container) -> tuple[str, str]:
    """Kill old tmate session and start a fresh one. Returns (ssh_cmd, web_url)."""
    sock = "/tmp/tmate.sock"
    container.exec_run("bash -c 'pkill tmate; rm -f /tmp/tmate.sock'", tty=False)
    time.sleep(2)

    container.exec_run(
        f"bash -c 'tmate -S {sock} new-session -d 2>&1'",
        tty=False,
    )
    time.sleep(3)
    container.exec_run(
        f"bash -c 'tmate -S {sock} wait tmate-ready 2>&1'",
        tty=False,
    )

    ssh_result = container.exec_run(
        f"bash -c \"tmate -S {sock} display -p '#{{tmate_ssh}}' 2>/dev/null\"",
        tty=False,
    )
    web_result = container.exec_run(
        f"bash -c \"tmate -S {sock} display -p '#{{tmate_web}}' 2>/dev/null\"",
        tty=False,
    )

    ssh_cmd = ssh_result.output.decode(errors="ignore").strip() if ssh_result.output else ""
    web_url = web_result.output.decode(errors="ignore").strip() if web_result.output else ""
    return ssh_cmd, web_url


def container_stats(container) -> dict:
    raw       = container.stats(stream=False)
    cpu_delta = (
        raw["cpu_stats"]["cpu_usage"]["total_usage"]
        - raw["precpu_stats"]["cpu_usage"]["total_usage"]
    )
    sys_delta = (
        raw["cpu_stats"]["system_cpu_usage"]
        - raw["precpu_stats"]["system_cpu_usage"]
    )
    num_cpus  = raw["cpu_stats"].get(
        "online_cpus",
        len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [1])),
    )
    cpu_pct   = (cpu_delta / sys_delta) * num_cpus * 100.0 if sys_delta else 0.0
    mem_usage = raw["memory_stats"].get("usage", 0)
    mem_limit = raw["memory_stats"].get("limit", 1)
    net_rx = net_tx = 0
    for iface in raw.get("networks", {}).values():
        net_rx += iface.get("rx_bytes", 0)
        net_tx += iface.get("tx_bytes", 0)

    started_at = container.attrs["State"].get("StartedAt", "")
    uptime_str = "N/A"
    if started_at and started_at != "0001-01-01T00:00:00Z":
        try:
            start  = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            delta  = datetime.datetime.now(datetime.timezone.utc) - start
            h, rem = divmod(int(delta.total_seconds()), 3600)
            m, s   = divmod(rem, 60)
            uptime_str = f"{h}h {m}m {s}s"
        except Exception:
            pass

    return {
        "cpu_pct":      round(cpu_pct, 2),
        "mem_usage_mb": round(mem_usage / 1024 / 1024, 1),
        "mem_limit_mb": round(mem_limit / 1024 / 1024, 1),
        "mem_pct":      round((mem_usage / mem_limit) * 100, 2),
        "net_rx_mb":    round(net_rx / 1024 / 1024, 2),
        "net_tx_mb":    round(net_tx / 1024 / 1024, 2),
        "uptime":       uptime_str,
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
        return conn.execute(
            "SELECT 1 FROM vps_instances WHERE vps_id=? AND user_id=?",
            (vps_id, user_id),
        ).fetchone() is not None


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
        log.info("Bot ready.")


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
        get_docker().containers.get(row["container_id"]).start()
        with db_connect() as conn:
            conn.execute("UPDATE vps_instances SET status='running' WHERE vps_id=?", (vps_id,))
        await interaction.followup.send(embed=make_embed(
            "✅ VPS Started",
            f"**{vps_id}** is running.\nRun `/regen-ssh {vps_id}` to get a fresh tmate link.",
            SUCCESS_COLOR,
        ))
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
            conn.execute(
                "UPDATE vps_instances SET status='stopped', tmate_ssh='', tmate_web='' WHERE vps_id=?",
                (vps_id,),
            )
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
            "🔄 VPS Restarted",
            f"**{vps_id}** restarted.\nRun `/regen-ssh {vps_id}` to get a fresh tmate link.",
            SUCCESS_COLOR,
        ))
    except Exception as e:
        log.error(f"restart: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="reinstall", description="Reinstall your VPS (same specs, all data wiped).")
@app_commands.describe(vps_id="Your VPS ID (e.g. BC-0001)")
async def cmd_reinstall(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=make_embed(
            "❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))

    with db_connect() as conn:
        row = conn.execute("SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()

    await interaction.followup.send(embed=make_embed(
        "⏳ Reinstalling...",
        f"Wiping **{vps_id}** and running:\n`apt update` → `apt install tmate` → `tmate`\nThis takes ~60 seconds.",
        WARN_COLOR,
    ))

    try:
        # Remove old container
        try:
            get_docker().containers.get(row["container_id"]).remove(force=True)
        except Exception:
            pass

        cpu_cores = row["cpu_quota"] / row["cpu_period"]

        # Provision fresh — runs apt update, apt install tmate, tmate
        container, ssh_cmd, web_url = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: provision_vps(vps_id, row["os_image"], row["memory_mb"], cpu_cores),
        )

        with db_connect() as conn:
            conn.execute(
                "UPDATE vps_instances SET container_id=?, tmate_ssh=?, tmate_web=?, status='running' WHERE vps_id=?",
                (container.id, ssh_cmd, web_url, vps_id),
            )

        # Send result in chat
        fields = [
            ("VPS ID", vps_id,          True),
            ("OS",     row["os_image"],  True),
            ("RAM",    f"{row['memory_mb']} MB", True),
        ]
        if ssh_cmd:
            fields.append(("🖥 SSH Command", f"```{ssh_cmd}```", False))
        if web_url:
            fields.append(("🌐 Web Terminal", web_url, False))

        await interaction.followup.send(
            content=f"✅ **{vps_id}** reinstalled, {interaction.user.mention}!",
            embed=make_embed("🔄 VPS Reinstalled", "", SUCCESS_COLOR, fields=fields),
        )

    except Exception as e:
        log.error(f"reinstall: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="regen-ssh", description="Get a fresh tmate session for your VPS.")
@app_commands.describe(vps_id="Your VPS ID (e.g. BC-0001)")
async def cmd_regen_ssh(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=make_embed(
            "❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))
    try:
        with db_connect() as conn:
            row = conn.execute("SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
        if row["status"] != "running":
            return await interaction.followup.send(embed=make_embed(
                "⚠️ Not Running", f"Start **{vps_id}** first with `/start {vps_id}`.", WARN_COLOR))

        container = get_docker().containers.get(row["container_id"])

        ssh_cmd, web_url = await asyncio.get_event_loop().run_in_executor(
            None, lambda: regen_tmate(container)
        )

        if not ssh_cmd:
            return await interaction.followup.send(embed=make_embed(
                "⚠️ Not Ready", "tmate is still starting. Try again in 15 seconds.", WARN_COLOR))

        with db_connect() as conn:
            conn.execute(
                "UPDATE vps_instances SET tmate_ssh=?, tmate_web=? WHERE vps_id=?",
                (ssh_cmd, web_url, vps_id),
            )

        fields = [("🖥 SSH Command", f"```{ssh_cmd}```", False)]
        if web_url:
            fields.append(("🌐 Web Terminal", web_url, False))

        await interaction.followup.send(embed=make_embed(
            f"🖥 tmate Session — {vps_id}",
            "⚠️ Keep these private — anyone with them can access your terminal.",
            SUCCESS_COLOR,
            fields=fields,
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
                f"Container is **{status}** — start it first.",
                WARN_COLOR,
                fields=[("VPS ID", vps_id, True), ("Status", status.capitalize(), True)],
            ))
        stats = container_stats(container)
        disk_result = container.exec_run("df -BG / --output=used | tail -1", tty=False)
        disk_used = disk_result.output.decode().strip().replace("G", "") if disk_result.exit_code == 0 else "N/A"
        await interaction.followup.send(embed=make_embed(
            "📊 VPS Performance", "", INFO_COLOR,
            fields=[
                ("🆔 VPS ID",  vps_id,                                                                  True),
                ("📌 Status",  status.capitalize(),                                                      True),
                ("🖥 OS",      row["os_image"],                                                          True),
                ("💻 CPU",     f"{stats['cpu_pct']}%",                                                  True),
                ("🧠 RAM",     f"{stats['mem_usage_mb']} / {stats['mem_limit_mb']} MB ({stats['mem_pct']}%)", True),
                ("💾 Disk",    f"{disk_used} GB / {row['disk_gb']} GB",                                 True),
                ("⏱ Uptime",  stats["uptime"],                                                          True),
                ("🌐 Net RX",  f"{stats['net_rx_mb']} MB",                                              True),
                ("🌐 Net TX",  f"{stats['net_tx_mb']} MB",                                              True),
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
        (r["vps_id"], f"OS: `{r['os_image']}` | RAM: `{r['memory_mb']}MB` | Status: `{r['status']}`", False)
        for r in rows
    ]
    await interaction.followup.send(embed=make_embed(
        f"📋 My VPS ({len(rows)} instance{'s' if len(rows) != 1 else ''})",
        "", INFO_COLOR, fields=fields,
    ))


# ══════════════════════════════════════════════
# ADMIN COMMANDS
# ══════════════════════════════════════════════

@bot.tree.command(name="admin-add-user", description="[Admin] Grant a user hosting access.")
@app_commands.describe(user="Discord user to grant access")
async def cmd_admin_add_user(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))
    with db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO allowed_users (user_id, added_by) VALUES (?, ?)",
            (user.id, interaction.user.id),
        )
    await interaction.followup.send(embed=make_embed(
        "✅ User Added", f"{user.mention} can now access hosting services.", SUCCESS_COLOR))


@bot.tree.command(name="admin-remove-user", description="[Admin] Revoke a user's hosting access.")
@app_commands.describe(user="Discord user to revoke")
async def cmd_admin_remove_user(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))
    with db_connect() as conn:
        conn.execute("DELETE FROM allowed_users WHERE user_id=?", (user.id,))
    await interaction.followup.send(embed=make_embed(
        "🗑 User Removed", f"{user.mention}'s hosting access has been revoked.", WARN_COLOR))


@bot.tree.command(name="create", description="[Admin] Create a new VPS for a user.")
@app_commands.describe(
    user="Target Discord user",
    memory="RAM in MB (e.g. 512)",
    cpu="CPU cores (e.g. 1.0)",
    disk="Disk size in GB (recorded)",
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
    await interaction.response.defer()   # public defer — result shows in channel

    if not is_admin(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR), ephemeral=True)

    os_lower = os.lower()
    if os_lower not in OS_MAP:
        return await interaction.followup.send(embed=make_embed(
            "❌ Invalid OS",
            f"Supported templates: {', '.join(OS_MAP.keys())}",
            ERROR_COLOR,
        ), ephemeral=True)

    image  = OS_MAP[os_lower]
    vps_id = next_vps_id()

    # ── Progress message ────────────────────────────────────────────
    await interaction.followup.send(embed=make_embed(
        "⏳ Provisioning VPS...",
        f"Setting up **{vps_id}** for {user.mention}\n\n"
        f"```\n"
        f"[1/3] apt update         ⏳\n"
        f"[2/3] apt install tmate  ⏳\n"
        f"[3/3] tmate              ⏳\n"
        f"```\n"
        f"Please wait ~60 seconds...",
        INFO_COLOR,
        fields=[
            ("OS",  image,          True),
            ("RAM", f"{memory} MB", True),
            ("CPU", f"{cpu} core(s)", True),
        ],
    ))

    try:
        # Run provision_vps in a thread (blocking docker + apt calls)
        container, ssh_cmd, web_url = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: provision_vps(vps_id, image, memory, cpu),
        )
    except Exception as e:
        log.error(f"create provision: {e}")
        return await interaction.followup.send(embed=make_embed("❌ Provision Error", str(e), ERROR_COLOR))

    cpu_period = 100_000
    cpu_quota  = int(cpu_period * cpu)

    with db_connect() as conn:
        conn.execute("""
            INSERT INTO vps_instances
              (vps_id, user_id, container_id, os_image, memory_mb, cpu_period, cpu_quota, disk_gb, tmate_ssh, tmate_web, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
        """, (vps_id, user.id, container.id, image, memory, cpu_period, cpu_quota, disk, ssh_cmd, web_url))

    log.info(f"{interaction.user} created VPS {vps_id} for {user}. SSH: {ssh_cmd}")

    # ── Final result — sent in channel so user can see it ───────────
    fields = [
        ("👤 Owner",  user.mention,   True),
        ("🆔 VPS ID", vps_id,         True),
        ("🖥 OS",     image,           True),
        ("🧠 RAM",    f"{memory} MB",  True),
        ("💻 CPU",    f"{cpu} core(s)", True),
        ("💾 Disk",   f"{disk} GB",    True),
    ]

    if ssh_cmd:
        fields.append(("🖥 SSH Command", f"```{ssh_cmd}```", False))
    else:
        fields.append(("🖥 SSH Command", "Not ready yet — run `/regen-ssh` in 30 seconds.", False))

    if web_url:
        fields.append(("🌐 Web Terminal", web_url, False))

    await interaction.followup.send(
        content=f"✅ VPS ready for {user.mention}!",
        embed=make_embed(
            "🎉 VPS Created — Blined Cloud",
            "Connect using the SSH command below. **Keep it private.**",
            SUCCESS_COLOR,
            fields=fields,
        ),
    )


@bot.tree.command(name="suspend-vps", description="[Admin] Suspend a VPS instance.")
@app_commands.describe(vps_id="VPS ID to suspend (e.g. BC-0001)")
async def cmd_suspend_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
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
        conn.execute(
            "UPDATE vps_instances SET status='suspended', tmate_ssh='', tmate_web='' WHERE vps_id=?",
            (vps_id,),
        )
    await interaction.followup.send(embed=make_embed(
        "⛔ VPS Suspended", f"**{vps_id}** has been suspended.", WARN_COLOR))


@bot.tree.command(name="unsuspend-vps", description="[Admin] Reactivate a suspended VPS.")
@app_commands.describe(vps_id="VPS ID to unsuspend (e.g. BC-0001)")
async def cmd_unsuspend_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
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
            "✅ VPS Unsuspended",
            f"**{vps_id}** is active. User can run `/regen-ssh {vps_id}` for a new tmate link.",
            SUCCESS_COLOR,
        ))
    except Exception as e:
        log.error(f"unsuspend: {e}")
        await interaction.followup.send(embed=make_embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="remove-vps", description="[Admin] Permanently delete a VPS instance.")
@app_commands.describe(vps_id="VPS ID to remove (e.g. BC-0001)")
async def cmd_remove_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
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
    with db_connect() as conn:
        conn.execute("DELETE FROM vps_instances WHERE vps_id=?", (vps_id,))
    await interaction.followup.send(embed=make_embed(
        "🗑 VPS Removed", f"**{vps_id}** has been permanently deleted.", WARN_COLOR))


@bot.tree.command(name="list-vps", description="[Admin] List all VPS instances on the node.")
async def cmd_list_vps(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM vps_instances ORDER BY vps_id").fetchall()
    if not rows:
        return await interaction.followup.send(embed=make_embed(
            "📋 All VPS", "No VPS instances found.", WARN_COLOR))
    fields = [
        (
            r["vps_id"],
            f"User: <@{r['user_id']}> | OS: `{r['os_image']}` | RAM: `{r['memory_mb']}MB` | Status: `{r['status']}`",
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
    if not is_admin(interaction):
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
            ("🖥 Host CPU",           f"{cpu_pct}%",                                                                     True),
            ("🧠 Host RAM",           f"{round(mem.used/1024**3,2)} / {round(mem.total/1024**3,2)} GB ({mem.percent}%)",  True),
            ("💾 Host Disk",          f"{bytes_to_gb(disk.used)} / {bytes_to_gb(disk.total)} GB ({disk.percent}%)",       True),
            ("🐳 Running Containers", str(running),                                                                       True),
            ("📦 Total Containers",   str(total),                                                                         True),
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
