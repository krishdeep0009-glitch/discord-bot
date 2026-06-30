"""
Blined Cloud VPS Manager - Discord Bot
- LXC-style containers (systemd as PID 1, not tail -f /dev/null)
- tmate for terminal access
- Fake /proc/meminfo + /proc/cpuinfo via bind-mount (--privileged)
- Dropdown choices for OS and CPU type
- Credentials sent via DM only
"""

import os
import io
import time
import tarfile
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

_raw_ids       = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = {int(x.strip()) for x in _raw_ids.split(",") if x.strip().isdigit()}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("blinedcloud.log"), logging.StreamHandler()],
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
FOOTER_TEXT   = "Powered by Blined Cloud"

# OS templates — value sent to Docker, key shown in the dropdown
OS_MAP = {
    "ubuntu20": "ubuntu:20.04",
    "ubuntu22": "ubuntu:22.04",
    "ubuntu24": "ubuntu:24.04",
    "debian11": "debian:11",
    "debian12": "debian:12",
}

# CPU display names — shown in neofetch / /proc/cpuinfo via bind-mount
CPU_TYPES = {
    "ryzen9":  "AMD Ryzen 9 9950X 16-Core Processor",
    "xeon":    "Intel(R) Xeon(R) Platinum 8480+ @ 3.80GHz",
}

DB_PATH = "blinedcloud.db"

# ─────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db_connect() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id  INTEGER PRIMARY KEY,
                added_by INTEGER,
                added_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS vps_instances (
                vps_id       TEXT PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                container_id TEXT,
                os_image     TEXT,
                memory_mb    INTEGER,
                cpu_cores    REAL,
                disk_gb      INTEGER,
                cpu_name     TEXT DEFAULT 'Blined Cloud Virtual CPU',
                tmate_ssh    TEXT DEFAULT '',
                tmate_web    TEXT DEFAULT '',
                status       TEXT DEFAULT 'running',
                created_at   TEXT DEFAULT (datetime('now'))
            );
        """)
    log.info("Database ready.")

# ─────────────────────────────────────────────
# Embed factory
# ─────────────────────────────────────────────

def embed(title, desc="", color=BRAND_COLOR, fields=None):
    e = discord.Embed(title=title, description=desc, color=color,
                      timestamp=datetime.datetime.utcnow())
    e.set_footer(text=FOOTER_TEXT)
    for name, val, inline in (fields or []):
        e.add_field(name=name, value=val, inline=inline)
    return e

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def next_vps_id():
    with db_connect() as c:
        row = c.execute("SELECT vps_id FROM vps_instances ORDER BY vps_id DESC LIMIT 1").fetchone()
    return "BC-0001" if not row else f"BC-{int(row['vps_id'].split('-')[1])+1:04d}"

def bytes_to_gb(b): return round(b / 1024**3, 2)
def get_docker():    return docker.from_env()

def is_admin(interaction):
    if interaction.user.id in ADMIN_USER_IDS:
        return True
    if interaction.guild:
        return any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles)
    return False

def owns_vps(user_id, vps_id):
    with db_connect() as c:
        return c.execute(
            "SELECT 1 FROM vps_instances WHERE vps_id=? AND user_id=?",
            (vps_id, user_id)
        ).fetchone() is not None

# ─────────────────────────────────────────────
# Write file into container via tar stream
# ─────────────────────────────────────────────

def container_put(container, dest_path: str, content: str):
    """Write a string as a file inside the container using put_archive."""
    data = content.encode()
    buf  = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info      = tarfile.TarInfo(name=os.path.basename(dest_path))
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    container.put_archive(os.path.dirname(dest_path) or "/", buf)

# ─────────────────────────────────────────────
# Fake /proc/meminfo and /proc/cpuinfo content
# ─────────────────────────────────────────────

def make_fake_meminfo(memory_mb: int) -> str:
    kb        = memory_mb * 1024
    free_kb   = int(kb * 0.88)
    avail_kb  = int(kb * 0.85)
    return "\n".join([
        f"MemTotal:       {kb} kB",
        f"MemFree:        {free_kb} kB",
        f"MemAvailable:   {avail_kb} kB",
        f"Buffers:            128 kB",
        f"Cached:         {int(kb * 0.05)} kB",
        f"SwapCached:           0 kB",
        f"Active:         {int(kb * 0.10)} kB",
        f"Inactive:       {int(kb * 0.02)} kB",
        f"SwapTotal:            0 kB",
        f"SwapFree:             0 kB",
        f"Dirty:                4 kB",
        f"Writeback:            0 kB",
        f"AnonPages:      {int(kb * 0.08)} kB",
        f"Mapped:         {int(kb * 0.02)} kB",
        f"Shmem:               64 kB",
        f"Slab:               512 kB",
        f"VmallocTotal:   {kb} kB",
        f"VmallocUsed:          0 kB",
        f"VmallocChunk:   {kb} kB",
        f"HugePages_Total:      0",
        f"HugePages_Free:       0",
        f"Hugepagesize:      2048 kB",
        "",
    ])

def make_fake_cpuinfo(cpu_cores: float, cpu_name: str) -> str:
    n      = max(1, int(cpu_cores))
    blocks = []
    for i in range(n):
        blocks.append("\n".join([
            f"processor\t: {i}",
            f"vendor_id\t: {'AuthenticAMD' if 'AMD' in cpu_name or 'Ryzen' in cpu_name else 'GenuineIntel'}",
            f"cpu family\t: 25",
            f"model\t\t: 97",
            f"model name\t: {cpu_name}",
            f"stepping\t: 2",
            f"cpu MHz\t\t: 4200.000",
            f"cache size\t: 65536 KB",
            f"physical id\t: 0",
            f"siblings\t: {n}",
            f"core id\t\t: {i}",
            f"cpu cores\t: {n}",
            f"fpu\t\t: yes",
            f"fpu_exception\t: yes",
            f"cpuid level\t: 16",
            f"wp\t\t: yes",
            f"bogomips\t: 8400.00",
            f"clflush size\t: 64",
            f"cache_alignment\t: 64",
            f"address sizes\t: 48 bits physical, 48 bits virtual",
            "",
        ]))
    return "\n".join(blocks)

# ─────────────────────────────────────────────
# Core provisioning — LXC-style container with systemd as init
# ─────────────────────────────────────────────

def provision_vps(vps_id: str, image: str, memory_mb: int,
                  cpu_cores: float, disk_gb: int,
                  cpu_name: str = "Blined Cloud Virtual CPU") -> tuple:
    """
    Creates an LXC-style container:
      - systemd as PID 1 (real init system, not tail -f /dev/null)
      - --privileged + cgroup mount for systemd to work inside Docker
      - Exact RAM / CPU / Disk limits
      - apt update -> apt install tmate neofetch
      - Bind-mount fake /proc/meminfo + /proc/cpuinfo
      - Start tmate, return (container, ssh_cmd, web_url)
    """
    client = get_docker()

    cpu_period = 100_000
    cpu_quota  = int(cpu_period * cpu_cores)
    mem_str    = f"{memory_mb}m"

    log.info(f"[{vps_id}] Creating LXC-style container — RAM:{memory_mb}MB CPU:{cpu_cores} Disk:{disk_gb}GB CPU_NAME:{cpu_name}")

    # ── systemd requires these mounts/settings to run as PID 1 in Docker ──
    run_kwargs = dict(
        name=vps_id,
        detach=True,
        privileged=True,                 # required for systemd + bind mounts
        tty=True,
        stdin_open=True,
        mem_limit=mem_str,
        memswap_limit=mem_str,
        cpu_period=cpu_period,
        cpu_quota=cpu_quota,
        environment={"TERM": "xterm-256color", "container": "docker"},
        # systemd as PID 1 — makes the container behave like a real VPS/LXC
        command="/sbin/init",
        tmpfs={"/run": "", "/run/lock": ""},
        volumes={"/sys/fs/cgroup": {"bind": "/sys/fs/cgroup", "mode": "rw"}},
        labels={"managed-by": "blined-cloud", "vps-id": vps_id},
    )

    try:
        run_kwargs["storage_opt"] = {"size": f"{disk_gb}G"}
        container = client.containers.run(image, **run_kwargs)
        log.info(f"[{vps_id}] Container created with disk quota.")
    except Exception as e:
        log.warning(f"[{vps_id}] storage_opt failed ({e}), retrying without.")
        run_kwargs.pop("storage_opt", None)
        container = client.containers.run(image, **run_kwargs)
        log.info(f"[{vps_id}] Container created without disk quota.")

    # Give systemd a moment to finish booting before running exec commands
    time.sleep(4)

    # ── apt update ──────────────────────────────────────────────────
    log.info(f"[{vps_id}] apt update...")
    container.exec_run("bash -c 'apt-get update -qq'", tty=False)

    # ── apt install tmate + neofetch ────────────────────────────────
    log.info(f"[{vps_id}] Installing tmate + neofetch...")
    container.exec_run(
        "bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tmate neofetch'",
        tty=False,
    )

    # ── Fake /proc/meminfo + /proc/cpuinfo ──────────────────────────
    container.exec_run("mkdir -p /etc/blined", tty=False)

    fake_mem = make_fake_meminfo(memory_mb)
    container_put(container, "/etc/blined/meminfo", fake_mem)
    container.exec_run("bash -c 'mount --bind /etc/blined/meminfo /proc/meminfo'", tty=False)
    log.info(f"[{vps_id}] /proc/meminfo bind-mounted ({memory_mb} MB).")

    fake_cpu = make_fake_cpuinfo(cpu_cores, cpu_name)
    container_put(container, "/etc/blined/cpuinfo", fake_cpu)
    container.exec_run("bash -c 'mount --bind /etc/blined/cpuinfo /proc/cpuinfo'", tty=False)
    log.info(f"[{vps_id}] /proc/cpuinfo bind-mounted ({int(cpu_cores)} vCPU, {cpu_name}).")

    # rc.local — re-apply on restart
    rc_local = (
        "#!/bin/bash\n"
        "mount --bind /etc/blined/meminfo /proc/meminfo 2>/dev/null\n"
        "mount --bind /etc/blined/cpuinfo /proc/cpuinfo 2>/dev/null\n"
        "exit 0\n"
    )
    container_put(container, "/etc/rc.local", rc_local)
    container.exec_run("chmod +x /etc/rc.local", tty=False)

    # ── Hostname + MOTD ──────────────────────────────────────────────
    cpu_int = int(cpu_cores) if cpu_cores == int(cpu_cores) else cpu_cores
    container.exec_run(f"bash -c 'hostnamectl set-hostname {vps_id} 2>/dev/null || hostname {vps_id}'", tty=False)
    container.exec_run(f"bash -c 'echo {vps_id} > /etc/hostname'", tty=False)

    motd = (
        f"\n"
        f"  ╔══════════════════════════════════╗\n"
        f"  ║       🌩  Blined Cloud VPS         ║\n"
        f"  ╠══════════════════════════════════╣\n"
        f"  ║  VPS ID : {vps_id:<24}║\n"
        f"  ║  RAM    : {str(memory_mb)+' MB':<24}║\n"
        f"  ║  CPU    : {str(cpu_int)+' vCore(s)':<24}║\n"
        f"  ║  Disk   : {str(disk_gb)+' GB':<24}║\n"
        f"  ║  OS     : {image:<24}║\n"
        f"  ╚══════════════════════════════════╝\n\n"
    )
    container_put(container, "/etc/motd", motd)

    # ── Start tmate ──────────────────────────────────────────────────
    log.info(f"[{vps_id}] Starting tmate...")
    sock = "/tmp/tmate.sock"
    container.exec_run(f"bash -c 'rm -f {sock}; tmate -S {sock} new-session -d'", tty=False)
    time.sleep(4)
    container.exec_run(f"bash -c 'tmate -S {sock} wait tmate-ready'", tty=False)

    ssh_r = container.exec_run(f"bash -c \"tmate -S {sock} display -p '#{{tmate_ssh}}'\"", tty=False)
    web_r = container.exec_run(f"bash -c \"tmate -S {sock} display -p '#{{tmate_web}}'\"", tty=False)

    ssh_cmd = ssh_r.output.decode(errors="ignore").strip() if ssh_r.output else ""
    web_url = web_r.output.decode(errors="ignore").strip() if web_r.output else ""
    log.info(f"[{vps_id}] tmate SSH: {ssh_cmd}")

    return container, ssh_cmd, web_url


def regen_tmate(container):
    sock = "/tmp/tmate.sock"
    container.exec_run("bash -c 'pkill tmate; rm -f /tmp/tmate.sock'", tty=False)
    time.sleep(2)
    container.exec_run(f"bash -c 'tmate -S {sock} new-session -d'", tty=False)
    time.sleep(4)
    container.exec_run(f"bash -c 'tmate -S {sock} wait tmate-ready'", tty=False)
    ssh_r = container.exec_run(f"bash -c \"tmate -S {sock} display -p '#{{tmate_ssh}}'\"", tty=False)
    web_r = container.exec_run(f"bash -c \"tmate -S {sock} display -p '#{{tmate_web}}'\"", tty=False)
    ssh_cmd = ssh_r.output.decode(errors="ignore").strip() if ssh_r.output else ""
    web_url = web_r.output.decode(errors="ignore").strip() if web_r.output else ""
    return ssh_cmd, web_url


def container_stats(container, assigned_ram_mb=0, assigned_cpu_cores=0):
    raw       = container.stats(stream=False)
    cpu_delta = (raw["cpu_stats"]["cpu_usage"]["total_usage"]
                 - raw["precpu_stats"]["cpu_usage"]["total_usage"])
    sys_delta = (raw["cpu_stats"]["system_cpu_usage"]
                 - raw["precpu_stats"]["system_cpu_usage"])
    ncpu      = raw["cpu_stats"].get("online_cpus", 1)
    raw_pct   = (cpu_delta / sys_delta) * ncpu * 100 if sys_delta else 0
    cpu_pct   = round(min(raw_pct / assigned_cpu_cores, 100), 2) if assigned_cpu_cores else round(raw_pct, 2)

    mem_used  = raw["memory_stats"].get("usage", 0)
    mem_limit = assigned_ram_mb * 1024 * 1024 if assigned_ram_mb else 1
    mem_mb    = round(mem_used / 1024 / 1024, 1)
    mem_pct   = round(min(mem_used / mem_limit * 100, 100), 2)

    net_rx = net_tx = 0
    for iface in raw.get("networks", {}).values():
        net_rx += iface.get("rx_bytes", 0)
        net_tx += iface.get("tx_bytes", 0)

    started = container.attrs["State"].get("StartedAt", "")
    uptime  = "N/A"
    if started and started != "0001-01-01T00:00:00Z":
        try:
            s = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
            d = datetime.datetime.now(datetime.timezone.utc) - s
            h, r = divmod(int(d.total_seconds()), 3600)
            m, s = divmod(r, 60)
            uptime = f"{h}h {m}m {s}s"
        except Exception:
            pass

    return {
        "cpu_pct": cpu_pct,
        "mem_mb": mem_mb,
        "mem_limit_mb": assigned_ram_mb,
        "mem_pct": mem_pct,
        "net_rx_mb": round(net_rx / 1024 / 1024, 2),
        "net_tx_mb": round(net_tx / 1024 / 1024, 2),
        "uptime": uptime,
    }

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
        log.info(f"Logged in as {self.user}")
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="Powered by Blined Cloud",
        ))

bot = BlinedCloudBot()

# ══════════════════════════════════════════════
# USER COMMANDS
# ══════════════════════════════════════════════

@bot.tree.command(name="start", description="Start your VPS.")
@app_commands.describe(vps_id="Your VPS ID e.g. BC-0001")
async def cmd_start(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=embed("❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))
    with db_connect() as c:
        row = c.execute("SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
    if row["status"] == "suspended":
        return await interaction.followup.send(embed=embed("⛔ Suspended", "Contact an admin.", WARN_COLOR))
    try:
        get_docker().containers.get(row["container_id"]).start()
        with db_connect() as c:
            c.execute("UPDATE vps_instances SET status='running' WHERE vps_id=?", (vps_id,))
        await interaction.followup.send(embed=embed("✅ Started", f"**{vps_id}** is running.\nUse `/regen-ssh {vps_id}` to get a fresh tmate link.", SUCCESS_COLOR))
    except Exception as e:
        await interaction.followup.send(embed=embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="stop", description="Stop your VPS.")
@app_commands.describe(vps_id="Your VPS ID e.g. BC-0001")
async def cmd_stop(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=embed("❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))
    try:
        with db_connect() as c:
            row = c.execute("SELECT container_id FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
        get_docker().containers.get(row["container_id"]).stop()
        with db_connect() as c:
            c.execute("UPDATE vps_instances SET status='stopped' WHERE vps_id=?", (vps_id,))
        await interaction.followup.send(embed=embed("🛑 Stopped", f"**{vps_id}** stopped.", WARN_COLOR))
    except Exception as e:
        await interaction.followup.send(embed=embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="restart", description="Restart your VPS.")
@app_commands.describe(vps_id="Your VPS ID e.g. BC-0001")
async def cmd_restart(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=embed("❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))
    try:
        with db_connect() as c:
            row = c.execute("SELECT container_id, status FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
        if row["status"] == "suspended":
            return await interaction.followup.send(embed=embed("⛔ Suspended", "Contact an admin.", WARN_COLOR))
        get_docker().containers.get(row["container_id"]).restart()
        with db_connect() as c:
            c.execute("UPDATE vps_instances SET status='running' WHERE vps_id=?", (vps_id,))
        await interaction.followup.send(embed=embed("🔄 Restarted", f"**{vps_id}** restarted.\nUse `/regen-ssh {vps_id}` to get a fresh tmate link.", SUCCESS_COLOR))
    except Exception as e:
        await interaction.followup.send(embed=embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="reinstall", description="Reinstall your VPS (same specs, data wiped).")
@app_commands.describe(vps_id="Your VPS ID e.g. BC-0001")
async def cmd_reinstall(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=embed("❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))
    with db_connect() as c:
        row = c.execute("SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
    await interaction.followup.send(embed=embed("⏳ Reinstalling...", "Running apt update → apt install tmate neofetch → tmate\n~60 seconds...", WARN_COLOR))
    try:
        try:
            get_docker().containers.get(row["container_id"]).remove(force=True)
        except Exception:
            pass
        container, ssh_cmd, web_url = await asyncio.get_event_loop().run_in_executor(
            None, lambda: provision_vps(vps_id, row["os_image"], row["memory_mb"], row["cpu_cores"], row["disk_gb"], row["cpu_name"])
        )
        with db_connect() as c:
            c.execute("UPDATE vps_instances SET container_id=?, tmate_ssh=?, tmate_web=?, status='running' WHERE vps_id=?",
                      (container.id, ssh_cmd, web_url, vps_id))
        try:
            dm = await interaction.user.create_dm()
            await dm.send(embed=embed("🔄 VPS Reinstalled", f"**{vps_id}** is fresh and ready.", SUCCESS_COLOR, fields=[
                ("🖥 SSH Command", f"```{ssh_cmd}```" if ssh_cmd else "Run `/regen-ssh`", False),
                ("🌐 Web Terminal", web_url or "Run `/regen-ssh`", False),
            ]))
        except discord.Forbidden:
            pass
        await interaction.followup.send(embed=embed("✅ Reinstalled", f"**{vps_id}** reinstalled. Check your DMs.", SUCCESS_COLOR))
    except Exception as e:
        log.error(f"reinstall: {e}")
        await interaction.followup.send(embed=embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="regen-ssh", description="Get a fresh tmate session for your VPS.")
@app_commands.describe(vps_id="Your VPS ID e.g. BC-0001")
async def cmd_regen_ssh(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=embed("❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))
    with db_connect() as c:
        row = c.execute("SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
    if row["status"] != "running":
        return await interaction.followup.send(embed=embed("⚠️ Not Running", f"Start it first: `/start {vps_id}`", WARN_COLOR))
    try:
        container = get_docker().containers.get(row["container_id"])
        ssh_cmd, web_url = await asyncio.get_event_loop().run_in_executor(None, lambda: regen_tmate(container))
        if not ssh_cmd:
            return await interaction.followup.send(embed=embed("⚠️ Not Ready", "Try again in 15 seconds.", WARN_COLOR))
        with db_connect() as c:
            c.execute("UPDATE vps_instances SET tmate_ssh=?, tmate_web=? WHERE vps_id=?", (ssh_cmd, web_url, vps_id))
        await interaction.followup.send(embed=embed(
            f"🖥 tmate Session — {vps_id}",
            "⚠️ Keep these private — anyone with the link can access your terminal.",
            SUCCESS_COLOR,
            fields=[
                ("🖥 SSH Command",  f"```{ssh_cmd}```", False),
                ("🌐 Web Terminal", web_url or "N/A",   False),
            ],
        ))
    except Exception as e:
        log.error(f"regen-ssh: {e}")
        await interaction.followup.send(embed=embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="vps-performance", description="Show live performance stats for your VPS.")
@app_commands.describe(vps_id="Your VPS ID e.g. BC-0001")
async def cmd_vps_performance(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.upper()
    if not owns_vps(interaction.user.id, vps_id):
        return await interaction.followup.send(embed=embed("❌ Access Denied", "That VPS doesn't belong to you.", ERROR_COLOR))
    try:
        with db_connect() as c:
            row = c.execute("SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
        ct = get_docker().containers.get(row["container_id"])
        ct.reload()
        if ct.status != "running":
            return await interaction.followup.send(embed=embed("⚠️ Not Running", f"Start it first: `/start {vps_id}`", WARN_COLOR))
        stats = container_stats(ct, row["memory_mb"], row["cpu_cores"])
        disk_r = ct.exec_run("df -BM / --output=used | tail -1", tty=False)
        disk_used = "N/A"
        if disk_r.exit_code == 0:
            raw = disk_r.output.decode().strip().replace("M", "").strip()
            try: disk_used = f"{round(int(raw)/1024, 2)} GB"
            except Exception: disk_used = raw + " MB"
        await interaction.followup.send(embed=embed("📊 VPS Performance", "", INFO_COLOR, fields=[
            ("🆔 VPS ID",     vps_id,                                                              True),
            ("📌 Status",     "Running",                                                            True),
            ("🖥 OS",         row["os_image"],                                                      True),
            ("🏷 CPU Model",  row["cpu_name"],                                                      True),
            ("💻 CPU Usage",  f"{stats['cpu_pct']}% of {row['cpu_cores']} core(s)",                True),
            ("🧠 RAM Usage",  f"{stats['mem_mb']} MB / {row['memory_mb']} MB ({stats['mem_pct']}%)", True),
            ("💾 Disk Usage", f"{disk_used} / {row['disk_gb']} GB",                                 True),
            ("⏱ Uptime",     stats["uptime"],                                                       True),
            ("🌐 Net RX",     f"{stats['net_rx_mb']} MB",                                           True),
            ("🌐 Net TX",     f"{stats['net_tx_mb']} MB",                                           True),
        ]))
    except Exception as e:
        log.error(f"vps-performance: {e}")
        await interaction.followup.send(embed=embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="my-vps", description="List all your VPS instances.")
async def cmd_my_vps(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    with db_connect() as c:
        rows = c.execute("SELECT * FROM vps_instances WHERE user_id=? ORDER BY vps_id", (interaction.user.id,)).fetchall()
    if not rows:
        return await interaction.followup.send(embed=embed("📋 My VPS", "You have no VPS instances.", WARN_COLOR))
    fields = [(r["vps_id"], f"OS: `{r['os_image']}` | RAM: `{r['memory_mb']}MB` | CPU: `{r['cpu_cores']}` ({r['cpu_name']}) | Disk: `{r['disk_gb']}GB` | Status: `{r['status']}`", False) for r in rows]
    await interaction.followup.send(embed=embed(f"📋 My VPS ({len(rows)})", "", INFO_COLOR, fields=fields))


@bot.tree.command(name="commands", description="Show all Blined Cloud commands.")
async def cmd_commands(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    u = embed("👤 User Commands", "Available to all VPS owners.", BRAND_COLOR, fields=[
        ("`/start <vps_id>`",           "▶️  Start your VPS",                          False),
        ("`/stop <vps_id>`",            "⏹️  Stop your VPS",                           False),
        ("`/restart <vps_id>`",         "🔄  Restart your VPS",                        False),
        ("`/reinstall <vps_id>`",       "🔁  Wipe & reinstall VPS (same specs)",       False),
        ("`/regen-ssh <vps_id>`",       "🔑  Get fresh tmate session link",            False),
        ("`/vps-performance <vps_id>`", "📊  Live CPU, RAM, Disk, Uptime stats",       False),
        ("`/my-vps`",                   "📋  List all your VPS instances",             False),
        ("`/commands`",                 "📖  Show this command list",                  False),
    ])
    a = embed("🛡️ Admin Commands", "Requires Admin Role or Admin User ID.", ERROR_COLOR, fields=[
        ("`/create <user> <memory> <cpu> <disk> <os> <cpu_name>`", "➕  Provision a new VPS",         False),
        ("`/admin-add-user <user>`",                               "✅  Grant hosting access",        False),
        ("`/admin-remove-user <user>`",                            "❌  Revoke hosting access",       False),
        ("`/suspend-vps <vps_id>`",                                "⛔  Stop & lock a VPS",           False),
        ("`/unsuspend-vps <vps_id>`",                              "🔓  Reactivate a suspended VPS",  False),
        ("`/remove-vps <vps_id>`",                                 "🗑️  Permanently delete a VPS",    False),
        ("`/list-vps`",                                            "📋  List all VPS on the node",    False),
        ("`/node-stats`",                                          "🖥️  Host CPU, RAM, Disk stats",   False),
    ])
    r = embed("📖 Reference", "", INFO_COLOR, fields=[
        ("OS Templates", "`ubuntu20` `ubuntu22` `ubuntu24` `debian11` `debian12`", False),
        ("CPU Types",    "`ryzen9` → AMD Ryzen 9 9950X\n`xeon` → Intel Xeon Platinum 8480+", False),
        ("VPS ID Format","Auto-assigned: `BC-0001`, `BC-0002` ...", False),
        ("Credentials",  "SSH/tmate links sent via **DM only** — never public.", False),
        ("Container Type", "LXC-style — systemd as PID 1, not a bare process.", False),
    ])
    await interaction.followup.send(embeds=[u, a, r])

# ══════════════════════════════════════════════
# ADMIN COMMANDS
# ══════════════════════════════════════════════

@bot.tree.command(name="admin-add-user", description="[Admin] Grant a user hosting access.")
@app_commands.describe(user="Discord user to grant access")
async def cmd_admin_add_user(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=embed("⛔ Forbidden", "Admin only.", ERROR_COLOR))
    with db_connect() as c:
        c.execute("INSERT OR IGNORE INTO allowed_users (user_id, added_by) VALUES (?, ?)", (user.id, interaction.user.id))
    await interaction.followup.send(embed=embed("✅ User Added", f"{user.mention} granted hosting access.", SUCCESS_COLOR))


@bot.tree.command(name="admin-remove-user", description="[Admin] Revoke a user's hosting access.")
@app_commands.describe(user="Discord user to revoke")
async def cmd_admin_remove_user(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=embed("⛔ Forbidden", "Admin only.", ERROR_COLOR))
    with db_connect() as c:
        c.execute("DELETE FROM allowed_users WHERE user_id=?", (user.id,))
    await interaction.followup.send(embed=embed("🗑 User Removed", f"{user.mention}'s access revoked.", WARN_COLOR))


@bot.tree.command(name="create", description="[Admin] Create a new VPS for a user.")
@app_commands.describe(
    user="Target Discord user",
    memory="RAM in MB (e.g. 512)",
    cpu="CPU cores (e.g. 1)",
    disk="Disk in GB (e.g. 10)",
    os="OS template",
    cpu_name="CPU model shown in neofetch",
)
@app_commands.choices(
    os=[
        app_commands.Choice(name="Ubuntu 20.04", value="ubuntu20"),
        app_commands.Choice(name="Ubuntu 22.04", value="ubuntu22"),
        app_commands.Choice(name="Ubuntu 24.04", value="ubuntu24"),
        app_commands.Choice(name="Debian 11",    value="debian11"),
        app_commands.Choice(name="Debian 12",    value="debian12"),
    ],
    cpu_name=[
        app_commands.Choice(name="AMD Ryzen 9 9950X",            value="ryzen9"),
        app_commands.Choice(name="Intel Xeon Platinum 8480+",    value="xeon"),
    ],
)
async def cmd_create(
    interaction: discord.Interaction,
    user: discord.Member,
    memory: int,
    cpu: float,
    disk: int,
    os: app_commands.Choice[str],
    cpu_name: app_commands.Choice[str],
):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=embed("⛔ Forbidden", "Admin only.", ERROR_COLOR))

    image         = OS_MAP[os.value]
    cpu_full_name = CPU_TYPES[cpu_name.value]
    vps_id        = next_vps_id()

    await interaction.followup.send(embed=embed(
        "⏳ Provisioning...",
        f"**{vps_id}** for {user.mention}\n\n"
        f"```\n[1/4] apt update                   ⏳\n"
        f"[2/4] apt install tmate neofetch   ⏳\n"
        f"[3/4] Faking CPU/RAM specs          ⏳\n"
        f"[4/4] Starting tmate                ⏳\n```\n"
        f"~60 seconds... Credentials sent to user's DM.",
        INFO_COLOR,
        fields=[
            ("OS",       os.name,        True),
            ("RAM",      f"{memory} MB", True),
            ("CPU",      f"{cpu} core(s)", True),
            ("Disk",     f"{disk} GB",   True),
            ("CPU Model",cpu_full_name,  False),
        ],
    ))

    try:
        container, ssh_cmd, web_url = await asyncio.get_event_loop().run_in_executor(
            None, lambda: provision_vps(vps_id, image, memory, cpu, disk, cpu_full_name)
        )
    except Exception as e:
        log.error(f"create: {e}")
        return await interaction.followup.send(embed=embed("❌ Error", str(e), ERROR_COLOR))

    with db_connect() as c:
        c.execute("""
            INSERT INTO vps_instances
              (vps_id, user_id, container_id, os_image, memory_mb, cpu_cores, disk_gb, cpu_name, tmate_ssh, tmate_web, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
        """, (vps_id, user.id, container.id, image, memory, cpu, disk, cpu_full_name, ssh_cmd, web_url))

    log.info(f"{interaction.user} created {vps_id} for {user}.")

    dm_sent = False
    try:
        dm = await user.create_dm()
        await dm.send(embed=embed(
            "🎉 Your VPS is Ready — Blined Cloud",
            "⚠️ **Keep these private — anyone with the link can access your terminal.**",
            SUCCESS_COLOR,
            fields=[
                ("🆔 VPS ID",      vps_id,                                                       True),
                ("🖥 OS",          os.name,                                                       True),
                ("🧠 RAM",         f"{memory} MB",                                                True),
                ("💻 CPU",         f"{cpu} core(s) — {cpu_full_name}",                           True),
                ("💾 Disk",        f"{disk} GB",                                                  True),
                ("🖥 SSH Command", f"```{ssh_cmd}```" if ssh_cmd else "Run `/regen-ssh` in 30s",  False),
                ("🌐 Web Terminal",web_url or "Run `/regen-ssh` to get the link",                False),
            ],
        ))
        dm_sent = True
    except discord.Forbidden:
        log.warning(f"Could not DM {user}.")

    note = "✅ Credentials sent to DM." if dm_sent else "⚠️ Could not DM user — share manually."
    await interaction.followup.send(embed=embed(
        "✅ VPS Created",
        f"**{vps_id}** is live for {user.mention}.\n{note}",
        SUCCESS_COLOR,
        fields=[
            ("🆔 VPS ID",   vps_id,           True),
            ("👤 Owner",    str(user),         True),
            ("🖥 OS",       os.name,           True),
            ("🧠 RAM",      f"{memory} MB",    True),
            ("💻 CPU",      f"{cpu} core(s)",  True),
            ("💾 Disk",     f"{disk} GB",      True),
            ("🏷 CPU Model",cpu_full_name,     False),
        ],
    ))

    if interaction.channel:
        await interaction.channel.send(embed=embed(
            "🌩️ VPS Provisioned",
            f"{user.mention} — your **{vps_id}** is ready! Check your **DMs** for the tmate link.",
            BRAND_COLOR,
            fields=[
                ("🆔 VPS ID", vps_id,          True),
                ("🖥 OS",     os.name,          True),
                ("🧠 RAM",    f"{memory} MB",   True),
                ("💻 CPU",    f"{cpu} core(s)", True),
                ("💾 Disk",   f"{disk} GB",     True),
            ],
        ))


@bot.tree.command(name="suspend-vps", description="[Admin] Suspend a VPS.")
@app_commands.describe(vps_id="VPS ID to suspend")
async def cmd_suspend_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=embed("⛔ Forbidden", "Admin only.", ERROR_COLOR))
    vps_id = vps_id.upper()
    with db_connect() as c:
        row = c.execute("SELECT container_id FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
    if not row:
        return await interaction.followup.send(embed=embed("❌ Not Found", f"**{vps_id}** not found.", ERROR_COLOR))
    try: get_docker().containers.get(row["container_id"]).stop()
    except Exception: pass
    with db_connect() as c:
        c.execute("UPDATE vps_instances SET status='suspended' WHERE vps_id=?", (vps_id,))
    await interaction.followup.send(embed=embed("⛔ Suspended", f"**{vps_id}** suspended.", WARN_COLOR))


@bot.tree.command(name="unsuspend-vps", description="[Admin] Reactivate a suspended VPS.")
@app_commands.describe(vps_id="VPS ID to unsuspend")
async def cmd_unsuspend_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=embed("⛔ Forbidden", "Admin only.", ERROR_COLOR))
    vps_id = vps_id.upper()
    with db_connect() as c:
        row = c.execute("SELECT container_id FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
    if not row:
        return await interaction.followup.send(embed=embed("❌ Not Found", f"**{vps_id}** not found.", ERROR_COLOR))
    try:
        get_docker().containers.get(row["container_id"]).start()
        with db_connect() as c:
            c.execute("UPDATE vps_instances SET status='running' WHERE vps_id=?", (vps_id,))
        await interaction.followup.send(embed=embed("✅ Unsuspended", f"**{vps_id}** active. User can run `/regen-ssh {vps_id}`.", SUCCESS_COLOR))
    except Exception as e:
        await interaction.followup.send(embed=embed("❌ Error", str(e), ERROR_COLOR))


@bot.tree.command(name="remove-vps", description="[Admin] Permanently delete a VPS.")
@app_commands.describe(vps_id="VPS ID to delete")
async def cmd_remove_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=embed("⛔ Forbidden", "Admin only.", ERROR_COLOR))
    vps_id = vps_id.upper()
    with db_connect() as c:
        row = c.execute("SELECT container_id FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
    if not row:
        return await interaction.followup.send(embed=embed("❌ Not Found", f"**{vps_id}** not found.", ERROR_COLOR))
    try: get_docker().containers.get(row["container_id"]).remove(force=True)
    except Exception: pass
    with db_connect() as c:
        c.execute("DELETE FROM vps_instances WHERE vps_id=?", (vps_id,))
    await interaction.followup.send(embed=embed("🗑 Removed", f"**{vps_id}** permanently deleted.", WARN_COLOR))


@bot.tree.command(name="list-vps", description="[Admin] List all VPS instances.")
async def cmd_list_vps(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=embed("⛔ Forbidden", "Admin only.", ERROR_COLOR))
    with db_connect() as c:
        rows = c.execute("SELECT * FROM vps_instances ORDER BY vps_id").fetchall()
    if not rows:
        return await interaction.followup.send(embed=embed("📋 All VPS", "None found.", WARN_COLOR))
    fields = [(r["vps_id"], f"<@{r['user_id']}> | OS:`{r['os_image']}` RAM:`{r['memory_mb']}MB` CPU:`{r['cpu_cores']}` ({r['cpu_name']}) Disk:`{r['disk_gb']}GB` Status:`{r['status']}`", False) for r in rows]
    for i in range(0, len(fields), 25):
        await interaction.followup.send(embed=embed(f"📋 All VPS ({len(rows)}) — Page {i//25+1}", "", INFO_COLOR, fields=fields[i:i+25]))


@bot.tree.command(name="node-stats", description="[Admin] Show host node resource usage.")
async def cmd_node_stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=embed("⛔ Forbidden", "Admin only.", ERROR_COLOR))
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    dsk = psutil.disk_usage("/")
    try:
        cl = get_docker()
        running = len([c for c in cl.containers.list() if c.status == "running"])
        total   = len(cl.containers.list(all=True))
    except Exception:
        running = total = 0
    await interaction.followup.send(embed=embed("🖥 Node Stats", "", INFO_COLOR, fields=[
        ("🖥 CPU",              f"{cpu}%",                                                                       True),
        ("🧠 RAM",              f"{round(mem.used/1024**3,2)} / {round(mem.total/1024**3,2)} GB ({mem.percent}%)", True),
        ("💾 Disk",             f"{bytes_to_gb(dsk.used)} / {bytes_to_gb(dsk.total)} GB ({dsk.percent}%)",        True),
        ("🐳 Running",          str(running),                                                                     True),
        ("📦 Total Containers", str(total),                                                                       True),
    ]))

# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.critical("DISCORD_TOKEN not set!")
        raise SystemExit(1)
    db_init()
    log.info("Starting Blined Cloud VPS Manager...")
    bot.run(DISCORD_TOKEN, log_handler=None)
