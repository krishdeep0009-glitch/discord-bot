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
from discord.ext import commands, tasks
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
                suspend_at   TEXT DEFAULT NULL,
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
    """
    Generate the next sequential VPS ID, checking BOTH the database
    AND existing Docker containers. This prevents collisions when a
    previous /create attempt created a container but failed before
    the DB insert happened (e.g. tmate timeout) — without this check,
    next_vps_id() would return the same ID again and hit a 409 Conflict.
    """
    with db_connect() as c:
        row = c.execute("SELECT vps_id FROM vps_instances ORDER BY vps_id DESC LIMIT 1").fetchone()
    db_next = 1 if not row else int(row['vps_id'].split('-')[-1]) + 1

    # Also check Docker for any blinedcloud-vps-XXXX containers not yet in DB
    docker_max = 0
    try:
        client = get_docker()
        containers = client.containers.list(all=True, filters={"label": "managed-by=blined-cloud"})
        for c_obj in containers:
            name = c_obj.name
            if name.startswith("blinedcloud-vps-"):
                try:
                    num = int(name.split("-")[-1])
                    docker_max = max(docker_max, num)
                except ValueError:
                    continue
    except Exception as e:
        log.warning(f"next_vps_id: could not check Docker for existing containers: {e}")

    next_num = max(db_next, docker_max + 1)
    return f"blinedcloud-vps-{next_num:04d}"

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

    # ── Remove any stale/leftover container with this name first ───────
    # This happens if a previous /create attempt failed partway through
    # (e.g. tmate timed out) and left the container behind, or a container
    # exists in 'created'/'exited' state from an earlier failed run.
    # We search by name filter (more reliable than .get() across API versions)
    # and force-remove + verify it's actually gone before proceeding.
    try:
        existing = client.containers.list(all=True, filters={"name": f"^/{vps_id}$"})
        for old in existing:
            log.warning(f"[{vps_id}] Found leftover container ({old.short_id}, status={old.status}) — force removing.")
            try:
                old.remove(force=True, v=True)
            except docker.errors.NotFound:
                pass
            except Exception as rm_err:
                log.error(f"[{vps_id}] Failed to remove leftover container: {rm_err}")

        # Verify removal completed — Docker's remove can be briefly async
        for attempt in range(10):
            still_there = client.containers.list(all=True, filters={"name": f"^/{vps_id}$"})
            if not still_there:
                break
            log.warning(f"[{vps_id}] Container still present, waiting... (attempt {attempt+1}/10)")
            time.sleep(1)
        else:
            raise RuntimeError(
                f"Container '{vps_id}' could not be removed after 10 attempts. "
                f"Run 'docker rm -f {vps_id}' manually on the host, then try again."
            )
    except RuntimeError:
        raise
    except Exception as e:
        log.warning(f"[{vps_id}] Cleanup check failed (continuing anyway): {e}")

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
        if not check_expired_vps.is_running():
            check_expired_vps.start()

bot = BlinedCloudBot()


@tasks.loop(minutes=15)
async def check_expired_vps():
    """Background task — checks every 15 min for VPS instances past their suspend_at date."""
    now = datetime.datetime.utcnow()
    with db_connect() as c:
        rows = c.execute(
            "SELECT * FROM vps_instances WHERE suspend_at IS NOT NULL AND status != 'suspended'"
        ).fetchall()

    for row in rows:
        try:
            expiry = datetime.datetime.fromisoformat(row["suspend_at"])
        except Exception:
            continue
        if now < expiry:
            continue

        vps_id = row["vps_id"]
        log.info(f"[{vps_id}] Auto-suspend triggered — expiry reached.")

        try:
            client = get_docker()
            client.containers.get(row["container_id"]).stop()
        except Exception as e:
            log.warning(f"[{vps_id}] Could not stop container during auto-suspend: {e}")

        with db_connect() as c:
            c.execute("UPDATE vps_instances SET status='suspended' WHERE vps_id=?", (vps_id,))

        # Notify the owner via DM
        try:
            user = await bot.fetch_user(row["user_id"])
            await user.send(embed=embed(
                "⏰ VPS Auto-Suspended",
                f"Your VPS **{vps_id}** has reached its expiry date and has been automatically suspended.\n"
                f"Contact an admin to unsuspend it with `/unsuspend-vps {vps_id}`.",
                WARN_COLOR,
            ))
        except Exception as e:
            log.warning(f"[{vps_id}] Could not DM owner about auto-suspend: {e}")


@check_expired_vps.before_loop
async def before_check_expired_vps():
    await bot.wait_until_ready()

# ══════════════════════════════════════════════
# USER COMMANDS
# ══════════════════════════════════════════════

@bot.tree.command(name="start", description="Start your VPS.")
@app_commands.describe(vps_id="Your VPS ID e.g. blinedcloud-vps-0001")
async def cmd_start(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
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
@app_commands.describe(vps_id="Your VPS ID e.g. blinedcloud-vps-0001")
async def cmd_stop(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
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
@app_commands.describe(vps_id="Your VPS ID e.g. blinedcloud-vps-0001")
async def cmd_restart(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
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
@app_commands.describe(vps_id="Your VPS ID e.g. blinedcloud-vps-0001")
async def cmd_reinstall(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
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
@app_commands.describe(vps_id="Your VPS ID e.g. blinedcloud-vps-0001")
async def cmd_regen_ssh(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
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
@app_commands.describe(vps_id="Your VPS ID e.g. blinedcloud-vps-0001")
async def cmd_vps_performance(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
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
    fields = []
    for r in rows:
        line = f"OS: `{r['os_image']}` | RAM: `{r['memory_mb']}MB` | CPU: `{r['cpu_cores']}` ({r['cpu_name']}) | Disk: `{r['disk_gb']}GB` | Status: `{r['status']}`"
        if r["suspend_at"]:
            try:
                exp_ts = int(datetime.datetime.fromisoformat(r["suspend_at"]).timestamp())
                line += f"\n⏰ Expires: <t:{exp_ts}:R>"
            except Exception:
                pass
        fields.append((r["vps_id"], line, False))
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
        ("`/create <user> <memory> <cpu> <disk> <os> <cpu_name> <suspend_in_days>`", "➕  Provision a new VPS",        False),
        ("`/admin-add-user <user>`",                                                "✅  Grant hosting access",       False),
        ("`/admin-remove-user <user>`",                                             "❌  Revoke hosting access",      False),
        ("`/extend-vps <vps_id> <days>`",                                           "⏰  Set/extend auto-suspend date", False),
        ("`/fix-vps <vps_id>`",                                                     "🔧  Remove a stuck/orphaned container", False),
        ("`/suspend-vps <vps_id>`",                                                 "⛔  Stop & lock a VPS",          False),
        ("`/unsuspend-vps <vps_id>`",                                               "🔓  Reactivate a suspended VPS", False),
        ("`/remove-vps <vps_id>`",                                                  "🗑️  Permanently delete a VPS",   False),
        ("`/list-vps`",                                                             "📋  List all VPS on the node",   False),
        ("`/node-stats`",                                                           "🖥️  Host CPU, RAM, Disk stats",  False),
    ])
    r = embed("📖 Reference", "", INFO_COLOR, fields=[
        ("OS Templates", "`ubuntu20` `ubuntu22` `ubuntu24` `debian11` `debian12`", False),
        ("CPU Types",    "`ryzen9` → AMD Ryzen 9 9950X\n`xeon` → Intel Xeon Platinum 8480+", False),
        ("VPS ID Format","Auto-assigned: `blinedcloud-vps-0001`, `blinedcloud-vps-0002` ...", False),
        ("Credentials",  "SSH/tmate links sent via **DM only** — never public.", False),
        ("Container Type", "LXC-style — systemd as PID 1, not a bare process.", False),
        ("Auto-Suspend", "Set `suspend_in_days` in `/create` (0 = never). Checked every 15 min.", False),
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
    suspend_in_days="Auto-suspend the VPS after this many days (0 = never expires)",
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
    suspend_in_days: int = 0,
):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=embed("⛔ Forbidden", "Admin only.", ERROR_COLOR))

    image         = OS_MAP[os.value]
    cpu_full_name = CPU_TYPES[cpu_name.value]
    vps_id        = next_vps_id()

    # Calculate auto-suspend date if requested
    suspend_at = None
    suspend_note = "Never expires"
    if suspend_in_days and suspend_in_days > 0:
        suspend_dt = datetime.datetime.utcnow() + datetime.timedelta(days=suspend_in_days)
        suspend_at = suspend_dt.isoformat()
        suspend_note = f"Auto-suspends on <t:{int(suspend_dt.timestamp())}:F> (in {suspend_in_days} day(s))"

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
            ("OS",         os.name,        True),
            ("RAM",        f"{memory} MB", True),
            ("CPU",        f"{cpu} core(s)", True),
            ("Disk",       f"{disk} GB",   True),
            ("CPU Model",  cpu_full_name,  False),
            ("⏰ Expiry",  suspend_note,    False),
        ],
    ))

    try:
        container, ssh_cmd, web_url = await asyncio.get_event_loop().run_in_executor(
            None, lambda: provision_vps(vps_id, image, memory, cpu, disk, cpu_full_name)
        )
    except Exception as e:
        log.error(f"create failed for {vps_id}: {e}")
        # Clean up any partially-created container so the next /create attempt
        # for this VPS ID (or a retry) doesn't hit a name conflict
        try:
            stray = get_docker().containers.get(vps_id)
            stray.remove(force=True, v=True)
            log.info(f"[{vps_id}] Cleaned up partial container after failure.")
        except Exception:
            pass
        return await interaction.followup.send(embed=embed(
            "❌ Provisioning Failed",
            f"Could not create **{vps_id}**.\n```{str(e)[:500]}```\n"
            f"Cleanup was attempted automatically. If `/create` still fails with the same ID, "
            f"run `/fix-vps {vps_id}` first, then try `/create` again.",
            ERROR_COLOR,
        ))

    with db_connect() as c:
        c.execute("""
            INSERT INTO vps_instances
              (vps_id, user_id, container_id, os_image, memory_mb, cpu_cores, disk_gb, cpu_name, tmate_ssh, tmate_web, status, suspend_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
        """, (vps_id, user.id, container.id, image, memory, cpu, disk, cpu_full_name, ssh_cmd, web_url, suspend_at))

    log.info(f"{interaction.user} created {vps_id} for {user}.")

    dm_sent = False
    try:
        dm = await user.create_dm()
        dm_fields = [
            ("🆔 VPS ID",      vps_id,                                                       True),
            ("🖥 OS",          os.name,                                                       True),
            ("🧠 RAM",         f"{memory} MB",                                                True),
            ("💻 CPU",         f"{cpu} core(s) — {cpu_full_name}",                           True),
            ("💾 Disk",        f"{disk} GB",                                                  True),
        ]
        if suspend_at:
            dm_fields.append(("⏰ Expiry", suspend_note, True))
        dm_fields += [
            ("🖥 SSH Command", f"```{ssh_cmd}```" if ssh_cmd else "Run `/regen-ssh` in 30s",  False),
            ("🌐 Web Terminal",web_url or "Run `/regen-ssh` to get the link",                False),
        ]
        await dm.send(embed=embed(
            "🎉 Your VPS is Ready — Blined Cloud",
            "⚠️ **Keep these private — anyone with the link can access your terminal.**",
            SUCCESS_COLOR,
            fields=dm_fields,
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
            ("🆔 VPS ID",    vps_id,           True),
            ("👤 Owner",     str(user),         True),
            ("🖥 OS",        os.name,           True),
            ("🧠 RAM",       f"{memory} MB",    True),
            ("💻 CPU",       f"{cpu} core(s)",  True),
            ("💾 Disk",      f"{disk} GB",      True),
            ("🏷 CPU Model", cpu_full_name,     False),
            ("⏰ Expiry",    suspend_note,      False),
        ],
    ))

    if interaction.channel:
        public_fields = [
            ("🆔 VPS ID", vps_id,          True),
            ("🖥 OS",     os.name,          True),
            ("🧠 RAM",    f"{memory} MB",   True),
            ("💻 CPU",    f"{cpu} core(s)", True),
            ("💾 Disk",   f"{disk} GB",     True),
        ]
        if suspend_at:
            public_fields.append(("⏰ Expires", suspend_note, False))
        await interaction.channel.send(embed=embed(
            "🌩️ VPS Provisioned",
            f"{user.mention} — your **{vps_id}** is ready! Check your **DMs** for the tmate link.",
            BRAND_COLOR,
            fields=public_fields,
        ))


@bot.tree.command(name="extend-vps", description="[Admin] Extend or set a VPS's auto-suspend expiry date.")
@app_commands.describe(vps_id="VPS ID to extend", days="New expiry in N days from now (0 = remove expiry, never suspends)")
async def cmd_extend_vps(interaction: discord.Interaction, vps_id: str, days: int):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=embed("⛔ Forbidden", "Admin only.", ERROR_COLOR))
    vps_id = vps_id.lower()
    with db_connect() as c:
        row = c.execute("SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
    if not row:
        return await interaction.followup.send(embed=embed("❌ Not Found", f"**{vps_id}** not found.", ERROR_COLOR))

    if days <= 0:
        with db_connect() as c:
            c.execute("UPDATE vps_instances SET suspend_at=NULL WHERE vps_id=?", (vps_id,))
        return await interaction.followup.send(embed=embed(
            "✅ Expiry Removed", f"**{vps_id}** will never auto-suspend.", SUCCESS_COLOR))

    new_expiry = datetime.datetime.utcnow() + datetime.timedelta(days=days)
    with db_connect() as c:
        c.execute("UPDATE vps_instances SET suspend_at=? WHERE vps_id=?", (new_expiry.isoformat(), vps_id))

    exp_ts = int(new_expiry.timestamp())
    await interaction.followup.send(embed=embed(
        "✅ Expiry Updated",
        f"**{vps_id}** will now auto-suspend <t:{exp_ts}:R> (<t:{exp_ts}:F>).",
        SUCCESS_COLOR,
    ))

    try:
        user = await bot.fetch_user(row["user_id"])
        await user.send(embed=embed(
            "⏰ VPS Expiry Updated",
            f"Your VPS **{vps_id}** will now auto-suspend <t:{exp_ts}:R>.",
            INFO_COLOR,
        ))
    except Exception:
        pass


@bot.tree.command(name="suspend-vps", description="[Admin] Suspend a VPS.")
@app_commands.describe(vps_id="VPS ID to suspend")
async def cmd_suspend_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=embed("⛔ Forbidden", "Admin only.", ERROR_COLOR))
    vps_id = vps_id.lower()
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
    vps_id = vps_id.lower()
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
    vps_id = vps_id.lower()
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
    fields = []
    for r in rows:
        line = f"<@{r['user_id']}> | OS:`{r['os_image']}` RAM:`{r['memory_mb']}MB` CPU:`{r['cpu_cores']}` ({r['cpu_name']}) Disk:`{r['disk_gb']}GB` Status:`{r['status']}`"
        if r["suspend_at"]:
            try:
                exp_ts = int(datetime.datetime.fromisoformat(r["suspend_at"]).timestamp())
                line += f" | Expires: <t:{exp_ts}:R>"
            except Exception:
                pass
        fields.append((r["vps_id"], line, False))
    for i in range(0, len(fields), 25):
        await interaction.followup.send(embed=embed(f"📋 All VPS ({len(rows)}) — Page {i//25+1}", "", INFO_COLOR, fields=fields[i:i+25]))


@bot.tree.command(name="fix-vps", description="[Admin] Force-remove a stuck/orphaned container blocking a VPS ID.")
@app_commands.describe(vps_id="VPS ID whose container is stuck (e.g. blinedcloud-vps-0001)")
async def cmd_fix_vps(interaction: discord.Interaction, vps_id: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        return await interaction.followup.send(embed=embed("⛔ Forbidden", "Admin only.", ERROR_COLOR))
    vps_id = vps_id.lower()

    removed = False
    try:
        stray = get_docker().containers.get(vps_id)
        stray.remove(force=True)
        removed = True
        log.info(f"{interaction.user} force-removed stuck container {vps_id}.")
    except docker.errors.NotFound:
        pass
    except Exception as e:
        return await interaction.followup.send(embed=embed("❌ Error", f"Could not remove container: {e}", ERROR_COLOR))

    # Also clear the container_id in DB if a record exists, so /create or /reinstall can run cleanly
    with db_connect() as c:
        row = c.execute("SELECT * FROM vps_instances WHERE vps_id=?", (vps_id,)).fetchone()
        if row:
            c.execute("UPDATE vps_instances SET status='stopped' WHERE vps_id=?", (vps_id,))

    if removed:
        await interaction.followup.send(embed=embed(
            "✅ Fixed", f"Removed the stuck Docker container for **{vps_id}**. You can now `/reinstall` or `/create` again.", SUCCESS_COLOR))
    else:
        await interaction.followup.send(embed=embed(
            "ℹ️ Nothing to Fix", f"No Docker container named **{vps_id}** was found — it's already clear.", INFO_COLOR))


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
