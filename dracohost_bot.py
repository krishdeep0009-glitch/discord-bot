"""
╔═══════════════════════════════════════════════════════╗
║           DracoHost VPS Manager Bot                  ║
║  Server: 180GB RAM | 94 Core CPU | Docker + systemd  ║
║  • Docker-in-Docker VPS containers                   ║
║  • Full systemctl support                            ║
║  • tmate SSH access                                  ║
║  • Fake neofetch specs                               ║
║  • Pterodactyl Panel + Wings                         ║
║  • 1-click deploy                                    ║
╚═══════════════════════════════════════════════════════╝
"""

import os, io, time, tarfile, asyncio, logging, sqlite3, datetime
import discord, docker, psutil, requests
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────
DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN", "")
ADMIN_ROLE_ID  = int(os.getenv("ADMIN_ROLE_ID", "0"))
ADMIN_USER_IDS = {
    int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",")
    if x.strip().isdigit()
}
PTERO_URL  = os.getenv("PTERO_URL", "").rstrip("/")
PTERO_KEY  = os.getenv("PTERO_API_KEY", "")
PTERO_ON   = bool(PTERO_URL and PTERO_KEY)

# ─────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("dracohost.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("DracoHost")

# ─────────────────────────────────────────────────────
# COLORS
# ─────────────────────────────────────────────────────
BLUE   = 0x5865F2
GREEN  = 0x57F287
RED    = 0xED4245
YELLOW = 0xFEE75C
DARK   = 0x2F3136
FOOTER = "Powered by DracoHost"

# ─────────────────────────────────────────────────────
# OS + CPU
# ─────────────────────────────────────────────────────
# Using jrei/systemd images — pre-built for systemd inside Docker
# These support systemctl, services, cron out of the box
OS_MAP = {
    "ubuntu20": ("jrei/systemd-ubuntu:20.04", "Ubuntu 20.04"),
    "ubuntu22": ("jrei/systemd-ubuntu:22.04", "Ubuntu 22.04"),
    "ubuntu24": ("jrei/systemd-ubuntu:24.04", "Ubuntu 24.04"),
    "debian11":  ("jrei/systemd-debian:11",   "Debian 11"),
    "debian12":  ("jrei/systemd-debian:12",   "Debian 12"),
}
CPU_MAP = {
    "ryzen9": "AMD Ryzen 9 9950X 16-Core Processor",
    "xeon":   "Intel(R) Xeon(R) Platinum 8480+ @ 3.80GHz",
}

DB_FILE = "dracohost.db"

# ─────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────
def get_db():
    c = sqlite3.connect(DB_FILE)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with get_db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id  INTEGER PRIMARY KEY,
                added_by INTEGER,
                added_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS vps (
                vps_id       TEXT    PRIMARY KEY,
                owner_id     INTEGER NOT NULL,
                container_id TEXT,
                os_image     TEXT,
                os_label     TEXT,
                ram_mb       INTEGER,
                cpu_cores    REAL,
                disk_gb      INTEGER,
                cpu_name     TEXT,
                ssh_cmd      TEXT    DEFAULT '',
                ptero_id     INTEGER DEFAULT NULL,
                status       TEXT    DEFAULT 'running',
                expires_at   TEXT    DEFAULT NULL,
                created_at   TEXT    DEFAULT (datetime('now'))
            );
        """)
    log.info("Database ready.")

# ─────────────────────────────────────────────────────
# EMBED HELPER
# ─────────────────────────────────────────────────────
def em(title, desc="", color=BLUE, fields=None):
    e = discord.Embed(
        title=title, description=desc,
        color=color, timestamp=datetime.datetime.utcnow()
    )
    e.set_footer(text=FOOTER)
    for n, v, i in (fields or []):
        e.add_field(name=n, value=v, inline=i)
    return e

# ─────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────
def get_docker():
    try:
        client = docker.from_env()
        client.ping()
        return client
    except FileNotFoundError:
        raise RuntimeError(
            "Docker socket not found!\n"
            "Run: `sudo systemctl start docker`"
        )
    except docker.errors.DockerException as e:
        raise RuntimeError(f"Docker error: {e}\nRun: `sudo systemctl start docker`")

def is_admin(ix: discord.Interaction) -> bool:
    if ix.user.id in ADMIN_USER_IDS:
        return True
    if ix.guild:
        return any(r.id == ADMIN_ROLE_ID for r in ix.user.roles)
    return False

def owns(uid: int, vid: str) -> bool:
    with get_db() as c:
        return bool(c.execute(
            "SELECT 1 FROM vps WHERE vps_id=? AND owner_id=?", (vid, uid)
        ).fetchone())

def next_id() -> str:
    with get_db() as c:
        row = c.execute("SELECT vps_id FROM vps ORDER BY vps_id DESC LIMIT 1").fetchone()
    db_num = 1 if not row else int(row["vps_id"].split("-")[-1]) + 1
    dk_max = 0
    try:
        for ct in get_docker().containers.list(
            all=True, filters={"label": "managed-by=dracohost"}
        ):
            if ct.name.startswith("dracohost-vps-"):
                try:
                    dk_max = max(dk_max, int(ct.name.split("-")[-1]))
                except ValueError:
                    pass
    except Exception:
        pass
    return f"dracohost-vps-{max(db_num, dk_max + 1):04d}"

def gb(b): return round(b / 1024**3, 2)

# ─────────────────────────────────────────────────────
# PTERODACTYL
# ─────────────────────────────────────────────────────
def ph():
    return {
        "Authorization": f"Bearer {PTERO_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def ptero_get(ep):
    r = requests.get(f"{PTERO_URL}/api/application/{ep}", headers=ph(), timeout=10)
    r.raise_for_status()
    return r.json()

def ptero_post(ep, data=None):
    r = requests.post(f"{PTERO_URL}/api/application/{ep}", headers=ph(), json=data or {}, timeout=10)
    r.raise_for_status()
    return r.json() if r.text.strip() else {}

def ptero_delete(ep):
    requests.delete(f"{PTERO_URL}/api/application/{ep}", headers=ph(), timeout=10).raise_for_status()

def ptero_check():
    try:
        n = ptero_get("nodes")
        return {"ok": True, "nodes": len(n.get("data", []))}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def ptero_suspend(pid):   ptero_post(f"servers/{pid}/suspend")
def ptero_unsuspend(pid): ptero_post(f"servers/{pid}/unsuspend")
def ptero_remove(pid):    ptero_delete(f"servers/{pid}/force")

# ─────────────────────────────────────────────────────
# FAKE /proc GENERATORS
# ─────────────────────────────────────────────────────
def fake_meminfo(mb: int) -> str:
    kb = mb * 1024
    return "\n".join([
        f"MemTotal:       {kb} kB",
        f"MemFree:        {int(kb*.88)} kB",
        f"MemAvailable:   {int(kb*.85)} kB",
        "Buffers:            128 kB",
        f"Cached:         {int(kb*.05)} kB",
        "SwapCached:           0 kB",
        f"Active:         {int(kb*.10)} kB",
        f"Inactive:       {int(kb*.02)} kB",
        "SwapTotal:            0 kB",
        "SwapFree:             0 kB",
        "Dirty:                4 kB",
        "Writeback:            0 kB",
        f"AnonPages:      {int(kb*.08)} kB",
        f"Mapped:         {int(kb*.02)} kB",
        "Shmem:               64 kB",
        "Slab:               512 kB",
        f"VmallocTotal:   {kb} kB",
        "VmallocUsed:          0 kB",
        f"VmallocChunk:   {kb} kB",
        "HugePages_Total:      0",
        "HugePages_Free:       0",
        "Hugepagesize:      2048 kB", "",
    ])

def fake_cpuinfo(cores: float, name: str) -> str:
    n = max(1, int(cores))
    v = "AuthenticAMD" if ("AMD" in name or "Ryzen" in name) else "GenuineIntel"
    blocks = []
    for i in range(n):
        blocks.append("\n".join([
            f"processor\t: {i}",
            f"vendor_id\t: {v}",
            "cpu family\t: 25",
            "model\t\t: 97",
            f"model name\t: {name}",
            "stepping\t: 2",
            "cpu MHz\t\t: 4200.000",
            "cache size\t: 65536 KB",
            "physical id\t: 0",
            f"siblings\t: {n}",
            f"core id\t\t: {i}",
            f"cpu cores\t: {n}",
            "fpu\t\t: yes",
            "bogomips\t: 8400.00",
            "clflush size\t: 64",
            "cache_alignment\t: 64", "",
        ]))
    return "\n".join(blocks)

def write_file(ct, path: str, content: str):
    data = content.encode()
    buf  = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        ti = tarfile.TarInfo(name=os.path.basename(path))
        ti.size = len(data)
        ti.mode = 0o644
        tar.addfile(ti, io.BytesIO(data))
    buf.seek(0)
    ct.put_archive(os.path.dirname(path) or "/", buf)

# ─────────────────────────────────────────────────────
# CORE VPS PROVISION
# ─────────────────────────────────────────────────────
def provision(vps_id, image, os_label, ram_mb, cpu_cores, disk_gb, cpu_name) -> tuple:
    """
    Creates a Docker VPS with full systemd support.

    KEY: Uses Docker low-level API with CgroupnsMode=host
    This is the only reliable way to run systemd inside Docker
    on cgroup v2 hosts without the threaded-mode error.

    Steps:
      1. Pull jrei/systemd image
      2. Create container via low-level API (CgroupnsMode=host)
      3. Wait for systemd to boot
      4. apt update + apt install tmate neofetch
      5. Fake /proc/meminfo and /proc/cpuinfo
      6. Set hostname and MOTD
      7. Start tmate SSH session
    """
    client  = get_docker()
    mem     = f"{ram_mb}m"
    period  = 100_000
    quota   = int(period * cpu_cores)

    log.info(f"[{vps_id}] Provisioning — RAM:{ram_mb}MB CPU:{cpu_cores} Disk:{disk_gb}GB")

    # ── Step 1: Remove any leftover container ───────────────────────
    try:
        for old in client.containers.list(all=True, filters={"name": f"^/{vps_id}$"}):
            log.warning(f"[{vps_id}] Removing leftover {old.short_id}")
            try: old.remove(force=True, v=True)
            except Exception: pass
        for _ in range(10):
            if not client.containers.list(all=True, filters={"name": f"^/{vps_id}$"}):
                break
            time.sleep(1)
    except Exception as e:
        log.warning(f"[{vps_id}] Cleanup warning: {e}")

    # ── Step 2: Pull jrei/systemd image ─────────────────────────────
    log.info(f"[{vps_id}] Pulling {image}...")
    try:
        client.images.pull(image)
        log.info(f"[{vps_id}] Image ready: {image}")
    except Exception as e:
        raise RuntimeError(
            f"Failed to pull `{image}`.\n"
            f"Check internet connection on your server.\nError: {e}"
        )

    # ── Step 3: Create container via low-level API ──────────────────
    # We MUST use the low-level API to pass CgroupnsMode=host
    # The high-level client.containers.run() doesn't support it.
    # CgroupnsMode=host lets systemd manage cgroups without
    # hitting the "threaded mode" error on cgroup v2 hosts.
    log.info(f"[{vps_id}] Creating container with CgroupnsMode=host...")

    try:
        host_cfg = client.api.create_host_config(
            mem_limit=mem,
            memswap_limit=mem,
            cpu_period=period,
            cpu_quota=quota,
            privileged=True,
            cgroupns="host",
            binds={"/sys/fs/cgroup": {"bind": "/sys/fs/cgroup", "mode": "rw"}},
            tmpfs={
                "/run":      "rw,nosuid,nodev",
                "/run/lock": "rw,nosuid,nodev",
                "/tmp":      "rw,nosuid,nodev",
            },
        )
    except TypeError:
        # Older docker-py doesn't have cgroupns param — try without
        log.warning(f"[{vps_id}] cgroupns not supported in this docker-py, trying without...")
        host_cfg = client.api.create_host_config(
            mem_limit=mem,
            memswap_limit=mem,
            cpu_period=period,
            cpu_quota=quota,
            privileged=True,
            binds={"/sys/fs/cgroup": {"bind": "/sys/fs/cgroup", "mode": "rw"}},
            tmpfs={
                "/run":      "rw,nosuid,nodev",
                "/run/lock": "rw,nosuid,nodev",
                "/tmp":      "rw,nosuid,nodev",
            },
        )

    ct_data = client.api.create_container(
        image=image,
        name=vps_id,
        detach=True,
        tty=True,
        stdin_open=True,
        environment={"TERM": "xterm-256color", "container": "docker"},
        command="/sbin/init",
        host_config=host_cfg,
        labels={"managed-by": "dracohost", "vps-id": vps_id},
    )
    client.api.start(ct_data["Id"])
    ct = client.containers.get(ct_data["Id"])
    log.info(f"[{vps_id}] Container started: {ct.short_id}")

    # ── Step 4: Wait for systemd to fully boot ───────────────────────
    log.info(f"[{vps_id}] Waiting for systemd to initialize...")
    time.sleep(8)

    # ── Step 5: apt update ───────────────────────────────────────────
    log.info(f"[{vps_id}] Running apt update...")
    ct.exec_run("bash -c 'apt-get update -qq'", tty=False)

    # ── Step 6: Install packages ─────────────────────────────────────
    log.info(f"[{vps_id}] Installing tmate, neofetch, tools...")
    ct.exec_run(
        "bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "tmate neofetch curl wget sudo procps net-tools iproute2 htop'",
        tty=False,
    )

    # ── Step 7: Fake /proc/meminfo and /proc/cpuinfo ─────────────────
    ct.exec_run("mkdir -p /etc/dracohost", tty=False)

    write_file(ct, "/etc/dracohost/meminfo", fake_meminfo(ram_mb))
    r = ct.exec_run("mount --bind /etc/dracohost/meminfo /proc/meminfo", tty=False)
    log.info(f"[{vps_id}] meminfo bind mount: exit={r.exit_code}")

    write_file(ct, "/etc/dracohost/cpuinfo", fake_cpuinfo(cpu_cores, cpu_name))
    r = ct.exec_run("mount --bind /etc/dracohost/cpuinfo /proc/cpuinfo", tty=False)
    log.info(f"[{vps_id}] cpuinfo bind mount: exit={r.exit_code}")

    # Re-apply mounts on container restart
    write_file(ct, "/etc/rc.local",
        "#!/bin/bash\n"
        "mount --bind /etc/dracohost/meminfo /proc/meminfo 2>/dev/null\n"
        "mount --bind /etc/dracohost/cpuinfo /proc/cpuinfo 2>/dev/null\n"
        "exit 0\n"
    )
    ct.exec_run("chmod +x /etc/rc.local", tty=False)

    # ── Step 8: Hostname + MOTD ──────────────────────────────────────
    ci = int(cpu_cores) if cpu_cores == int(cpu_cores) else cpu_cores
    ct.exec_run(
        f"bash -c 'hostnamectl set-hostname {vps_id} 2>/dev/null || hostname {vps_id}'",
        tty=False,
    )
    ct.exec_run(f"bash -c 'echo {vps_id} > /etc/hostname'", tty=False)
    write_file(ct, "/etc/motd",
        f"\n"
        f"  ╔══════════════════════════════════╗\n"
        f"  ║        🐉  DracoHost VPS           ║\n"
        f"  ╠══════════════════════════════════╣\n"
        f"  ║  VPS ID : {vps_id:<24}║\n"
        f"  ║  RAM    : {str(ram_mb)+' MB':<24}║\n"
        f"  ║  CPU    : {str(ci)+' vCore(s)':<24}║\n"
        f"  ║  Disk   : {str(disk_gb)+' GB':<24}║\n"
        f"  ║  OS     : {os_label:<24}║\n"
        f"  ╚══════════════════════════════════╝\n\n"
    )

    # ── Step 9: tmate SSH session ────────────────────────────────────
    log.info(f"[{vps_id}] Starting tmate SSH session...")
    sock = "/tmp/tmate.sock"
    ct.exec_run(f"bash -c 'rm -f {sock}; tmate -S {sock} new-session -d'", tty=False)
    time.sleep(5)
    ct.exec_run(f"bash -c 'tmate -S {sock} wait tmate-ready'", tty=False)
    r   = ct.exec_run(f"bash -c \"tmate -S {sock} display -p '#{{tmate_ssh}}'\"", tty=False)
    ssh = r.output.decode(errors="ignore").strip() if r.output else ""
    log.info(f"[{vps_id}] SSH ready: {ssh}")

    return ct, ssh


def regen_ssh(ct) -> str:
    sock = "/tmp/tmate.sock"
    ct.exec_run("bash -c 'pkill tmate; rm -f /tmp/tmate.sock'", tty=False)
    time.sleep(2)
    ct.exec_run(f"bash -c 'tmate -S {sock} new-session -d'", tty=False)
    time.sleep(5)
    ct.exec_run(f"bash -c 'tmate -S {sock} wait tmate-ready'", tty=False)
    r = ct.exec_run(f"bash -c \"tmate -S {sock} display -p '#{{tmate_ssh}}'\"", tty=False)
    return r.output.decode(errors="ignore").strip() if r.output else ""


def get_stats(ct, ram_mb=0, cores=0) -> dict:
    raw = ct.stats(stream=False)
    cd  = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
    sd  = raw["cpu_stats"]["system_cpu_usage"] - raw["precpu_stats"]["system_cpu_usage"]
    nc  = raw["cpu_stats"].get("online_cpus", 1)
    rp  = (cd / sd) * nc * 100 if sd else 0
    cpu = round(min(rp / cores, 100), 2) if cores else round(rp, 2)
    mu  = raw["memory_stats"].get("usage", 0)
    ml  = ram_mb * 1024 * 1024 if ram_mb else 1
    rx = tx = 0
    for iface in raw.get("networks", {}).values():
        rx += iface.get("rx_bytes", 0)
        tx += iface.get("tx_bytes", 0)
    started = ct.attrs["State"].get("StartedAt", "")
    up = "N/A"
    if started and started != "0001-01-01T00:00:00Z":
        try:
            s = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
            d = datetime.datetime.now(datetime.timezone.utc) - s
            h, r2 = divmod(int(d.total_seconds()), 3600)
            m, s2 = divmod(r2, 60)
            up = f"{h}h {m}m {s2}s"
        except Exception: pass
    return {
        "cpu":    cpu,
        "mem_mb": round(mu / 1024 / 1024, 1),
        "mem_p":  round(min(mu / ml * 100, 100), 2),
        "rx":     round(rx / 1024 / 1024, 2),
        "tx":     round(tx / 1024 / 1024, 2),
        "up":     up,
    }

# ─────────────────────────────────────────────────────
# SHARED CREATE LOGIC
# ─────────────────────────────────────────────────────
async def do_create(ix, user, ram, cpu, disk, os_key, cpu_key, days=0):
    image, os_label = OS_MAP[os_key]
    cpu_name        = CPU_MAP[cpu_key]
    vps_id          = next_id()

    exp_at   = None
    exp_note = "Never expires"
    if days > 0:
        dt       = datetime.datetime.utcnow() + datetime.timedelta(days=days)
        exp_at   = dt.isoformat()
        exp_note = f"Auto-suspends <t:{int(dt.timestamp())}:R>"

    await ix.followup.send(embed=em(
        "⏳ Provisioning VPS...",
        f"**{vps_id}** for {user.mention}\n\n"
        "```\n"
        "[1/5] Pulling jrei/systemd image  ⏳\n"
        "[2/5] Creating container          ⏳\n"
        "[3/5] apt update + apt install    ⏳\n"
        "[4/5] Faking CPU & RAM specs      ⏳\n"
        "[5/5] Starting tmate SSH          ⏳\n"
        "```\n"
        "⏱ ~90 seconds — SSH sent to DM.",
        BLUE,
        fields=[
            ("🖥 OS",        os_label,          True),
            ("🧠 RAM",       f"{ram} MB",       True),
            ("💻 CPU",       f"{cpu} Core(s)",  True),
            ("💾 Disk",      f"{disk} GB",      True),
            ("🏷 CPU Model", cpu_name,          False),
            ("⏰ Expiry",    exp_note,          False),
        ],
    ))

    try:
        ct, ssh = await asyncio.get_event_loop().run_in_executor(
            None, lambda: provision(vps_id, image, os_label, ram, cpu, disk, cpu_name)
        )
    except Exception as e:
        log.error(f"[{vps_id}] Failed: {e}")
        try: get_docker().containers.get(vps_id).remove(force=True, v=True)
        except Exception: pass
        return await ix.followup.send(embed=em(
            "❌ Provisioning Failed",
            f"**{vps_id}** could not be created.\n```{str(e)[:600]}```\n"
            f"Run `/fix-vps {vps_id}` then try again.",
            RED,
        ))

    with get_db() as c:
        c.execute("""
            INSERT INTO vps
              (vps_id,owner_id,container_id,os_image,os_label,
               ram_mb,cpu_cores,disk_gb,cpu_name,ssh_cmd,status,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,'running',?)
        """, (vps_id, user.id, ct.id, image, os_label,
              ram, cpu, disk, cpu_name, ssh, exp_at))

    log.info(f"Created {vps_id} for {user} by {ix.user}")

    # DM user credentials
    dm_ok = False
    try:
        fields = [
            ("🆔 VPS ID",    vps_id,             True),
            ("🖥 OS",        os_label,            True),
            ("🧠 RAM",       f"{ram} MB",         True),
            ("💻 CPU",       f"{cpu} Core(s)",    True),
            ("💾 Disk",      f"{disk} GB",        True),
            ("🏷 CPU Model", cpu_name,            True),
        ]
        if exp_at: fields.append(("⏰ Expiry", exp_note, False))
        fields.append(("🖥 SSH Command",
            f"```{ssh}```" if ssh else f"Run `/regen-ssh {vps_id}` in 30s.", False))
        dm = await user.create_dm()
        await dm.send(embed=em(
            "🎉 Your VPS is Ready!",
            "⚠️ **Keep the SSH command private.**",
            GREEN, fields=fields,
        ))
        dm_ok = True
    except discord.Forbidden:
        log.warning(f"Cannot DM {user}")

    note = "✅ SSH sent to DM." if dm_ok else "⚠️ Could not DM — share SSH manually."
    await ix.followup.send(embed=em(
        "✅ VPS Created",
        f"**{vps_id}** is live for {user.mention}\n{note}",
        GREEN,
        fields=[
            ("🆔 VPS ID", vps_id,            True),
            ("👤 Owner",  str(user),          True),
            ("🖥 OS",     os_label,           True),
            ("🧠 RAM",    f"{ram} MB",        True),
            ("💻 CPU",    f"{cpu} Core(s)",   True),
            ("💾 Disk",   f"{disk} GB",       True),
            ("⏰ Expiry", exp_note,           False),
        ],
    ))

    if ix.channel:
        await ix.channel.send(embed=em(
            "🐉 VPS Provisioned",
            f"{user.mention} your **{vps_id}** is ready!\nCheck your **DMs** for the SSH command.",
            BLUE,
            fields=[
                ("🆔 VPS ID", vps_id,          True),
                ("🖥 OS",     os_label,         True),
                ("🧠 RAM",    f"{ram} MB",      True),
                ("💻 CPU",    f"{cpu} Core(s)", True),
                ("💾 Disk",   f"{disk} GB",     True),
            ],
        ))

# ─────────────────────────────────────────────────────
# BOT
# ─────────────────────────────────────────────────────
intents         = discord.Intents.default()
intents.members = True

class DracoHostBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        log.info("Commands synced.")

    async def on_ready(self):
        log.info(f"Online as {self.user}")
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="Powered by DracoHost"))
        if not auto_suspend.is_running():
            auto_suspend.start()

bot = DracoHostBot()

# ─────────────────────────────────────────────────────
# AUTO-SUSPEND TASK
# ─────────────────────────────────────────────────────
@tasks.loop(minutes=15)
async def auto_suspend():
    now = datetime.datetime.utcnow()
    with get_db() as c:
        rows = c.execute(
            "SELECT * FROM vps WHERE expires_at IS NOT NULL AND status!='suspended'"
        ).fetchall()
    for row in rows:
        try:
            if now < datetime.datetime.fromisoformat(row["expires_at"]): continue
        except Exception: continue
        vid = row["vps_id"]
        log.info(f"[{vid}] Auto-suspending.")
        try: get_docker().containers.get(row["container_id"]).stop()
        except Exception: pass
        if PTERO_ON and row["ptero_id"]:
            try: ptero_suspend(row["ptero_id"])
            except Exception as e: log.warning(f"Ptero suspend: {e}")
        with get_db() as c:
            c.execute("UPDATE vps SET status='suspended' WHERE vps_id=?", (vid,))
        try:
            u = await bot.fetch_user(row["owner_id"])
            await u.send(embed=em("⏰ VPS Suspended",
                f"Your VPS **{vid}** has expired and been suspended.\nContact admin to reactivate.",
                YELLOW))
        except Exception: pass

@auto_suspend.before_loop
async def _before(): await bot.wait_until_ready()

# ══════════════════════════════════════════════
# USER COMMANDS
# ══════════════════════════════════════════════

@bot.tree.command(name="start", description="Start your VPS.")
@app_commands.describe(vps_id="e.g. dracohost-vps-0001")
async def cmd_start(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    if row["status"] == "suspended":
        return await ix.followup.send(embed=em("⛔ Suspended", "Contact an admin to reactivate.", YELLOW))
    try:
        get_docker().containers.get(row["container_id"]).start()
        if PTERO_ON and row["ptero_id"]:
            try: ptero_unsuspend(row["ptero_id"])
            except Exception: pass
        with get_db() as c: c.execute("UPDATE vps SET status='running' WHERE vps_id=?", (vps_id,))
        await ix.followup.send(embed=em("✅ Started",
            f"**{vps_id}** is running.\nUse `/regen-ssh {vps_id}` for a fresh SSH link.", GREEN))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="stop", description="Stop your VPS.")
@app_commands.describe(vps_id="e.g. dracohost-vps-0001")
async def cmd_stop(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c: row = c.execute("SELECT container_id FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    try:
        get_docker().containers.get(row["container_id"]).stop()
        with get_db() as c: c.execute("UPDATE vps SET status='stopped' WHERE vps_id=?", (vps_id,))
        await ix.followup.send(embed=em("🛑 Stopped", f"**{vps_id}** stopped.", YELLOW))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="restart", description="Restart your VPS.")
@app_commands.describe(vps_id="e.g. dracohost-vps-0001")
async def cmd_restart(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c: row = c.execute("SELECT container_id,status FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    if row["status"] == "suspended":
        return await ix.followup.send(embed=em("⛔ Suspended", "Contact an admin to reactivate.", YELLOW))
    try:
        get_docker().containers.get(row["container_id"]).restart()
        with get_db() as c: c.execute("UPDATE vps SET status='running' WHERE vps_id=?", (vps_id,))
        await ix.followup.send(embed=em("🔄 Restarted",
            f"**{vps_id}** restarted.\nUse `/regen-ssh {vps_id}` for a fresh SSH link.", GREEN))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="reinstall", description="Reinstall your VPS (same specs, data wiped).")
@app_commands.describe(vps_id="e.g. dracohost-vps-0001")
async def cmd_reinstall(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    await ix.followup.send(embed=em("⏳ Reinstalling...", "~90 seconds...", YELLOW))
    try:
        try: get_docker().containers.get(row["container_id"]).remove(force=True)
        except Exception: pass
        ct, ssh = await asyncio.get_event_loop().run_in_executor(
            None, lambda: provision(vps_id, row["os_image"], row["os_label"],
                                    row["ram_mb"], row["cpu_cores"], row["disk_gb"], row["cpu_name"])
        )
        with get_db() as c:
            c.execute("UPDATE vps SET container_id=?,ssh_cmd=?,status='running' WHERE vps_id=?",
                      (ct.id, ssh, vps_id))
        try:
            dm = await ix.user.create_dm()
            await dm.send(embed=em("🔄 Reinstalled", f"**{vps_id}** rebuilt.", GREEN,
                fields=[("🖥 SSH", f"```{ssh}```" if ssh else "Run `/regen-ssh`", False)]))
        except discord.Forbidden: pass
        await ix.followup.send(embed=em("✅ Reinstalled", f"**{vps_id}** done. Check DMs.", GREEN))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="regen-ssh", description="Get a fresh tmate SSH session.")
@app_commands.describe(vps_id="e.g. dracohost-vps-0001")
async def cmd_regen(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    if row["status"] != "running":
        return await ix.followup.send(embed=em("⚠️ Not Running", f"Start first: `/start {vps_id}`", YELLOW))
    try:
        ct  = get_docker().containers.get(row["container_id"])
        ssh = await asyncio.get_event_loop().run_in_executor(None, lambda: regen_ssh(ct))
        if not ssh:
            return await ix.followup.send(embed=em("⚠️ Not Ready", "Try again in 15 seconds.", YELLOW))
        with get_db() as c: c.execute("UPDATE vps SET ssh_cmd=? WHERE vps_id=?", (ssh, vps_id))
        await ix.followup.send(embed=em(f"🔑 SSH Session — {vps_id}",
            "⚠️ Keep private — anyone with this can access your terminal.",
            GREEN, fields=[("🖥 SSH Command", f"```{ssh}```", False)]))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="vps-performance", description="Live stats for your VPS.")
@app_commands.describe(vps_id="e.g. dracohost-vps-0001")
async def cmd_perf(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied", "That VPS doesn't belong to you.", RED))
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    try:
        ct = get_docker().containers.get(row["container_id"])
        ct.reload()
        if ct.status != "running":
            return await ix.followup.send(embed=em("⚠️ Not Running", f"Start first: `/start {vps_id}`", YELLOW))
        s  = get_stats(ct, row["ram_mb"], row["cpu_cores"])
        dr = ct.exec_run("df -BM / --output=used | tail -1", tty=False)
        du = "N/A"
        if dr.exit_code == 0:
            raw = dr.output.decode().strip().replace("M","").strip()
            try: du = f"{round(int(raw)/1024,2)} GB"
            except Exception: du = raw + " MB"
        pf = [("🦅 Ptero ID", str(row["ptero_id"]), True)] if PTERO_ON and row["ptero_id"] else []
        await ix.followup.send(embed=em("📊 VPS Performance", "", BLUE, fields=[
            ("🆔 VPS ID",    vps_id,                                               True),
            ("🖥 OS",        row["os_label"] or row["os_image"],                   True),
            ("🏷 CPU Model", row["cpu_name"],                                       True),
            ("💻 CPU",       f"{s['cpu']}% of {row['cpu_cores']} Core(s)",        True),
            ("🧠 RAM",       f"{s['mem_mb']} MB / {row['ram_mb']} MB ({s['mem_p']}%)", True),
            ("💾 Disk",      f"{du} / {row['disk_gb']} GB",                        True),
            ("⏱ Uptime",    s["up"],                                               True),
            ("🌐 Net RX",    f"{s['rx']} MB",                                      True),
            ("🌐 Net TX",    f"{s['tx']} MB",                                      True),
            *pf,
        ]))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="my-vps", description="List all your VPS instances.")
async def cmd_my_vps(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    with get_db() as c:
        rows = c.execute("SELECT * FROM vps WHERE owner_id=? ORDER BY vps_id", (ix.user.id,)).fetchall()
    if not rows:
        return await ix.followup.send(embed=em("📋 My VPS", "You have no VPS instances.", YELLOW))
    fields = []
    for r in rows:
        line = (f"OS:`{r['os_label']}` RAM:`{r['ram_mb']}MB` "
                f"CPU:`{r['cpu_cores']}` Disk:`{r['disk_gb']}GB` Status:`{r['status']}`")
        if r["expires_at"]:
            try:
                ts = int(datetime.datetime.fromisoformat(r["expires_at"]).timestamp())
                line += f"\n⏰ Expires: <t:{ts}:R>"
            except Exception: pass
        fields.append((r["vps_id"], line, False))
    await ix.followup.send(embed=em(f"📋 My VPS ({len(rows)})", "", BLUE, fields=fields))


@bot.tree.command(name="commands", description="Show all commands.")
async def cmd_commands(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    u = em("👤 User Commands", "", BLUE, fields=[
        ("`/start <id>`",           "▶️  Start VPS",                      False),
        ("`/stop <id>`",            "⏹️  Stop VPS",                       False),
        ("`/restart <id>`",         "🔄  Restart VPS",                    False),
        ("`/reinstall <id>`",       "🔁  Wipe & reinstall",               False),
        ("`/regen-ssh <id>`",       "🔑  Fresh tmate SSH session",        False),
        ("`/vps-performance <id>`", "📊  Live CPU/RAM/Disk/Net stats",    False),
        ("`/my-vps`",               "📋  List your VPS instances",        False),
        ("`/commands`",             "📖  This help",                      False),
    ])
    a = em("🛡️ Admin Commands", "", RED, fields=[
        ("`/deploy <user>`",                                     "🎛️  1-click deploy",          False),
        ("`/create <user> <ram> <cpu> <disk> <os> <cpu> <days>`","➕  Full param create",       False),
        ("`/admin-add-user <user>`",                             "✅  Grant access",            False),
        ("`/admin-remove-user <user>`",                          "❌  Revoke access",           False),
        ("`/extend-vps <id> <days>`",                            "⏰  Extend/remove expiry",   False),
        ("`/suspend-vps <id>`",                                  "⛔  Suspend VPS",             False),
        ("`/unsuspend-vps <id>`",                                "🔓  Unsuspend VPS",          False),
        ("`/remove-vps <id>`",                                   "🗑️  Delete VPS",             False),
        ("`/fix-vps <id>`",                                      "🔧  Remove stuck container", False),
        ("`/list-vps`",                                          "📋  List all VPS",           False),
        ("`/node-stats`",                                        "🖥️  Host stats",             False),
        ("`/ptero-status`",                                      "🦅  Pterodactyl status",     False),
    ])
    r = em("📖 Reference", "", DARK, fields=[
        ("VPS ID",      "`dracohost-vps-0001`, `dracohost-vps-0002` ...",                 False),
        ("OS",          "`ubuntu20` `ubuntu22` `ubuntu24` `debian11` `debian12`",        False),
        ("CPU",         "`ryzen9` → AMD Ryzen 9 9950X\n`xeon` → Intel Xeon Platinum 8480+", False),
        ("SSH Access",  "tmate SSH only — sent to DM, never public",                     False),
        ("systemctl",   "Full systemd — `systemctl`, services, cron all work",           False),
        ("Pterodactyl", "Syncs when `PTERO_URL` + `PTERO_API_KEY` set in .env",          False),
    ])
    await ix.followup.send(embeds=[u, a, r])

# ══════════════════════════════════════════════
# ADMIN COMMANDS
# ══════════════════════════════════════════════

@bot.tree.command(name="admin-add-user", description="[Admin] Grant hosting access.")
@app_commands.describe(user="User to grant access")
async def cmd_add(ix: discord.Interaction, user: discord.Member):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    with get_db() as c:
        c.execute("INSERT OR IGNORE INTO allowed_users (user_id,added_by) VALUES (?,?)", (user.id, ix.user.id))
    await ix.followup.send(embed=em("✅ Added", f"{user.mention} granted access.", GREEN))


@bot.tree.command(name="admin-remove-user", description="[Admin] Revoke hosting access.")
@app_commands.describe(user="User to revoke")
async def cmd_rm(ix: discord.Interaction, user: discord.Member):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    with get_db() as c: c.execute("DELETE FROM allowed_users WHERE user_id=?", (user.id,))
    await ix.followup.send(embed=em("🗑 Removed", f"{user.mention} access revoked.", YELLOW))


@bot.tree.command(name="create", description="[Admin] Create VPS with full parameters.")
@app_commands.describe(user="Target user", ram="RAM in MB", cpu="CPU cores",
    disk="Disk in GB", os="OS", cpu_name="CPU model", suspend_in_days="Days until auto-suspend (0=never)")
@app_commands.choices(
    os=[
        app_commands.Choice(name="Ubuntu 20.04", value="ubuntu20"),
        app_commands.Choice(name="Ubuntu 22.04", value="ubuntu22"),
        app_commands.Choice(name="Ubuntu 24.04", value="ubuntu24"),
        app_commands.Choice(name="Debian 11",    value="debian11"),
        app_commands.Choice(name="Debian 12",    value="debian12"),
    ],
    cpu_name=[
        app_commands.Choice(name="AMD Ryzen 9 9950X",         value="ryzen9"),
        app_commands.Choice(name="Intel Xeon Platinum 8480+", value="xeon"),
    ],
)
async def cmd_create(ix: discord.Interaction, user: discord.Member, ram: int, cpu: float,
    disk: int, os: app_commands.Choice[str], cpu_name: app_commands.Choice[str],
    suspend_in_days: int = 0):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    await do_create(ix, user, ram, cpu, disk, os.value, cpu_name.value, suspend_in_days)


@bot.tree.command(name="extend-vps", description="[Admin] Extend or remove expiry.")
@app_commands.describe(vps_id="VPS ID", days="Days from now (0 = never)")
async def cmd_extend(ix: discord.Interaction, vps_id: str, days: int):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    vps_id = vps_id.lower()
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    if days <= 0:
        with get_db() as c: c.execute("UPDATE vps SET expires_at=NULL WHERE vps_id=?", (vps_id,))
        return await ix.followup.send(embed=em("✅ Expiry Removed", f"**{vps_id}** never auto-suspends.", GREEN))
    dt = datetime.datetime.utcnow() + datetime.timedelta(days=days)
    with get_db() as c: c.execute("UPDATE vps SET expires_at=? WHERE vps_id=?", (dt.isoformat(), vps_id))
    ts = int(dt.timestamp())
    await ix.followup.send(embed=em("✅ Expiry Set", f"**{vps_id}** auto-suspends <t:{ts}:R>.", GREEN))
    try:
        u = await bot.fetch_user(row["owner_id"])
        await u.send(embed=em("⏰ Expiry Updated", f"Your VPS **{vps_id}** auto-suspends <t:{ts}:R>.", BLUE))
    except Exception: pass


@bot.tree.command(name="suspend-vps", description="[Admin] Suspend a VPS.")
@app_commands.describe(vps_id="VPS ID")
async def cmd_suspend(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    vps_id = vps_id.lower()
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    try: get_docker().containers.get(row["container_id"]).stop()
    except Exception: pass
    if PTERO_ON and row["ptero_id"]:
        try: ptero_suspend(row["ptero_id"])
        except Exception as e: log.warning(f"Ptero suspend: {e}")
    with get_db() as c: c.execute("UPDATE vps SET status='suspended' WHERE vps_id=?", (vps_id,))
    await ix.followup.send(embed=em("⛔ Suspended", f"**{vps_id}** suspended.", YELLOW))


@bot.tree.command(name="unsuspend-vps", description="[Admin] Reactivate a VPS.")
@app_commands.describe(vps_id="VPS ID")
async def cmd_unsuspend(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    vps_id = vps_id.lower()
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    try:
        get_docker().containers.get(row["container_id"]).start()
        if PTERO_ON and row["ptero_id"]:
            try: ptero_unsuspend(row["ptero_id"])
            except Exception as e: log.warning(f"Ptero unsuspend: {e}")
        with get_db() as c: c.execute("UPDATE vps SET status='running' WHERE vps_id=?", (vps_id,))
        await ix.followup.send(embed=em("✅ Unsuspended",
            f"**{vps_id}** is active. User can run `/regen-ssh {vps_id}`.", GREEN))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), RED))


@bot.tree.command(name="remove-vps", description="[Admin] Permanently delete a VPS.")
@app_commands.describe(vps_id="VPS ID")
async def cmd_remove(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    vps_id = vps_id.lower()
    with get_db() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found", f"**{vps_id}** not found.", RED))
    try: get_docker().containers.get(row["container_id"]).remove(force=True)
    except Exception: pass
    if PTERO_ON and row["ptero_id"]:
        try: ptero_remove(row["ptero_id"])
        except Exception as e: log.warning(f"Ptero delete: {e}")
    with get_db() as c: c.execute("DELETE FROM vps WHERE vps_id=?", (vps_id,))
    await ix.followup.send(embed=em("🗑 Deleted", f"**{vps_id}** permanently deleted.", YELLOW))


@bot.tree.command(name="fix-vps", description="[Admin] Force-remove a stuck container.")
@app_commands.describe(vps_id="VPS ID to fix")
async def cmd_fix(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    vps_id  = vps_id.lower()
    removed = False
    try:
        get_docker().containers.get(vps_id).remove(force=True)
        removed = True
        log.info(f"{ix.user} fixed stuck container {vps_id}")
    except docker.errors.NotFound: pass
    except Exception as e:
        return await ix.followup.send(embed=em("❌ Error", str(e), RED))
    with get_db() as c:
        if c.execute("SELECT 1 FROM vps WHERE vps_id=?", (vps_id,)).fetchone():
            c.execute("UPDATE vps SET status='stopped' WHERE vps_id=?", (vps_id,))
    msg = (f"Removed stuck container for **{vps_id}**.\nNow run `/reinstall {vps_id}` or `/create` again."
           if removed else f"No stuck container found for **{vps_id}** — already clean.")
    await ix.followup.send(embed=em("✅ Fixed" if removed else "ℹ️ Clean", msg,
                                    GREEN if removed else BLUE))


@bot.tree.command(name="list-vps", description="[Admin] List all VPS on the node.")
async def cmd_list(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    with get_db() as c: rows = c.execute("SELECT * FROM vps ORDER BY vps_id").fetchall()
    if not rows: return await ix.followup.send(embed=em("📋 All VPS", "None found.", YELLOW))
    fields = []
    for r in rows:
        line = (f"<@{r['owner_id']}> OS:`{r['os_label']}` RAM:`{r['ram_mb']}MB` "
                f"CPU:`{r['cpu_cores']}` Disk:`{r['disk_gb']}GB` Status:`{r['status']}`")
        if PTERO_ON and r["ptero_id"]: line += f" 🦅`{r['ptero_id']}`"
        if r["expires_at"]:
            try:
                ts = int(datetime.datetime.fromisoformat(r["expires_at"]).timestamp())
                line += f" Expires:<t:{ts}:R>"
            except Exception: pass
        fields.append((r["vps_id"], line, False))
    for i in range(0, len(fields), 25):
        await ix.followup.send(embed=em(f"📋 All VPS ({len(rows)}) — Page {i//25+1}", "", BLUE,
                                        fields=fields[i:i+25]))


@bot.tree.command(name="node-stats", description="[Admin] Host node resource usage.")
async def cmd_node(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    dsk = psutil.disk_usage("/")
    try:
        cl = get_docker()
        running = len([c for c in cl.containers.list() if c.status == "running"])
        total   = len(cl.containers.list(all=True))
    except Exception: running = total = 0
    pf = []
    if PTERO_ON:
        s  = ptero_check()
        pf = [("🦅 Pterodactyl",
               f"✅ {s.get('nodes',0)} node(s)" if s["ok"] else f"❌ {s.get('error','Error')}", False)]
    await ix.followup.send(embed=em("🖥️ Node Stats", "", BLUE, fields=[
        ("🖥 Host CPU",    f"{cpu}%",                                                                        True),
        ("🧠 Host RAM",    f"{round(mem.used/1024**3,2)}/{round(mem.total/1024**3,2)} GB ({mem.percent}%)", True),
        ("💾 Host Disk",   f"{gb(dsk.used)}/{gb(dsk.total)} GB ({dsk.percent}%)",                           True),
        ("🐳 Running",     str(running),                                                                     True),
        ("📦 Total",       str(total),                                                                       True),
        *pf,
    ]))


@bot.tree.command(name="ptero-status", description="[Admin] Pterodactyl panel status.")
async def cmd_ptero(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden", "Admin only.", RED))
    if not PTERO_ON:
        return await ix.followup.send(embed=em("🦅 Not Configured",
            "Add `PTERO_URL` and `PTERO_API_KEY` to .env", YELLOW))
    s = ptero_check()
    if s["ok"]:
        try:
            nodes = ptero_get("nodes")
            nl = "\n".join(
                f"• **{n['attributes']['name']}** — `{n['attributes']['fqdn']}`"
                for n in nodes.get("data", [])
            ) or "No nodes."
        except Exception: nl = "Could not fetch nodes."
        await ix.followup.send(embed=em("🦅 Pterodactyl — Connected",
            f"Panel: `{PTERO_URL}`", GREEN, fields=[("Nodes", nl, False)]))
    else:
        await ix.followup.send(embed=em("🦅 Pterodactyl — Error",
            f"Panel: `{PTERO_URL}`\n```{s.get('error','Unknown')}```", RED))

# ══════════════════════════════════════════════
# 1-CLICK DEPLOY
# ══════════════════════════════════════════════

class DeployModal(discord.ui.Modal, title="🐉 DracoHost — Deploy VPS"):
    ram  = discord.ui.TextInput(label="RAM (MB)",  placeholder="512",  default="512", min_length=1, max_length=7)
    cpu  = discord.ui.TextInput(label="CPU Cores", placeholder="1",    default="1",   min_length=1, max_length=5)
    disk = discord.ui.TextInput(label="Disk (GB)", placeholder="10",   default="10",  min_length=1, max_length=5)
    days = discord.ui.TextInput(label="Auto-Suspend After Days (0=never)", placeholder="0", default="0", min_length=1, max_length=4)

    def __init__(self, target: discord.Member, os_key: str, cpu_key: str):
        super().__init__()
        self.target  = target
        self.os_key  = os_key
        self.cpu_key = cpu_key

    async def on_submit(self, ix: discord.Interaction):
        await ix.response.defer(ephemeral=True)
        try:
            ram  = int(self.ram.value.strip())
            cpu  = float(self.cpu.value.strip())
            disk = int(self.disk.value.strip())
            days = int(self.days.value.strip())
        except ValueError:
            return await ix.followup.send(embed=em("❌ Invalid", "All fields must be numbers.", RED))
        await do_create(ix, self.target, ram, cpu, disk, self.os_key, self.cpu_key, days)


class OSView(discord.ui.View):
    def __init__(self, target: discord.Member):
        super().__init__(timeout=120)
        self.target = target

    async def pick(self, ix: discord.Interaction, key: str):
        if not is_admin(ix):
            return await ix.response.send_message(embed=em("⛔ Forbidden", "Admin only.", RED), ephemeral=True)
        _, label = OS_MAP[key]
        await ix.response.edit_message(
            embed=em("🐉 Deploy — Step 2/3", f"**OS:** {label}\n\nChoose **CPU**:", BLUE),
            view=CPUView(self.target, key),
        )

    @discord.ui.button(label="Ubuntu 20.04", style=discord.ButtonStyle.secondary, emoji="🐧", row=0)
    async def u20(self, ix, b): await self.pick(ix, "ubuntu20")
    @discord.ui.button(label="Ubuntu 22.04", style=discord.ButtonStyle.secondary, emoji="🐧", row=0)
    async def u22(self, ix, b): await self.pick(ix, "ubuntu22")
    @discord.ui.button(label="Ubuntu 24.04", style=discord.ButtonStyle.primary,   emoji="🐧", row=0)
    async def u24(self, ix, b): await self.pick(ix, "ubuntu24")
    @discord.ui.button(label="Debian 11",    style=discord.ButtonStyle.secondary, emoji="🌀", row=1)
    async def d11(self, ix, b): await self.pick(ix, "debian11")
    @discord.ui.button(label="Debian 12",    style=discord.ButtonStyle.primary,   emoji="🌀", row=1)
    async def d12(self, ix, b): await self.pick(ix, "debian12")
    @discord.ui.button(label="Cancel",       style=discord.ButtonStyle.danger,    emoji="✖️", row=1)
    async def cancel(self, ix: discord.Interaction, b):
        await ix.response.edit_message(embed=em("❌ Cancelled", "Deployment cancelled.", RED), view=None)


class CPUView(discord.ui.View):
    def __init__(self, target: discord.Member, os_key: str):
        super().__init__(timeout=120)
        self.target = target
        self.os_key = os_key

    async def pick(self, ix: discord.Interaction, key: str):
        if not is_admin(ix):
            return await ix.response.send_message(embed=em("⛔ Forbidden", "Admin only.", RED), ephemeral=True)
        await ix.response.send_modal(DeployModal(self.target, self.os_key, key))

    @discord.ui.button(label="AMD Ryzen 9 9950X",        style=discord.ButtonStyle.danger,   emoji="🔴", row=0)
    async def ryzen(self, ix, b): await self.pick(ix, "ryzen9")
    @discord.ui.button(label="Intel Xeon Platinum 8480+", style=discord.ButtonStyle.primary,  emoji="🔵", row=0)
    async def xeon(self, ix, b):  await self.pick(ix, "xeon")
    @discord.ui.button(label="◀ Back",  style=discord.ButtonStyle.secondary, row=1)
    async def back(self, ix: discord.Interaction, b):
        await ix.response.edit_message(
            embed=em("🐉 Deploy — Step 1/3",
                     f"Deploying for **{self.target.display_name}**\n\nChoose **OS**:", BLUE),
            view=OSView(self.target),
        )
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️", row=1)
    async def cancel(self, ix: discord.Interaction, b):
        await ix.response.edit_message(embed=em("❌ Cancelled", "Deployment cancelled.", RED), view=None)


@bot.tree.command(name="deploy", description="[Admin] 1-click VPS deploy.")
@app_commands.describe(user="User to deploy VPS for")
async def cmd_deploy(ix: discord.Interaction, user: discord.Member):
    if not is_admin(ix):
        return await ix.response.send_message(embed=em("⛔ Forbidden", "Admin only.", RED), ephemeral=True)
    await ix.response.send_message(
        embed=em("🐉 Deploy — Step 1/3",
                 f"Deploying for **{user.display_name}** ({user.mention})\n\nChoose **OS**:", BLUE),
        view=OSView(user), ephemeral=True,
    )

# ─────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.critical("DISCORD_TOKEN not set in .env!")
        raise SystemExit(1)
    if not PTERO_ON:
        log.warning("Pterodactyl not configured — running without panel integration.")
    else:
        log.info(f"Pterodactyl enabled — {PTERO_URL}")
    init_db()
    log.info("Starting DracoHost VPS Manager...")
    bot.run(DISCORD_TOKEN, log_handler=None)
