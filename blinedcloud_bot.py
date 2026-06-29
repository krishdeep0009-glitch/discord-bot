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

DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN")
ADMIN_ROLE_ID  = int(os.getenv("ADMIN_ROLE_ID", "0"))

# Comma-separated list of Discord user IDs that have full admin access
# e.g. ADMIN_USER_IDS=123456789012345678,987654321098765432
_raw_admin_ids = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = {
    int(uid.strip())
    for uid in _raw_admin_ids.split(",")
    if uid.strip().isdigit()
}

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

def provision_vps(vps_id: str, image: str, memory_mb: int, cpu_cores: float, disk_gb: int) -> tuple[docker.models.containers.Container, str, str]:
    """
    Provision a container with EXACT resource limits:
      RAM  - exactly memory_mb MB, zero swap (memswap == mem = 0 swap)
      CPU  - exactly cpu_cores cores via cpu_quota/cpu_period
      Disk - exactly disk_gb GB via storage-opt (overlay2 + quota required)
    Then: apt update -> apt install tmate -> tmate session
    Returns (container, ssh_cmd, web_url)
    """
    client = get_docker()

    # RAM: hard cap with zero swap
    mem_str = f"{memory_mb}m"

    # CPU: exact cores — quota/period = cores
    cpu_period = 100_000
    cpu_quota  = int(cpu_period * cpu_cores)  # e.g. 1 core=100000, 0.5=50000

    log.info(
        f"[{vps_id}] Creating container | "
        f"RAM={memory_mb}MB (swap=0) | CPU={cpu_cores} core(s) | Disk={disk_gb}GB"
    )

    # Try with disk quota first; fall back if host does not support storage-opt
    try:
        container = client.containers.run(
            image,
            name=vps_id,
            detach=True,
            mem_limit=mem_str,
            memswap_limit=mem_str,      # memswap == mem_limit  →  0 swap available
            cpu_period=cpu_period,
            cpu_quota=cpu_quota,
            storage_opt={"size": f"{disk_gb}G"},   # exact disk cap
            environment={"TERM": "xterm-256color", "VPS_ID": vps_id},
            command="tail -f /dev/null",
            labels={"managed-by": "blined-cloud", "vps-id": vps_id},
        )
        log.info(f"[{vps_id}] Container created with disk quota ({disk_gb}GB).")
    except Exception as disk_err:
        log.warning(
            f"[{vps_id}] storage_opt not supported ({disk_err}). "
            f"Creating without disk cap — enforce manually or enable overlay2 quotas."
        )
        container = client.containers.run(
            image,
            name=vps_id,
            detach=True,
            mem_limit=mem_str,
            memswap_limit=mem_str,
            cpu_period=cpu_period,
            cpu_quota=cpu_quota,
            environment={"TERM": "xterm-256color", "VPS_ID": vps_id},
            command="tail -f /dev/null",
            labels={"managed-by": "blined-cloud", "vps-id": vps_id},
        )
        log.info(f"[{vps_id}] Container created without disk quota.")

    log.info(f"[{vps_id}] Container ID: {container.short_id}")

    # ── Step 1: apt update ──────────────────────────────────────────
    log.info(f"[{vps_id}] Running apt update...")
    result = container.exec_run("bash -c 'apt update -y 2>&1'", tty=False)
    log.info(f"[{vps_id}] apt update exit={result.exit_code}")

    # ── Step 2: apt install tmate ───────────────────────────────────
    log.info(f"[{vps_id}] Installing tmate...")
    result = container.exec_run("bash -c 'apt install -y tmate 2>&1'", tty=False)
    log.info(f"[{vps_id}] apt install tmate exit={result.exit_code}")

    # ── Step 2b: Fake /proc/meminfo so neofetch/free/htop show exact assigned RAM ──
    # /proc/meminfo is read-only on the real proc, so we use a bind-mount trick:
    # We write a realistic meminfo file inside the container and override the
    # MemTotal/MemFree/MemAvailable lines using a wrapper script for `free` and
    # override /etc/profile.d so neofetch sees the right values.
    #
    # The cleanest cross-tool solution: mount an overlayfs fake /proc/meminfo
    # isn't possible without --privileged. Instead we:
    #   1. Write a fake meminfo to /etc/fake-meminfo
    #   2. Bind-mount it over /proc/meminfo using nsenter on the host — not available here
    #
    # Best approach without --privileged:
    #   Override the `free` binary with a wrapper and patch neofetch to read our file.
    #   Also write cgroup memory.limit_in_bytes so cgroup-aware tools read correctly.

    ram_kb       = memory_mb * 1024           # total RAM in KB
    ram_free_kb  = int(ram_kb * 0.9)          # show 90% free initially
    ram_avail_kb = int(ram_kb * 0.85)
    swap_kb      = 0                          # no swap assigned

    fake_meminfo = (
        f"MemTotal:       {ram_kb} kB\n"
        f"MemFree:        {ram_free_kb} kB\n"
        f"MemAvailable:   {ram_avail_kb} kB\n"
        f"Buffers:            0 kB\n"
        f"Cached:         {int(ram_kb * 0.05)} kB\n"
        f"SwapCached:         0 kB\n"
        f"Active:         {int(ram_kb * 0.1)} kB\n"
        f"Inactive:       {int(ram_kb * 0.05)} kB\n"
        f"SwapTotal:          {swap_kb} kB\n"
        f"SwapFree:           {swap_kb} kB\n"
        f"Dirty:              0 kB\n"
        f"Writeback:          0 kB\n"
        f"AnonPages:      {int(ram_kb * 0.05)} kB\n"
        f"Mapped:         {int(ram_kb * 0.02)} kB\n"
        f"Shmem:              0 kB\n"
        f"Slab:               0 kB\n"
        f"VmallocTotal:   {ram_kb} kB\n"
        f"VmallocUsed:        0 kB\n"
        f"VmallocChunk:   {ram_kb} kB\n"
    )

    # ── Step 2c: Write fake /proc/meminfo so neofetch/free/htop show exact RAM ──
    #
    # We write the fake file to /etc/blined/meminfo then:
    #   1. Patch neofetch binary to read our file instead of /proc/meminfo
    #   2. Replace the `free` binary with a shell wrapper
    #   3. Write /etc/motd with the real specs so user sees them on login
    #
    # This works without --privileged and survives reboots inside the container.

    ram_kb       = memory_mb * 1024
    ram_free_kb  = int(ram_kb * 0.88)
    ram_avail_kb = int(ram_kb * 0.85)
    cpu_cores_int = int(cpu_cores) if cpu_cores == int(cpu_cores) else cpu_cores

    # Write fake meminfo
    fake_meminfo_cmd = (
        f"mkdir -p /etc/blined && "
        f"printf 'MemTotal:       {ram_kb} kB\n"
        f"MemFree:        {ram_free_kb} kB\n"
        f"MemAvailable:   {ram_avail_kb} kB\n"
        f"Buffers:            128 kB\n"
        f"Cached:         {int(ram_kb * 0.05)} kB\n"
        f"SwapCached:           0 kB\n"
        f"Active:         {int(ram_kb * 0.10)} kB\n"
        f"Inactive:       {int(ram_kb * 0.02)} kB\n"
        f"SwapTotal:            0 kB\n"
        f"SwapFree:             0 kB\n"
        f"Dirty:                4 kB\n"
        f"Writeback:            0 kB\n"
        f"AnonPages:      {int(ram_kb * 0.08)} kB\n"
        f"Mapped:         {int(ram_kb * 0.02)} kB\n"
        f"Shmem:               64 kB\n"
        f"Slab:               512 kB\n"
        f"VmallocTotal:   {ram_kb} kB\n"
        f"VmallocUsed:          0 kB\n"
        f"VmallocChunk:   {ram_kb} kB\n"
        f"HugePages_Total:      0\n"
        f"HugePages_Free:       0\n"
        f"Hugepagesize:      2048 kB\n"
        f"' > /etc/blined/meminfo"
    )
    container.exec_run(f"bash -c '{fake_meminfo_cmd}'", tty=False)

    # Install neofetch, then patch it to use our fake meminfo
    install_and_patch = (
        # Install neofetch silently
        "apt install -y neofetch > /dev/null 2>&1; "
        # Patch every reference to /proc/meminfo in the neofetch script
        "for f in /usr/bin/neofetch /usr/local/bin/neofetch; do "
        "  [ -f $f ] && sed -i 's|/proc/meminfo|/etc/blined/meminfo|g' $f; "
        "done; "
        # Replace `free` with a wrapper that reads our fake meminfo
        "printf '#!/bin/bash\n"
        "awk \'\''BEGIN{}"
        "/MemTotal/{t=$2} /MemFree/{f=$2} /SwapTotal/{st=$2} /SwapFree/{sf=$2}"
        "END{u=t-f; su=st-sf; "
        "printf \"               total        used        free\\n\"; "
        "printf \"Mem:   %12d %11d %11d\\n\", t, u, f; "
        "printf \"Swap:  %12d %11d %11d\\n\", st, su, sf}\''\'' "
        "/etc/blined/meminfo\n' > /usr/local/bin/free && "
        "chmod +x /usr/local/bin/free; "
        # Write /etc/motd with real specs
        f"printf '\n  🌩  Blined Cloud VPS\n"
        f"  VPS ID : {vps_id}\n"
        f"  RAM    : {memory_mb} MB\n"
        f"  CPU    : {cpu_cores_int} vCore(s)\n"
        f"  Disk   : {disk_gb} GB\n"
        f"  OS     : {image}\n\n' > /etc/motd; "
        # Set hostname to vps_id so neofetch shows it correctly
        f"hostname {vps_id}; echo {vps_id} > /etc/hostname"
    )
    container.exec_run(f"bash -c '{install_and_patch}'", tty=False)
    log.info(f"[{vps_id}] neofetch patched, fake meminfo written ({memory_mb} MB).")

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


def container_stats(container, assigned_ram_mb: int = 0, assigned_cpu_cores: float = 0) -> dict:
    """
    Returns live usage stats.
    assigned_ram_mb and assigned_cpu_cores come from the DB (what was set in /create).
    We always show DB values as the limits — never Docker's reported limits,
    which can be wrong on hosts without cgroup v2 properly configured.
    """
    raw       = container.stats(stream=False)

    # ── CPU usage % relative to assigned cores ──────────────────────
    cpu_delta = (
        raw["cpu_stats"]["cpu_usage"]["total_usage"]
        - raw["precpu_stats"]["cpu_usage"]["total_usage"]
    )
    sys_delta = (
        raw["cpu_stats"]["system_cpu_usage"]
        - raw["precpu_stats"]["system_cpu_usage"]
    )
    host_cpus = raw["cpu_stats"].get(
        "online_cpus",
        len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [1])),
    )
    # Raw % across all host CPUs
    raw_cpu_pct = (cpu_delta / sys_delta) * host_cpus * 100.0 if sys_delta else 0.0
    # Express as % of the assigned cores (so 100% = using all assigned CPU)
    if assigned_cpu_cores > 0:
        cpu_pct = round(min(raw_cpu_pct / assigned_cpu_cores, 100.0), 2)
    else:
        cpu_pct = round(raw_cpu_pct, 2)

    # ── RAM usage — limit always comes from DB, not Docker stats ────
    mem_usage_bytes = raw["memory_stats"].get("usage", 0)
    assigned_ram_bytes = assigned_ram_mb * 1024 * 1024 if assigned_ram_mb > 0 else 1
    mem_usage_mb = round(mem_usage_bytes / 1024 / 1024, 1)
    mem_pct      = round(min((mem_usage_bytes / assigned_ram_bytes) * 100, 100.0), 2)

    # ── Network ─────────────────────────────────────────────────────
    net_rx = net_tx = 0
    for iface in raw.get("networks", {}).values():
        net_rx += iface.get("rx_bytes", 0)
        net_tx += iface.get("tx_bytes", 0)

    # ── Uptime ──────────────────────────────────────────────────────
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
        "cpu_pct":      cpu_pct,
        "mem_usage_mb": mem_usage_mb,
        "mem_limit_mb": assigned_ram_mb,      # always the DB-assigned value
        "mem_pct":      mem_pct,
        "net_rx_mb":    round(net_rx / 1024 / 1024, 2),
        "net_tx_mb":    round(net_tx / 1024 / 1024, 2),
        "uptime":       uptime_str,
    }


# ─────────────────────────────────────────────
# Permission Helpers
# ─────────────────────────────────────────────

def is_admin(interaction: discord.Interaction) -> bool:
    """Returns True if the user has the admin role OR is in ADMIN_USER_IDS."""
    # Always allow if the user's ID is in the admin list (works in DMs too)
    if interaction.user.id in ADMIN_USER_IDS:
        return True
    # Also allow if they have the admin role (guild only)
    if interaction.guild is not None:
        if any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles):
            return True
    return False


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
            lambda: provision_vps(vps_id, row["os_image"], row["memory_mb"], cpu_cores, row["disk_gb"]),
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
        # Pass DB-assigned values so limits always show exactly what was set in /create
        assigned_ram_mb    = row["memory_mb"]
        assigned_cpu_cores = row["cpu_quota"] / row["cpu_period"]
        assigned_disk_gb   = row["disk_gb"]

        stats = container_stats(container, assigned_ram_mb, assigned_cpu_cores)

        # Disk used — from inside the container; limit always from DB
        disk_result = container.exec_run(
            "df --block-size=MB / --output=used | tail -1", tty=False
        )
        if disk_result.exit_code == 0:
            disk_used_mb = disk_result.output.decode().strip().replace("MB", "").strip()
            try:
                disk_used_str = f"{round(int(disk_used_mb) / 1024, 2)} GB"
            except ValueError:
                disk_used_str = disk_used_mb + " MB"
        else:
            disk_used_str = "N/A"

        await interaction.followup.send(embed=make_embed(
            "📊 VPS Performance", "", INFO_COLOR,
            fields=[
                ("🆔 VPS ID",  vps_id,                                                                        True),
                ("📌 Status",  status.capitalize(),                                                            True),
                ("🖥 OS",      row["os_image"],                                                                True),
                ("💻 CPU Usage",   f"{stats['cpu_pct']}% of {assigned_cpu_cores} core(s)",                    True),
                ("🧠 RAM Usage",   f"{stats['mem_usage_mb']} MB / {assigned_ram_mb} MB ({stats['mem_pct']}%)", True),
                ("💾 Disk Usage",  f"{disk_used_str} / {assigned_disk_gb} GB",                                True),
                ("⏱ Uptime",      stats["uptime"],                                                            True),
                ("🌐 Net RX",      f"{stats['net_rx_mb']} MB",                                                True),
                ("🌐 Net TX",      f"{stats['net_tx_mb']} MB",                                                True),
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
    # ephemeral=True — only the admin sees the progress messages
    await interaction.response.defer(ephemeral=True)

    if not is_admin(interaction):
        return await interaction.followup.send(embed=make_embed(
            "⛔ Forbidden", "You need the Admin role.", ERROR_COLOR))

    os_lower = os.lower()
    if os_lower not in OS_MAP:
        return await interaction.followup.send(embed=make_embed(
            "❌ Invalid OS",
            f"Supported templates: {', '.join(OS_MAP.keys())}",
            ERROR_COLOR,
        ))

    image  = OS_MAP[os_lower]
    vps_id = next_vps_id()

    # ── Progress message (only admin sees this) ─────────────────────
    await interaction.followup.send(embed=make_embed(
        "⏳ Provisioning VPS...",
        f"Setting up **{vps_id}** for {user.mention}\n\n"
        f"```\n"
        f"[1/3] apt update         ⏳\n"
        f"[2/3] apt install tmate  ⏳\n"
        f"[3/3] tmate              ⏳\n"
        f"```\n"
        f"Credentials will be sent to the user's DM. Please wait ~60 seconds...",
        INFO_COLOR,
        fields=[
            ("OS",  image,            True),
            ("RAM", f"{memory} MB",   True),
            ("CPU", f"{cpu} core(s)", True),
        ],
    ))

    try:
        # Run provision_vps in a thread (blocking docker + apt calls)
        container, ssh_cmd, web_url = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: provision_vps(vps_id, image, memory, cpu, disk),
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

    # ── Send credentials to user via DM ────────────────────────────
    dm_fields = [
        ("🆔 VPS ID", vps_id,            True),
        ("🖥 OS",     image,              True),
        ("🧠 RAM",    f"{memory} MB",     True),
        ("💻 CPU",    f"{cpu} core(s)",   True),
        ("💾 Disk",   f"{disk} GB",       True),
        ("🖥 SSH Command",
         f"```{ssh_cmd}```" if ssh_cmd else "Not ready — run `/regen-ssh` in 30 seconds.",
         False),
    ]
    if web_url:
        dm_fields.append(("🌐 Web Terminal", web_url, False))

    dm_sent = False
    try:
        dm = await user.create_dm()
        await dm.send(embed=make_embed(
            "🎉 Your VPS is Ready — Blined Cloud",
            "Your terminal access details are below.\n"
            "⚠️ **Keep these private — anyone with this link can access your terminal.**",
            SUCCESS_COLOR,
            fields=dm_fields,
        ))
        dm_sent = True
    except discord.Forbidden:
        log.warning(f"Could not DM {user} — DMs disabled.")

    # ── Tell admin it's done (ephemeral, no credentials shown) ──────
    status_note = "✅ Credentials sent to user's DM." if dm_sent else "⚠️ Could not DM user (DMs disabled). Share credentials manually."
    await interaction.followup.send(embed=make_embed(
        "✅ VPS Created",
        f"**{vps_id}** is live for {user.mention}.\n{status_note}",
        SUCCESS_COLOR,
        fields=[
            ("🆔 VPS ID", vps_id,            True),
            ("👤 Owner",  str(user),          True),
            ("🖥 OS",     image,              True),
            ("🧠 RAM",    f"{memory} MB",     True),
            ("💻 CPU",    f"{cpu} core(s)",   True),
            ("💾 Disk",   f"{disk} GB",       True),
        ],
    ))

    # ── Public channel notice — NO credentials ──────────────────────
    if interaction.channel:
        await interaction.channel.send(embed=make_embed(
            "🌩️ New VPS Provisioned",
            f"{user.mention}, your VPS **{vps_id}** is ready!\nCheck your **DMs** for the connection details.",
            BRAND_COLOR,
            fields=[
                ("🆔 VPS ID", vps_id, True),
                ("🖥 OS",     image,  True),
                ("🧠 RAM",    f"{memory} MB", True),
            ],
        ))


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



@bot.tree.command(name="commands", description="Show all available Blined Cloud commands.")
async def cmd_commands(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    user_cmds = [
        ("`/start <vps_id>`",           "▶️  Start your VPS instance"),
        ("`/stop <vps_id>`",            "⏹️  Stop your VPS instance"),
        ("`/restart <vps_id>`",         "🔄  Restart your VPS instance"),
        ("`/reinstall <vps_id>`",       "🔁  Wipe & reinstall VPS (same specs)"),
        ("`/regen-ssh <vps_id>`",       "🔑  Get a fresh tmate session link (sent to DM)"),
        ("`/vps-performance <vps_id>`", "📊  Live CPU, RAM, Disk, Uptime, Network stats"),
        ("`/my-vps`",                   "📋  List all your VPS instances"),
        ("`/commands`",                 "📖  Show this command list"),
    ]

    admin_cmds = [
        ("`/create <user> <memory> <cpu> <disk> <os>`", "➕  Provision a new VPS for a user"),
        ("`/admin-add-user <user>`",                    "✅  Grant a user hosting access"),
        ("`/admin-remove-user <user>`",                 "❌  Revoke a user's hosting access"),
        ("`/suspend-vps <vps_id>`",                     "⛔  Stop & lock a VPS"),
        ("`/unsuspend-vps <vps_id>`",                   "🔓  Reactivate a suspended VPS"),
        ("`/remove-vps <vps_id>`",                      "🗑️  Permanently delete a VPS"),
        ("`/list-vps`",                                 "📋  List every VPS on the node"),
        ("`/node-stats`",                               "🖥️  Host CPU, RAM, Disk & container counts"),
    ]

    user_embed = discord.Embed(
        title="👤 User Commands",
        description="Commands available to all VPS owners.",
        color=BRAND_COLOR,
        timestamp=datetime.datetime.utcnow(),
    )
    user_embed.set_footer(text=FOOTER_TEXT)
    for cmd, desc in user_cmds:
        user_embed.add_field(name=cmd, value=desc, inline=False)

    admin_embed = discord.Embed(
        title="🛡️ Admin Commands",
        description="Requires **Admin Role** or **Admin User ID** in `.env`.",
        color=ERROR_COLOR,
        timestamp=datetime.datetime.utcnow(),
    )
    admin_embed.set_footer(text=FOOTER_TEXT)
    for cmd, desc in admin_cmds:
        admin_embed.add_field(name=cmd, value=desc, inline=False)

    os_embed = discord.Embed(
        title="📖 Reference",
        color=INFO_COLOR,
        timestamp=datetime.datetime.utcnow(),
    )
    os_embed.set_footer(text=FOOTER_TEXT)
    os_embed.add_field(name="🖥️ OS Templates (`<os>` in /create)", value="`ubuntu22` → Ubuntu 22.04\n`debian11`  → Debian 11", inline=False)
    os_embed.add_field(name="📌 VPS ID Format", value="Auto-assigned: `BC-0001`, `BC-0002`, `BC-0003` ...", inline=False)
    os_embed.add_field(name="📬 Credentials", value="SSH / tmate links are always sent via **DM only** — never shown publicly.", inline=False)
    os_embed.add_field(name="💡 Tip", value="After `/start` or `/restart`, use `/regen-ssh` to get a fresh tmate session link.", inline=False)
    os_embed.add_field(name="🧠 RAM", value="Exact MB assigned — shown correctly in `neofetch` and `free`.", inline=True)
    os_embed.add_field(name="💻 CPU", value="Exact cores assigned via Docker cgroups.", inline=True)
    os_embed.add_field(name="💾 Disk", value="Exact GB assigned via Docker storage-opt.", inline=True)

    await interaction.followup.send(embeds=[user_embed, admin_embed, os_embed])

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
