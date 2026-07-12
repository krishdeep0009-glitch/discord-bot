"""
DracoHost VPS Manager
━━━━━━━━━━━━━━━━━━━━━━━
• Docker containers as VPS (ubuntu/debian official images)
• Pterodactyl Panel + Wings integration
• tmate SSH only (web terminal removed)
• Fake /proc/meminfo + /proc/cpuinfo (neofetch shows correct specs)
• 1-click /deploy (buttons + modal)
• Auto-expiry / suspend system
"""

import os, io, time, tarfile, asyncio, logging, sqlite3, datetime
import discord, docker, psutil, requests
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────
DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN", "")
ADMIN_ROLE_ID  = int(os.getenv("ADMIN_ROLE_ID", "0"))
ADMIN_USER_IDS = {int(x) for x in os.getenv("ADMIN_USER_IDS","").split(",") if x.strip().isdigit()}
PTERO_URL      = os.getenv("PTERO_URL","").rstrip("/")
PTERO_KEY      = os.getenv("PTERO_API_KEY","")
PTERO_ON       = bool(PTERO_URL and PTERO_KEY)

# ──────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("dracohost.log"), logging.StreamHandler()]
)
log = logging.getLogger("DracoHost")

# ──────────────────────────────────────────────────────
# COLOURS & CONSTANTS
# ──────────────────────────────────────────────────────
C_BRAND  = 0x5865F2
C_GREEN  = 0x57F287
C_RED    = 0xED4245
C_YELLOW = 0xFEE75C
C_BLUE   = 0x3498DB
FOOTER   = "Powered by DracoHost"

OS_MAP = {
    "ubuntu20": "ubuntu:20.04",
    "ubuntu22": "ubuntu:22.04",
    "ubuntu24": "ubuntu:24.04",
    "debian11":  "debian:11",
    "debian12":  "debian:12",
}
OS_NAME = {
    "ubuntu20": "Ubuntu 20.04",
    "ubuntu22": "Ubuntu 22.04",
    "ubuntu24": "Ubuntu 24.04",
    "debian11":  "Debian 11",
    "debian12":  "Debian 12",
}
CPU_MAP = {
    "ryzen9": "AMD Ryzen 9 9950X 16-Core Processor",
    "xeon":   "Intel(R) Xeon(R) Platinum 8480+ @ 3.80GHz",
}

DB = "dracohost.db"

# ──────────────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────────────

def dbconn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def db_init():
    with dbconn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id  INTEGER PRIMARY KEY,
                added_by INTEGER,
                added_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS vps (
                vps_id       TEXT PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                container_id TEXT,
                os_image     TEXT,
                os_name      TEXT,
                memory_mb    INTEGER,
                cpu_cores    REAL,
                disk_gb      INTEGER,
                cpu_name     TEXT,
                tmate_ssh    TEXT DEFAULT '',
                ptero_id     INTEGER DEFAULT NULL,
                status       TEXT DEFAULT 'running',
                suspend_at   TEXT DEFAULT NULL,
                created_at   TEXT DEFAULT (datetime('now'))
            );
        """)
    log.info("Database ready.")

# ──────────────────────────────────────────────────────
# EMBED HELPER
# ──────────────────────────────────────────────────────

def em(title, desc="", color=C_BRAND, fields=None):
    e = discord.Embed(title=title, description=desc, color=color,
                      timestamp=datetime.datetime.utcnow())
    e.set_footer(text=FOOTER)
    for n,v,i in (fields or []):
        e.add_field(name=n, value=v, inline=i)
    return e

# ──────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────

def dclient(): return docker.from_env()

def is_admin(ix: discord.Interaction) -> bool:
    if ix.user.id in ADMIN_USER_IDS: return True
    if ix.guild: return any(r.id == ADMIN_ROLE_ID for r in ix.user.roles)
    return False

def owns(uid: int, vid: str) -> bool:
    with dbconn() as c:
        return bool(c.execute("SELECT 1 FROM vps WHERE vps_id=? AND user_id=?", (vid,uid)).fetchone())

def next_vps_id() -> str:
    with dbconn() as c:
        row = c.execute("SELECT vps_id FROM vps ORDER BY vps_id DESC LIMIT 1").fetchone()
    db_num = 1 if not row else int(row["vps_id"].split("-")[-1]) + 1
    dk_max = 0
    try:
        for ct in dclient().containers.list(all=True, filters={"label":"managed-by=dracohost"}):
            if ct.name.startswith("dracohost-vps-"):
                try: dk_max = max(dk_max, int(ct.name.split("-")[-1]))
                except: pass
    except: pass
    return f"dracohost-vps-{max(db_num, dk_max+1):04d}"

def gb(b): return round(b/1024**3, 2)

# ──────────────────────────────────────────────────────
# PTERODACTYL API
# ──────────────────────────────────────────────────────

def _ph():
    return {"Authorization": f"Bearer {PTERO_KEY}",
            "Accept": "application/json", "Content-Type": "application/json"}

def ptero_get(ep):
    r = requests.get(f"{PTERO_URL}/api/application/{ep}", headers=_ph(), timeout=10)
    r.raise_for_status(); return r.json()

def ptero_post(ep, data=None):
    r = requests.post(f"{PTERO_URL}/api/application/{ep}", headers=_ph(), json=data or {}, timeout=10)
    r.raise_for_status(); return r.json() if r.text else {}

def ptero_delete(ep):
    r = requests.delete(f"{PTERO_URL}/api/application/{ep}", headers=_ph(), timeout=10)
    r.raise_for_status()

def ptero_check():
    try:
        n = ptero_get("nodes")
        return {"ok": True, "nodes": len(n.get("data",[]))}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def ptero_suspend(pid):   ptero_post(f"servers/{pid}/suspend")
def ptero_unsuspend(pid): ptero_post(f"servers/{pid}/unsuspend")
def ptero_remove(pid):    ptero_delete(f"servers/{pid}/force")

# ──────────────────────────────────────────────────────
# FAKE /proc GENERATORS
# ──────────────────────────────────────────────────────

def fake_mem(mb: int) -> str:
    kb = mb * 1024
    return "\n".join([
        f"MemTotal:       {kb} kB",
        f"MemFree:        {int(kb*.88)} kB",
        f"MemAvailable:   {int(kb*.85)} kB",
        f"Buffers:            128 kB",
        f"Cached:         {int(kb*.05)} kB",
        f"SwapCached:           0 kB",
        f"Active:         {int(kb*.10)} kB",
        f"Inactive:       {int(kb*.02)} kB",
        f"SwapTotal:            0 kB",
        f"SwapFree:             0 kB",
        f"Dirty:                4 kB",
        f"Writeback:            0 kB",
        f"AnonPages:      {int(kb*.08)} kB",
        f"Mapped:         {int(kb*.02)} kB",
        f"Shmem:               64 kB",
        f"Slab:               512 kB",
        f"VmallocTotal:   {kb} kB",
        f"VmallocUsed:          0 kB",
        f"VmallocChunk:   {kb} kB",
        f"HugePages_Total:      0",
        f"HugePages_Free:       0",
        f"Hugepagesize:      2048 kB", "",
    ])

def fake_cpu(cores: float, name: str) -> str:
    n = max(1, int(cores))
    v = "AuthenticAMD" if ("AMD" in name or "Ryzen" in name) else "GenuineIntel"
    return "\n".join("\n".join([
        f"processor\t: {i}", f"vendor_id\t: {v}",
        f"cpu family\t: 25", f"model\t\t: 97",
        f"model name\t: {name}", f"stepping\t: 2",
        f"cpu MHz\t\t: 4200.000", f"cache size\t: 65536 KB",
        f"physical id\t: 0", f"siblings\t: {n}",
        f"core id\t\t: {i}", f"cpu cores\t: {n}",
        f"fpu\t\t: yes", f"fpu_exception\t: yes",
        f"cpuid level\t: 16", f"wp\t\t: yes",
        f"bogomips\t: 8400.00", f"clflush size\t: 64",
        f"cache_alignment\t: 64",
        f"address sizes\t: 48 bits physical, 48 bits virtual", "",
    ]) for i in range(n))

# ──────────────────────────────────────────────────────
# PUT FILE INTO CONTAINER
# ──────────────────────────────────────────────────────

def put(ct, path: str, content: str):
    data = content.encode()
    buf  = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        ti = tarfile.TarInfo(name=os.path.basename(path))
        ti.size = len(data); ti.mode = 0o644
        tar.addfile(ti, io.BytesIO(data))
    buf.seek(0)
    ct.put_archive(os.path.dirname(path) or "/", buf)

# ──────────────────────────────────────────────────────
# PROVISION VPS
# ──────────────────────────────────────────────────────

def provision(vps_id, image, memory_mb, cpu_cores, disk_gb, cpu_name) -> tuple:
    """
    Provisions a Docker VPS container:
    ✅ Official ubuntu/debian images — lightweight, no cgroup issues
    ✅ --privileged        — needed for /proc bind mounts
    ✅ Exact RAM/CPU/Disk  — hard limits enforced
    ✅ Fake /proc/meminfo + /proc/cpuinfo — neofetch shows correct specs
    ✅ tmate SSH only      — web terminal removed
    Returns (container, ssh_cmd)
    """
    client = dclient()
    mem    = f"{memory_mb}m"
    period = 100_000
    quota  = int(period * cpu_cores)

    log.info(f"[{vps_id}] Provisioning RAM:{memory_mb}MB CPU:{cpu_cores} Disk:{disk_gb}GB")

    # ── Remove any leftover container ───────────────────────────────
    try:
        for old in client.containers.list(all=True, filters={"name": f"^/{vps_id}$"}):
            log.warning(f"[{vps_id}] Removing leftover {old.short_id} ({old.status})")
            try: old.remove(force=True, v=True)
            except: pass
        for _ in range(10):
            if not client.containers.list(all=True, filters={"name": f"^/{vps_id}$"}): break
            time.sleep(1)
    except Exception as e:
        log.warning(f"[{vps_id}] Cleanup: {e}")

    # ── Pull image first (auto-downloads if not cached) ───────────
    log.info(f"[{vps_id}] Pulling image {image}...")
    try:
        client.images.pull(image)
        log.info(f"[{vps_id}] Image ready: {image}")
    except Exception as pull_err:
        raise RuntimeError(
            f"Failed to pull Docker image `{image}`.\n"
            f"Make sure your server has internet access and Docker Hub is reachable.\n"
            f"Error: {pull_err}"
        )

    # ── Create container ────────────────────────────────────────────
    kw = dict(
        name=vps_id, detach=True, privileged=True, tty=True, stdin_open=True,
        mem_limit=mem, memswap_limit=mem,
        cpu_period=period, cpu_quota=quota,
        environment={"TERM": "xterm-256color", "container": "docker"},
        # Use tail -f /dev/null — works on ALL images without cgroup issues.
        # systemctl is replaced by direct service management via exec_run.
        command="tail -f /dev/null",
        labels={"managed-by":"dracohost","vps-id":vps_id},
    )
    try:
        kw["storage_opt"] = {"size": f"{disk_gb}G"}
        ct = client.containers.run(image, **kw)
        log.info(f"[{vps_id}] Created with disk quota.")
    except Exception as e:
        log.warning(f"[{vps_id}] storage_opt failed ({e}), retrying without.")
        kw.pop("storage_opt", None)
        ct = client.containers.run(image, **kw)

    # ── Small wait for container to fully start ────────────────────
    time.sleep(2)

    # ── apt update ──────────────────────────────────────────────────
    log.info(f"[{vps_id}] apt update...")
    ct.exec_run("bash -c 'apt-get update -qq'", tty=False)

    # ── apt install ─────────────────────────────────────────────────
    log.info(f"[{vps_id}] Installing tmate + neofetch...")
    ct.exec_run(
        "bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tmate neofetch curl wget'",
        tty=False,
    )

    # ── Fake /proc/meminfo ──────────────────────────────────────────
    ct.exec_run("mkdir -p /etc/dracohost", tty=False)
    put(ct, "/etc/dracohost/meminfo", fake_mem(memory_mb))
    ct.exec_run("bash -c 'mount --bind /etc/dracohost/meminfo /proc/meminfo'", tty=False)
    log.info(f"[{vps_id}] /proc/meminfo faked ({memory_mb} MB)")

    # ── Fake /proc/cpuinfo ──────────────────────────────────────────
    put(ct, "/etc/dracohost/cpuinfo", fake_cpu(cpu_cores, cpu_name))
    ct.exec_run("bash -c 'mount --bind /etc/dracohost/cpuinfo /proc/cpuinfo'", tty=False)
    log.info(f"[{vps_id}] /proc/cpuinfo faked ({int(cpu_cores)} vCPU — {cpu_name})")

    # ── rc.local (re-apply on restart) ──────────────────────────────
    put(ct, "/etc/rc.local",
        "#!/bin/bash\n"
        "mount --bind /etc/dracohost/meminfo /proc/meminfo 2>/dev/null\n"
        "mount --bind /etc/dracohost/cpuinfo /proc/cpuinfo 2>/dev/null\n"
        "exit 0\n")
    ct.exec_run("chmod +x /etc/rc.local", tty=False)

    # ── Hostname ────────────────────────────────────────────────────
    ct.exec_run(f"bash -c 'hostnamectl set-hostname {vps_id} 2>/dev/null || hostname {vps_id}'", tty=False)
    ct.exec_run(f"bash -c 'echo {vps_id} > /etc/hostname'", tty=False)

    # ── MOTD ────────────────────────────────────────────────────────
    ci = int(cpu_cores) if cpu_cores == int(cpu_cores) else cpu_cores
    put(ct, "/etc/motd",
        f"\n"
        f"  ╔══════════════════════════════════╗\n"
        f"  ║       🌩  DracoHost VPS         ║\n"
        f"  ╠══════════════════════════════════╣\n"
        f"  ║  VPS ID : {vps_id:<24}║\n"
        f"  ║  RAM    : {str(memory_mb)+' MB':<24}║\n"
        f"  ║  CPU    : {str(ci)+' vCore(s)':<24}║\n"
        f"  ║  Disk   : {str(disk_gb)+' GB':<24}║\n"
        f"  ╚══════════════════════════════════╝\n\n")

    # ── tmate SSH only ──────────────────────────────────────────────
    log.info(f"[{vps_id}] Starting tmate...")
    sock = "/tmp/tmate.sock"
    ct.exec_run(f"bash -c 'rm -f {sock}; tmate -S {sock} new-session -d'", tty=False)
    time.sleep(4)
    ct.exec_run(f"bash -c 'tmate -S {sock} wait tmate-ready'", tty=False)
    r   = ct.exec_run(f"bash -c \"tmate -S {sock} display -p '#{{tmate_ssh}}'\"", tty=False)
    ssh = r.output.decode(errors="ignore").strip() if r.output else ""
    log.info(f"[{vps_id}] SSH: {ssh}")

    return ct, ssh


def regen(ct) -> str:
    sock = "/tmp/tmate.sock"
    ct.exec_run("bash -c 'pkill tmate; rm -f /tmp/tmate.sock'", tty=False)
    time.sleep(2)
    ct.exec_run(f"bash -c 'tmate -S {sock} new-session -d'", tty=False)
    time.sleep(4)
    ct.exec_run(f"bash -c 'tmate -S {sock} wait tmate-ready'", tty=False)
    r = ct.exec_run(f"bash -c \"tmate -S {sock} display -p '#{{tmate_ssh}}'\"", tty=False)
    return r.output.decode(errors="ignore").strip() if r.output else ""


def stats(ct, ram=0, cores=0) -> dict:
    raw = ct.stats(stream=False)
    cd  = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
    sd  = raw["cpu_stats"]["system_cpu_usage"] - raw["precpu_stats"]["system_cpu_usage"]
    nc  = raw["cpu_stats"].get("online_cpus", 1)
    rp  = (cd/sd)*nc*100 if sd else 0
    cpu = round(min(rp/cores,100),2) if cores else round(rp,2)
    mu  = raw["memory_stats"].get("usage", 0)
    ml  = ram*1024*1024 if ram else 1
    rx = tx = 0
    for iface in raw.get("networks",{}).values():
        rx += iface.get("rx_bytes",0); tx += iface.get("tx_bytes",0)
    started = ct.attrs["State"].get("StartedAt","")
    up = "N/A"
    if started and started != "0001-01-01T00:00:00Z":
        try:
            s = datetime.datetime.fromisoformat(started.replace("Z","+00:00"))
            d = datetime.datetime.now(datetime.timezone.utc) - s
            h,r2 = divmod(int(d.total_seconds()),3600); m,s2 = divmod(r2,60)
            up = f"{h}h {m}m {s2}s"
        except: pass
    return {"cpu":cpu,"mem_mb":round(mu/1024/1024,1),
            "mem_p":round(min(mu/ml*100,100),2),
            "rx":round(rx/1024/1024,2),"tx":round(tx/1024/1024,2),"up":up}

# ──────────────────────────────────────────────────────
# SHARED CREATE LOGIC
# ──────────────────────────────────────────────────────

async def do_create(ix, user, memory, cpu, disk, os_key, cpu_key, suspend_days=0):
    image    = OS_MAP[os_key]
    os_name  = OS_NAME[os_key]
    cpu_name = CPU_MAP[cpu_key]
    vps_id   = next_vps_id()

    sus_at   = None
    sus_note = "Never expires"
    if suspend_days > 0:
        dt       = datetime.datetime.utcnow() + datetime.timedelta(days=suspend_days)
        sus_at   = dt.isoformat()
        sus_note = f"Auto-suspends <t:{int(dt.timestamp())}:R>"

    await ix.followup.send(embed=em(
        "⏳ Provisioning VPS...",
        f"**{vps_id}** for {user.mention}\n\n"
        "```\n"
        "[1/3] apt update                  ⏳\n"
        "[2/3] apt install tmate neofetch  ⏳\n"
        "[3/3] Starting tmate              ⏳\n"
        "```\n"
        "~60 seconds — SSH credentials will be sent to DM.",
        C_BLUE,
        fields=[
            ("OS",        os_name,          True),
            ("RAM",       f"{memory} MB",   True),
            ("CPU",       f"{cpu} Core(s)", True),
            ("Disk",      f"{disk} GB",     True),
            ("CPU Model", cpu_name,         False),
            ("⏰ Expiry", sus_note,          False),
        ],
    ))

    try:
        ct, ssh = await asyncio.get_event_loop().run_in_executor(
            None, lambda: provision(vps_id, image, memory, cpu, disk, cpu_name)
        )
    except Exception as e:
        log.error(f"[{vps_id}] Provision failed: {e}")
        try: dclient().containers.get(vps_id).remove(force=True, v=True)
        except: pass
        return await ix.followup.send(embed=em(
            "❌ Provisioning Failed",
            f"**{vps_id}** could not be created.\n```{str(e)[:500]}```\n"
            f"Run `/fix-vps {vps_id}` then try again.",
            C_RED,
        ))

    with dbconn() as c:
        c.execute("""
            INSERT INTO vps (vps_id,user_id,container_id,os_image,os_name,
              memory_mb,cpu_cores,disk_gb,cpu_name,tmate_ssh,status,suspend_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,'running',?)
        """, (vps_id, user.id, ct.id, image, os_name, memory, cpu, disk, cpu_name, ssh, sus_at))

    log.info(f"Created {vps_id} for {user} by {ix.user}")

    # DM credentials
    dm_ok = False
    try:
        fields = [
            ("🆔 VPS ID",    vps_id,           True),
            ("🖥 OS",        os_name,           True),
            ("🧠 RAM",       f"{memory} MB",    True),
            ("💻 CPU",       f"{cpu} Core(s)",  True),
            ("💾 Disk",      f"{disk} GB",      True),
            ("🏷 CPU Model", cpu_name,          True),
        ]
        if sus_at: fields.append(("⏰ Expiry", sus_note, False))
        fields.append(("🖥 SSH Command",
            f"```{ssh}```" if ssh else f"Run `/regen-ssh {vps_id}` in 30 seconds.", False))
        dm = await user.create_dm()
        await dm.send(embed=em(
            "🎉 Your VPS is Ready — DracoHost",
            "⚠️ **Keep the SSH command private.**\nAnyone with it can access your terminal.",
            C_GREEN, fields=fields,
        ))
        dm_ok = True
    except discord.Forbidden:
        log.warning(f"Cannot DM {user}")

    note = "✅ Credentials sent to DM." if dm_ok else "⚠️ Could not DM user — share credentials manually."
    await ix.followup.send(embed=em(
        "✅ VPS Created",
        f"**{vps_id}** is live for {user.mention}.\n{note}",
        C_GREEN,
        fields=[
            ("🆔 VPS ID", vps_id,          True),
            ("👤 Owner",  str(user),        True),
            ("🖥 OS",     os_name,          True),
            ("🧠 RAM",    f"{memory} MB",   True),
            ("💻 CPU",    f"{cpu} Core(s)", True),
            ("💾 Disk",   f"{disk} GB",     True),
            ("⏰ Expiry", sus_note,         False),
        ],
    ))

    if ix.channel:
        await ix.channel.send(embed=em(
            "🌩️ VPS Provisioned",
            f"{user.mention} your **{vps_id}** is ready!\nCheck your **DMs** for the SSH command.",
            C_BRAND,
            fields=[
                ("🆔 VPS ID", vps_id,          True),
                ("🖥 OS",     os_name,          True),
                ("🧠 RAM",    f"{memory} MB",   True),
                ("💻 CPU",    f"{cpu} Core(s)", True),
                ("💾 Disk",   f"{disk} GB",     True),
            ],
        ))

# ──────────────────────────────────────────────────────
# BOT
# ──────────────────────────────────────────────────────

intents         = discord.Intents.default()
intents.members = True

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
    async def setup_hook(self):
        await self.tree.sync()
        log.info("Commands synced.")
    async def on_ready(self):
        log.info(f"Online as {self.user}")
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="Powered by DracoHost"))
        if not auto_suspend.is_running(): auto_suspend.start()

bot = Bot()

# ──────────────────────────────────────────────────────
# AUTO-SUSPEND TASK
# ──────────────────────────────────────────────────────

@tasks.loop(minutes=15)
async def auto_suspend():
    now = datetime.datetime.utcnow()
    with dbconn() as c:
        rows = c.execute(
            "SELECT * FROM vps WHERE suspend_at IS NOT NULL AND status!='suspended'"
        ).fetchall()
    for row in rows:
        try:
            if now < datetime.datetime.fromisoformat(row["suspend_at"]): continue
        except: continue
        vid = row["vps_id"]
        log.info(f"[{vid}] Auto-suspending.")
        try: dclient().containers.get(row["container_id"]).stop()
        except: pass
        if PTERO_ON and row["ptero_id"]:
            try: ptero_suspend(row["ptero_id"])
            except Exception as e: log.warning(f"[{vid}] Ptero suspend: {e}")
        with dbconn() as c:
            c.execute("UPDATE vps SET status='suspended' WHERE vps_id=?", (vid,))
        try:
            u = await bot.fetch_user(row["user_id"])
            await u.send(embed=em("⏰ VPS Auto-Suspended",
                f"Your VPS **{vid}** has expired and been suspended.\n"
                "Contact an admin to reactivate.", C_YELLOW))
        except: pass

@auto_suspend.before_loop
async def _before(): await bot.wait_until_ready()

# ══════════════════════════════════════════════════════
# USER COMMANDS
# ══════════════════════════════════════════════════════

@bot.tree.command(name="start", description="Start your VPS.")
@app_commands.describe(vps_id="e.g. dracohost-vps-0001")
async def cmd_start(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied","That VPS doesn't belong to you.",C_RED))
    with dbconn() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found",f"**{vps_id}** not found.",C_RED))
    if row["status"] == "suspended":
        return await ix.followup.send(embed=em("⛔ Suspended","Contact an admin to reactivate.",C_YELLOW))
    try:
        dclient().containers.get(row["container_id"]).start()
        if PTERO_ON and row["ptero_id"]:
            try: ptero_unsuspend(row["ptero_id"])
            except: pass
        with dbconn() as c: c.execute("UPDATE vps SET status='running' WHERE vps_id=?", (vps_id,))
        await ix.followup.send(embed=em("✅ Started",
            f"**{vps_id}** is running.\nUse `/regen-ssh {vps_id}` for a fresh SSH link.",C_GREEN))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), C_RED))


@bot.tree.command(name="stop", description="Stop your VPS.")
@app_commands.describe(vps_id="e.g. dracohost-vps-0001")
async def cmd_stop(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied","That VPS doesn't belong to you.",C_RED))
    with dbconn() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found",f"**{vps_id}** not found.",C_RED))
    try:
        dclient().containers.get(row["container_id"]).stop()
        with dbconn() as c: c.execute("UPDATE vps SET status='stopped' WHERE vps_id=?", (vps_id,))
        await ix.followup.send(embed=em("🛑 Stopped",f"**{vps_id}** stopped.",C_YELLOW))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), C_RED))


@bot.tree.command(name="restart", description="Restart your VPS.")
@app_commands.describe(vps_id="e.g. dracohost-vps-0001")
async def cmd_restart(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied","That VPS doesn't belong to you.",C_RED))
    with dbconn() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found",f"**{vps_id}** not found.",C_RED))
    if row["status"] == "suspended":
        return await ix.followup.send(embed=em("⛔ Suspended","Contact an admin.",C_YELLOW))
    try:
        dclient().containers.get(row["container_id"]).restart()
        with dbconn() as c: c.execute("UPDATE vps SET status='running' WHERE vps_id=?", (vps_id,))
        await ix.followup.send(embed=em("🔄 Restarted",
            f"**{vps_id}** restarted.\nUse `/regen-ssh {vps_id}` for a fresh SSH link.",C_GREEN))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), C_RED))


@bot.tree.command(name="reinstall", description="Reinstall your VPS (same specs, data wiped).")
@app_commands.describe(vps_id="e.g. dracohost-vps-0001")
async def cmd_reinstall(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied","That VPS doesn't belong to you.",C_RED))
    with dbconn() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found",f"**{vps_id}** not found.",C_RED))
    await ix.followup.send(embed=em("⏳ Reinstalling...","~60 seconds...",C_YELLOW))
    try:
        try: dclient().containers.get(row["container_id"]).remove(force=True)
        except: pass
        ct, ssh = await asyncio.get_event_loop().run_in_executor(
            None, lambda: provision(vps_id, row["os_image"], row["memory_mb"],
                                    row["cpu_cores"], row["disk_gb"], row["cpu_name"]))
        with dbconn() as c:
            c.execute("UPDATE vps SET container_id=?,tmate_ssh=?,status='running' WHERE vps_id=?",
                      (ct.id, ssh, vps_id))
        try:
            dm = await ix.user.create_dm()
            await dm.send(embed=em("🔄 Reinstalled",f"**{vps_id}** is fresh and ready.",C_GREEN,
                fields=[("🖥 SSH Command",f"```{ssh}```" if ssh else "Run `/regen-ssh`",False)]))
        except discord.Forbidden: pass
        await ix.followup.send(embed=em("✅ Reinstalled",f"**{vps_id}** reinstalled. Check your DMs.",C_GREEN))
    except Exception as e:
        log.error(f"reinstall {vps_id}: {e}")
        await ix.followup.send(embed=em("❌ Error", str(e), C_RED))


@bot.tree.command(name="regen-ssh", description="Get a fresh tmate SSH session for your VPS.")
@app_commands.describe(vps_id="e.g. dracohost-vps-0001")
async def cmd_regen(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied","That VPS doesn't belong to you.",C_RED))
    with dbconn() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found",f"**{vps_id}** not found.",C_RED))
    if row["status"] != "running":
        return await ix.followup.send(embed=em("⚠️ Not Running",f"Start it first: `/start {vps_id}`",C_YELLOW))
    try:
        ct  = dclient().containers.get(row["container_id"])
        ssh = await asyncio.get_event_loop().run_in_executor(None, lambda: regen(ct))
        if not ssh:
            return await ix.followup.send(embed=em("⚠️ Not Ready","Try again in 15 seconds.",C_YELLOW))
        with dbconn() as c: c.execute("UPDATE vps SET tmate_ssh=? WHERE vps_id=?", (ssh, vps_id))
        await ix.followup.send(embed=em(
            f"🖥 SSH Session — {vps_id}",
            "⚠️ Keep this private — anyone with it can access your terminal.",
            C_GREEN,
            fields=[("🖥 SSH Command",f"```{ssh}```",False)],
        ))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), C_RED))


@bot.tree.command(name="vps-performance", description="Show live stats for your VPS.")
@app_commands.describe(vps_id="e.g. dracohost-vps-0001")
async def cmd_perf(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    vps_id = vps_id.lower()
    if not owns(ix.user.id, vps_id):
        return await ix.followup.send(embed=em("❌ Access Denied","That VPS doesn't belong to you.",C_RED))
    with dbconn() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found",f"**{vps_id}** not found.",C_RED))
    try:
        ct = dclient().containers.get(row["container_id"])
        ct.reload()
        if ct.status != "running":
            return await ix.followup.send(embed=em("⚠️ Not Running",f"Start first: `/start {vps_id}`",C_YELLOW))
        s  = stats(ct, row["memory_mb"], row["cpu_cores"])
        dr = ct.exec_run("df -BM / --output=used | tail -1", tty=False)
        du = "N/A"
        if dr.exit_code == 0:
            raw = dr.output.decode().strip().replace("M","").strip()
            try: du = f"{round(int(raw)/1024,2)} GB"
            except: du = raw + " MB"
        pf = [("🦅 Pterodactyl ID", str(row["ptero_id"]), True)] if PTERO_ON and row["ptero_id"] else []
        await ix.followup.send(embed=em("📊 VPS Performance","",C_BLUE,fields=[
            ("🆔 VPS ID",    vps_id,                                              True),
            ("🖥 OS",        row["os_name"] or row["os_image"],                   True),
            ("🏷 CPU Model", row["cpu_name"],                                     True),
            ("💻 CPU",       f"{s['cpu']}% of {row['cpu_cores']} Core(s)",       True),
            ("🧠 RAM",       f"{s['mem_mb']} MB / {row['memory_mb']} MB ({s['mem_p']}%)", True),
            ("💾 Disk",      f"{du} / {row['disk_gb']} GB",                      True),
            ("⏱ Uptime",    s["up"],                                              True),
            ("🌐 Net RX",    f"{s['rx']} MB",                                     True),
            ("🌐 Net TX",    f"{s['tx']} MB",                                     True),
            *pf,
        ]))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), C_RED))


@bot.tree.command(name="my-vps", description="List all your VPS instances.")
async def cmd_my_vps(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    with dbconn() as c:
        rows = c.execute("SELECT * FROM vps WHERE user_id=? ORDER BY vps_id", (ix.user.id,)).fetchall()
    if not rows:
        return await ix.followup.send(embed=em("📋 My VPS","You have no VPS instances.",C_YELLOW))
    fields = []
    for r in rows:
        line = (f"OS: `{r['os_name'] or r['os_image']}` | RAM: `{r['memory_mb']}MB` | "
                f"CPU: `{r['cpu_cores']}` | Disk: `{r['disk_gb']}GB` | Status: `{r['status']}`")
        if r["suspend_at"]:
            try:
                ts = int(datetime.datetime.fromisoformat(r["suspend_at"]).timestamp())
                line += f"\n⏰ Expires: <t:{ts}:R>"
            except: pass
        fields.append((r["vps_id"], line, False))
    await ix.followup.send(embed=em(f"📋 My VPS ({len(rows)})", "", C_BLUE, fields=fields))


@bot.tree.command(name="commands", description="Show all DracoHost commands.")
async def cmd_help(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    u = em("👤 User Commands","Available to all VPS owners.",C_BRAND,fields=[
        ("`/start <id>`",           "▶️  Start your VPS",                    False),
        ("`/stop <id>`",            "⏹️  Stop your VPS",                     False),
        ("`/restart <id>`",         "🔄  Restart your VPS",                  False),
        ("`/reinstall <id>`",       "🔁  Wipe & reinstall (same specs)",     False),
        ("`/regen-ssh <id>`",       "🔑  Get fresh tmate SSH session",       False),
        ("`/vps-performance <id>`", "📊  Live CPU, RAM, Disk, Uptime stats", False),
        ("`/my-vps`",               "📋  List all your VPS instances",       False),
        ("`/commands`",             "📖  Show this help",                    False),
    ])
    a = em("🛡️ Admin Commands","Requires Admin Role or Admin User ID.",C_RED,fields=[
        ("`/deploy <user>`",                                      "🎛️  1-click deploy (buttons + modal)",    False),
        ("`/create <user> <mem> <cpu> <disk> <os> <cpu> <days>`","➕  Full param VPS creation",             False),
        ("`/admin-add-user <user>`",                              "✅  Grant hosting access",               False),
        ("`/admin-remove-user <user>`",                           "❌  Revoke hosting access",              False),
        ("`/extend-vps <id> <days>`",                             "⏰  Extend or remove auto-suspend",      False),
        ("`/suspend-vps <id>`",                                   "⛔  Stop & lock a VPS",                  False),
        ("`/unsuspend-vps <id>`",                                 "🔓  Reactivate a suspended VPS",        False),
        ("`/remove-vps <id>`",                                    "🗑️  Permanently delete a VPS",          False),
        ("`/fix-vps <id>`",                                       "🔧  Force-remove a stuck container",    False),
        ("`/list-vps`",                                           "📋  List all VPS on the node",          False),
        ("`/node-stats`",                                         "🖥️  Host CPU, RAM, Disk & containers",  False),
        ("`/ptero-status`",                                       "🦅  Pterodactyl panel connection",      False),
    ])
    r = em("📖 Reference","",C_BLUE,fields=[
        ("OS Templates",    "`ubuntu20` `ubuntu22` `ubuntu24` `debian11` `debian12`",              False),
        ("CPU Types",       "`ryzen9` → AMD Ryzen 9 9950X\n`xeon` → Intel Xeon Platinum 8480+",  False),
        ("VPS ID Format",   "`dracohost-vps-0001`, `dracohost-vps-0002` ...",                 False),
        ("Terminal Access", "SSH via tmate only — sent to DM, never public",                      False),
        ("systemctl",       "Install and run services with `apt install` inside your VPS",        False),
        ("Pterodactyl",     "Syncs suspend/unsuspend/delete with panel when configured",          False),
    ])
    await ix.followup.send(embeds=[u, a, r])

# ══════════════════════════════════════════════════════
# ADMIN COMMANDS
# ══════════════════════════════════════════════════════

@bot.tree.command(name="admin-add-user", description="[Admin] Grant a user hosting access.")
@app_commands.describe(user="User to grant access")
async def cmd_adduser(ix: discord.Interaction, user: discord.Member):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden","Admin only.",C_RED))
    with dbconn() as c:
        c.execute("INSERT OR IGNORE INTO users (user_id,added_by) VALUES (?,?)", (user.id, ix.user.id))
    await ix.followup.send(embed=em("✅ Added",f"{user.mention} granted hosting access.",C_GREEN))


@bot.tree.command(name="admin-remove-user", description="[Admin] Revoke a user's hosting access.")
@app_commands.describe(user="User to revoke")
async def cmd_rmuser(ix: discord.Interaction, user: discord.Member):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden","Admin only.",C_RED))
    with dbconn() as c: c.execute("DELETE FROM users WHERE user_id=?", (user.id,))
    await ix.followup.send(embed=em("🗑 Removed",f"{user.mention}'s access revoked.",C_YELLOW))


@bot.tree.command(name="create", description="[Admin] Create a VPS with full parameters.")
@app_commands.describe(user="Target user", memory="RAM in MB", cpu="CPU cores",
    disk="Disk in GB", os="OS template", cpu_name="CPU type",
    suspend_in_days="Auto-suspend after N days (0 = never)")
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
async def cmd_create(ix: discord.Interaction, user: discord.Member, memory: int,
    cpu: float, disk: int, os: app_commands.Choice[str],
    cpu_name: app_commands.Choice[str], suspend_in_days: int = 0):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden","Admin only.",C_RED))
    await do_create(ix, user, memory, cpu, disk, os.value, cpu_name.value, suspend_in_days)


@bot.tree.command(name="extend-vps", description="[Admin] Extend or remove auto-suspend expiry.")
@app_commands.describe(vps_id="VPS ID", days="Days from now (0 = never expires)")
async def cmd_extend(ix: discord.Interaction, vps_id: str, days: int):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden","Admin only.",C_RED))
    vps_id = vps_id.lower()
    with dbconn() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found",f"**{vps_id}** not found.",C_RED))
    if days <= 0:
        with dbconn() as c: c.execute("UPDATE vps SET suspend_at=NULL WHERE vps_id=?", (vps_id,))
        return await ix.followup.send(embed=em("✅ Expiry Removed",f"**{vps_id}** will never auto-suspend.",C_GREEN))
    dt = datetime.datetime.utcnow() + datetime.timedelta(days=days)
    with dbconn() as c: c.execute("UPDATE vps SET suspend_at=? WHERE vps_id=?", (dt.isoformat(), vps_id))
    ts = int(dt.timestamp())
    await ix.followup.send(embed=em("✅ Expiry Set",f"**{vps_id}** auto-suspends <t:{ts}:R>.",C_GREEN))
    try:
        u = await bot.fetch_user(row["user_id"])
        await u.send(embed=em("⏰ Expiry Updated",f"Your VPS **{vps_id}** will auto-suspend <t:{ts}:R>.",C_BLUE))
    except: pass


@bot.tree.command(name="suspend-vps", description="[Admin] Suspend a VPS.")
@app_commands.describe(vps_id="VPS ID to suspend")
async def cmd_suspend(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden","Admin only.",C_RED))
    vps_id = vps_id.lower()
    with dbconn() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found",f"**{vps_id}** not found.",C_RED))
    try: dclient().containers.get(row["container_id"]).stop()
    except: pass
    if PTERO_ON and row["ptero_id"]:
        try: ptero_suspend(row["ptero_id"])
        except Exception as e: log.warning(f"Ptero suspend: {e}")
    with dbconn() as c: c.execute("UPDATE vps SET status='suspended' WHERE vps_id=?", (vps_id,))
    await ix.followup.send(embed=em("⛔ Suspended",f"**{vps_id}** suspended.",C_YELLOW))


@bot.tree.command(name="unsuspend-vps", description="[Admin] Reactivate a suspended VPS.")
@app_commands.describe(vps_id="VPS ID to unsuspend")
async def cmd_unsuspend(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden","Admin only.",C_RED))
    vps_id = vps_id.lower()
    with dbconn() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found",f"**{vps_id}** not found.",C_RED))
    try:
        dclient().containers.get(row["container_id"]).start()
        if PTERO_ON and row["ptero_id"]:
            try: ptero_unsuspend(row["ptero_id"])
            except Exception as e: log.warning(f"Ptero unsuspend: {e}")
        with dbconn() as c: c.execute("UPDATE vps SET status='running' WHERE vps_id=?", (vps_id,))
        await ix.followup.send(embed=em("✅ Unsuspended",
            f"**{vps_id}** is active. User can run `/regen-ssh {vps_id}`.",C_GREEN))
    except Exception as e:
        await ix.followup.send(embed=em("❌ Error", str(e), C_RED))


@bot.tree.command(name="remove-vps", description="[Admin] Permanently delete a VPS.")
@app_commands.describe(vps_id="VPS ID to delete")
async def cmd_remove(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden","Admin only.",C_RED))
    vps_id = vps_id.lower()
    with dbconn() as c: row = c.execute("SELECT * FROM vps WHERE vps_id=?", (vps_id,)).fetchone()
    if not row: return await ix.followup.send(embed=em("❌ Not Found",f"**{vps_id}** not found.",C_RED))
    try: dclient().containers.get(row["container_id"]).remove(force=True)
    except: pass
    if PTERO_ON and row["ptero_id"]:
        try: ptero_remove(row["ptero_id"])
        except Exception as e: log.warning(f"Ptero delete: {e}")
    with dbconn() as c: c.execute("DELETE FROM vps WHERE vps_id=?", (vps_id,))
    await ix.followup.send(embed=em("🗑 Deleted",f"**{vps_id}** permanently deleted.",C_YELLOW))


@bot.tree.command(name="fix-vps", description="[Admin] Force-remove a stuck/orphaned container.")
@app_commands.describe(vps_id="VPS ID to fix")
async def cmd_fix(ix: discord.Interaction, vps_id: str):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden","Admin only.",C_RED))
    vps_id  = vps_id.lower()
    removed = False
    try:
        dclient().containers.get(vps_id).remove(force=True)
        removed = True
        log.info(f"{ix.user} fixed stuck container {vps_id}")
    except docker.errors.NotFound: pass
    except Exception as e:
        return await ix.followup.send(embed=em("❌ Error", str(e), C_RED))
    with dbconn() as c:
        if c.execute("SELECT 1 FROM vps WHERE vps_id=?", (vps_id,)).fetchone():
            c.execute("UPDATE vps SET status='stopped' WHERE vps_id=?", (vps_id,))
    msg = f"Removed stuck container for **{vps_id}**. Try `/reinstall` or `/create` again." if removed \
          else f"No stuck container found for **{vps_id}** — already clear."
    await ix.followup.send(embed=em("✅ Fixed" if removed else "ℹ️ Nothing Found", msg, C_GREEN if removed else C_BLUE))


@bot.tree.command(name="list-vps", description="[Admin] List all VPS instances on the node.")
async def cmd_list(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden","Admin only.",C_RED))
    with dbconn() as c: rows = c.execute("SELECT * FROM vps ORDER BY vps_id").fetchall()
    if not rows: return await ix.followup.send(embed=em("📋 All VPS","No VPS instances found.",C_YELLOW))
    fields = []
    for r in rows:
        line = (f"<@{r['user_id']}> | OS:`{r['os_name'] or r['os_image']}` | "
                f"RAM:`{r['memory_mb']}MB` | CPU:`{r['cpu_cores']}` | "
                f"Disk:`{r['disk_gb']}GB` | Status:`{r['status']}`")
        if PTERO_ON and r["ptero_id"]: line += f" | 🦅`{r['ptero_id']}`"
        if r["suspend_at"]:
            try:
                ts = int(datetime.datetime.fromisoformat(r["suspend_at"]).timestamp())
                line += f" | Expires:<t:{ts}:R>"
            except: pass
        fields.append((r["vps_id"], line, False))
    for x in range(0, len(fields), 25):
        await ix.followup.send(embed=em(f"📋 All VPS ({len(rows)}) — Page {x//25+1}","",C_BLUE,fields=fields[x:x+25]))


@bot.tree.command(name="node-stats", description="[Admin] Show host node resource usage.")
async def cmd_node(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden","Admin only.",C_RED))
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    dsk = psutil.disk_usage("/")
    try:
        cl = dclient()
        running = len([c for c in cl.containers.list() if c.status == "running"])
        total   = len(cl.containers.list(all=True))
    except: running = total = 0
    pf = []
    if PTERO_ON:
        s  = ptero_check()
        pf = [("🦅 Pterodactyl", f"✅ Connected — {s.get('nodes',0)} node(s)" if s["ok"] else f"❌ {s.get('error','Error')}", False)]
    await ix.followup.send(embed=em("🖥 Node Stats","",C_BLUE,fields=[
        ("🖥 CPU",              f"{cpu}%",                                                                          True),
        ("🧠 RAM",              f"{round(mem.used/1024**3,2)} / {round(mem.total/1024**3,2)} GB ({mem.percent}%)", True),
        ("💾 Disk",             f"{gb(dsk.used)} / {gb(dsk.total)} GB ({dsk.percent}%)",                            True),
        ("🐳 Running",          str(running),                                                                        True),
        ("📦 Total Containers", str(total),                                                                          True),
        *pf,
    ]))


@bot.tree.command(name="ptero-status", description="[Admin] Check Pterodactyl panel connection.")
async def cmd_ptero(ix: discord.Interaction):
    await ix.response.defer(ephemeral=True)
    if not is_admin(ix): return await ix.followup.send(embed=em("⛔ Forbidden","Admin only.",C_RED))
    if not PTERO_ON:
        return await ix.followup.send(embed=em("🦅 Pterodactyl — Not Configured",
            "Add `PTERO_URL` and `PTERO_API_KEY` to your `.env` to enable.", C_YELLOW))
    s = ptero_check()
    if s["ok"]:
        try:
            nodes = ptero_get("nodes")
            nl = "\n".join(
                f"• **{n['attributes']['name']}** — `{n['attributes']['fqdn']}` "
                f"({n['attributes']['memory']}MB RAM / {n['attributes']['disk']}MB Disk)"
                for n in nodes.get("data",[])
            ) or "No nodes found."
        except: nl = "Could not fetch nodes."
        await ix.followup.send(embed=em("🦅 Pterodactyl — Connected",f"Panel: `{PTERO_URL}`",C_GREEN,
            fields=[("Nodes", nl, False)]))
    else:
        await ix.followup.send(embed=em("🦅 Pterodactyl — Error",
            f"Panel: `{PTERO_URL}`\n```{s.get('error','Unknown')}```", C_RED))

# ══════════════════════════════════════════════════════
# 1-CLICK DEPLOY (Buttons + Modal)
# ══════════════════════════════════════════════════════

class DeployModal(discord.ui.Modal, title="🌩️ DracoHost — Deploy VPS"):
    memory = discord.ui.TextInput(label="RAM (MB)",  placeholder="512", default="512", min_length=1, max_length=6)
    cpu    = discord.ui.TextInput(label="CPU Cores", placeholder="1",   default="1",   min_length=1, max_length=4)
    disk   = discord.ui.TextInput(label="Disk (GB)", placeholder="10",  default="10",  min_length=1, max_length=4)
    days   = discord.ui.TextInput(label="Auto-Suspend After Days (0 = never)", placeholder="0", default="0", min_length=1, max_length=4)

    def __init__(self, target: discord.Member, os_key: str, cpu_key: str):
        super().__init__()
        self.target  = target
        self.os_key  = os_key
        self.cpu_key = cpu_key

    async def on_submit(self, ix: discord.Interaction):
        await ix.response.defer(ephemeral=True)
        try:
            mem  = int(self.memory.value.strip())
            cpu  = float(self.cpu.value.strip())
            disk = int(self.disk.value.strip())
            days = int(self.days.value.strip())
        except ValueError:
            return await ix.followup.send(embed=em("❌ Invalid Input","RAM, CPU, Disk, Days must all be numbers.",C_RED))
        await do_create(ix, self.target, mem, cpu, disk, self.os_key, self.cpu_key, days)


class OSView(discord.ui.View):
    def __init__(self, target: discord.Member):
        super().__init__(timeout=120)
        self.target = target

    async def pick(self, ix: discord.Interaction, key: str):
        if not is_admin(ix):
            return await ix.response.send_message(embed=em("⛔ Forbidden","Admin only.",C_RED), ephemeral=True)
        await ix.response.edit_message(
            embed=em("🌩️ Deploy — Step 2/3", f"**OS:** {OS_NAME[key]}\n\nChoose **CPU type**:", C_BRAND),
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
        await ix.response.edit_message(embed=em("❌ Cancelled","Deployment cancelled.",C_RED), view=None)


class CPUView(discord.ui.View):
    def __init__(self, target: discord.Member, os_key: str):
        super().__init__(timeout=120)
        self.target = target
        self.os_key = os_key

    async def pick(self, ix: discord.Interaction, key: str):
        if not is_admin(ix):
            return await ix.response.send_message(embed=em("⛔ Forbidden","Admin only.",C_RED), ephemeral=True)
        await ix.response.send_modal(DeployModal(self.target, self.os_key, key))

    @discord.ui.button(label="AMD Ryzen 9 9950X",        style=discord.ButtonStyle.danger,   emoji="🔴", row=0)
    async def ryzen(self, ix, b): await self.pick(ix, "ryzen9")
    @discord.ui.button(label="Intel Xeon Platinum 8480+", style=discord.ButtonStyle.primary,  emoji="🔵", row=0)
    async def xeon(self, ix, b):  await self.pick(ix, "xeon")
    @discord.ui.button(label="◀ Back",  style=discord.ButtonStyle.secondary, row=1)
    async def back(self, ix: discord.Interaction, b):
        await ix.response.edit_message(
            embed=em("🌩️ Deploy — Step 1/3",
                     f"Deploying for **{self.target.display_name}**\n\nChoose **OS**:", C_BRAND),
            view=OSView(self.target),
        )
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️", row=1)
    async def cancel(self, ix: discord.Interaction, b):
        await ix.response.edit_message(embed=em("❌ Cancelled","Deployment cancelled.",C_RED), view=None)


@bot.tree.command(name="deploy", description="[Admin] 1-click VPS deploy — buttons + modal form.")
@app_commands.describe(user="User to deploy VPS for")
async def cmd_deploy(ix: discord.Interaction, user: discord.Member):
    if not is_admin(ix):
        return await ix.response.send_message(embed=em("⛔ Forbidden","Admin only.",C_RED), ephemeral=True)
    await ix.response.send_message(
        embed=em("🌩️ Deploy — Step 1/3",
                 f"Deploying for **{user.display_name}** ({user.mention})\n\nChoose **OS**:", C_BRAND),
        view=OSView(user), ephemeral=True,
    )

# ──────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.critical("DISCORD_TOKEN not set in .env!")
        raise SystemExit(1)
    if not PTERO_ON:
        log.warning("Pterodactyl not configured — running without panel integration.")
    db_init()
    log.info("Starting DracoHost VPS Manager...")
    bot.run(DISCORD_TOKEN, log_handler=None)
