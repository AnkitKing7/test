import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import json
import os
import subprocess
import re

TOKEN = ""
OWNER_ID = 1405866008127864852   # Only this user can run /create-vps
MACVLAN_LINK = "eth0"  # Host interface for macvlan, change if needed
IP_POOL_FILE = "/var/lib/vps-ip-pool/next_ip.txt"
DB_FILE = "/var/lib/vps-db.json"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------- Utils ---------------- #
async def run_cmd(*args):
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode().strip(), err.decode().strip()


def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r") as f:
        return json.load(f)


def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)


def allocate_ip():
    os.makedirs(os.path.dirname(IP_POOL_FILE), exist_ok=True)
    if not os.path.exists(IP_POOL_FILE):
        try:
            result = subprocess.run(['ip', 'route', 'get', '1'], capture_output=True, text=True, check=True)
            match = re.search(r'src\s+(\d+\.\d+\.\d+\.\d+)', result.stdout)
            if match:
                base = match.group(1)
                parts = base.split('.')
                start_ip = '.'.join(parts[:-1] + ['100'])
                next_ip = '.'.join(parts[:-1] + ['101'])
                with open(IP_POOL_FILE, 'w') as f:
                    f.write(next_ip)
                return start_ip
            else:
                raise ValueError("Could not parse host IP")
        except Exception as e:
            raise ValueError(f"IP auto-detection failed: {e}. Set starting IP manually in {IP_POOL_FILE}")

    with open(IP_POOL_FILE, "r+") as f:
        ip = f.read().strip()
        if not ip:
            raise ValueError("IP pool file is empty. Set starting IP manually.")
        parts = ip.split(".")
        last = int(parts[-1])
        if last >= 254:
            raise ValueError("IP pool exhausted. Expand manually.")
        next_ip = ".".join(parts[:-1] + [str(last + 1)])
        f.seek(0)
        f.write(next_ip)
        f.truncate()
    return ip


async def setup_lxc_config(name: str, ip: str, ram_gb: int = 1, cpu: int = 1):
    config_path = f"/var/lib/lxc/{name}/config"
    if not os.path.exists(config_path):
        raise ValueError("LXC config not found")
    
    gateway = '.'.join(ip.split('.')[:-1]) + '.1'
    memory_limit = ram_gb * 1024 * 1024 * 1024  # bytes
    lines_to_add = [
        "lxc.net.0.type = macvlan",
        f"lxc.net.0.link = {MACVLAN_LINK}",
        "lxc.net.0.name = eth0",
        "lxc.net.0.flags = up",
        f"lxc.net.0.ipv4 = {ip}/24",
        f"lxc.net.0.ipv4.gateway = {gateway}",
        "lxc.net.0.ipv4.dns = 8.8.8.8 1.1.1.1",
        "lxc.cgroup.devices.allow = a",
        "lxc.cap.drop = ",
        "lxc.apparmor.profile = unconfined",
        f"lxc.cgroup2.memory.max = {memory_limit}"
    ]
    
    if cpu > 1:
        lines_to_add.append(f"lxc.cgroup.cpuset.cpus = 0-{cpu-1}")
    else:
        lines_to_add.append("lxc.cgroup.cpuset.cpus = 0")  # Limit to 1 core by default
    
    with open(config_path, 'r') as f:
        existing = f.readlines()
    
    new_config = []
    for line in existing:
        # Skip existing network, cgroup, cap, apparmor lines
        if any(line.startswith(prefix) for prefix in ['lxc.net.', 'lxc.cgroup.', 'lxc.cap.', 'lxc.apparmor.']):
            continue
        new_config.append(line)
    
    new_config.extend([line + '\n' for line in lines_to_add])
    
    with open(config_path, 'w') as f:
        f.writelines(new_config)


async def get_status(vps_name: str):
    code, out, _ = await run_cmd("lxc-info", "-n", vps_name, "-sH")
    if code == 0 and out:
        for line in out.split('\n'):
            if line.startswith("State:"):
                return line.split(":", 1)[1].strip()
    return "unknown"


# ---------------- Change Password Modal ---------------- #
class ChangePasswordModal(discord.ui.Modal, title="🔑 Change VPS Root Password"):
    def __init__(self, vps_name: str):
        super().__init__()
        self.vps_name = vps_name

    new_password = discord.ui.TextInput(label="New Root Password", style=discord.TextStyle.short)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # Ensure container is running
        status = await get_status(self.vps_name)
        if status != "RUNNING":
            await interaction.followup.send(f"❌ VPS must be running to change password. Current status: `{status}`", ephemeral=True)
            return
        code, _, err = await run_cmd(
            "lxc-attach", "-n", self.vps_name, "--",
            "bash", "-lc", f"echo 'root:{self.new_password.value}' | chpasswd"
        )
        if code == 0:
            db = load_db()
            if self.vps_name in db:
                db[self.vps_name]["password"] = self.new_password.value
                save_db(db)
            await interaction.followup.send(f"✅ Password updated for `{self.vps_name}`.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Failed: {err}", ephemeral=True)


# ---------------- Manage View ---------------- #
class ManageView(discord.ui.View):
    def __init__(self, vps_name: str, ip: str, owner_id: int):
        super().__init__(timeout=900)
        self.vps_name = vps_name
        self.ip = ip
        self.owner_id = owner_id

    async def update_embed(self, interaction: discord.Interaction, msg: str = None):
        status = await get_status(self.vps_name)
        db = load_db()
        vps = db.get(self.vps_name, {})
        embed = discord.Embed(
            title=f"⚙️ VPS Manager: {self.vps_name}",
            description="Control your VPS with the buttons below:",
            color=discord.Color.blurple()
        )
        embed.add_field(name="📡 Status", value=f"`{status}`", inline=False)
        embed.add_field(name="💻 SSH", value=f"`ssh root@{self.ip}`", inline=False)
        if vps.get("password"):
            embed.add_field(name="🔑 Root Password", value=f"`{vps['password']}`", inline=False)
        ram_gb = vps.get("ram_gb", 1)
        cpu = vps.get("cpu", 1)
        disk_gb = vps.get("disk_gb", 10)
        embed.add_field(name="🛠️ Resources", value=f"`{ram_gb}GB RAM | {cpu} CPU | {disk_gb}GB Disk`", inline=False)
        embed.set_footer(text="🚀 Powered by PowerDev")

        if msg:
            await interaction.followup.send(msg, ephemeral=True)
        await interaction.message.edit(embed=embed, view=self)

    async def _lxc_action(self, interaction: discord.Interaction, action: str):
        await interaction.response.defer(ephemeral=True)
        if action == "start":
            code, _, err = await run_cmd("lxc-start", "-n", self.vps_name, "-d")
        elif action == "stop":
            code, _, err = await run_cmd("lxc-stop", "-n", self.vps_name, "-t", "30")
        elif action == "restart":
            await run_cmd("lxc-stop", "-n", self.vps_name, "-t", "30")
            code, _, err = await run_cmd("lxc-start", "-n", self.vps_name, "-d")
            if code != 0:
                err = "Restart failed after stop"
        else:
            code, _, err = 1, "", "Unknown action"
        
        if code == 0:
            await self.update_embed(interaction, f"✅ VPS `{self.vps_name}` {action}ed.")
        else:
            await interaction.followup.send(f"❌ Failed to {action}: {err}", ephemeral=True)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._lxc_action(interaction, "start")

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._lxc_action(interaction, "stop")

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.primary)
    async def restart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._lxc_action(interaction, "restart")

    @discord.ui.button(label="Reinstall (Debian 12)", style=discord.ButtonStyle.secondary)
    async def reinstall(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        db = load_db()
        vps = db.get(self.vps_name, {})
        ram_gb = vps.get("ram_gb", 1)
        cpu = vps.get("cpu", 1)
        disk_gb = vps.get("disk_gb", 10)
        
        await run_cmd("lxc-stop", "-n", self.vps_name, "-t", "30")
        await run_cmd("lxc-destroy", "-n", self.vps_name)
        code, _, err = await run_cmd(
            "lxc-create", "-t", "download", "-n", self.vps_name, "--",
            "-d", "debian", "-r", "12", "-a", "amd64", f"-D", f"{disk_gb}G"
        )
        if code == 0:
            await setup_lxc_config(self.vps_name, self.ip, ram_gb, cpu)
            await run_cmd("lxc-start", "-n", self.vps_name, "-d")
            # Password not reset; use Change Password
            if self.vps_name in db:
                db[self.vps_name]["os"] = "debian"
                save_db(db)
            await self.update_embed(interaction, f"🔄 VPS `{self.vps_name}` reinstalled with Debian 12.")
        else:
            await interaction.followup.send(f"❌ Reinstall failed: {err}", ephemeral=True)

    @discord.ui.button(label="Change Password", style=discord.ButtonStyle.blurple)
    async def change_password(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ChangePasswordModal(self.vps_name))

    @discord.ui.button(label="❌ Delete VPS", style=discord.ButtonStyle.red)
    async def delete_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id and interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ You are not allowed.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await run_cmd("lxc-stop", "-n", self.vps_name, "-t", "30")
        await run_cmd("lxc-destroy", "-n", self.vps_name)
        db = load_db()
        db.pop(self.vps_name, None)
        save_db(db)
        await interaction.followup.send(f"🗑️ VPS `{self.vps_name}` deleted.", ephemeral=True)
        await interaction.message.delete()


# ---------------- Commands ---------------- #
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} commands")
    except Exception as e:
        print(f"❌ Sync failed: {e}")


@bot.tree.command(name="create-vps", description="Create a new VPS")
@app_commands.describe(
    name="VPS name",
    password="Root password",
    owner="Owner user",
    os_type="OS type (ubuntu or debian)",
    ram_gb="RAM in GB (default: 1)",
    cpu="CPU cores (default: 1)",
    disk_gb="Disk size in GB (default: 10)"
)
async def create_vps(interaction: discord.Interaction, name: str, password: str, owner: discord.Member,
                     os_type: str = "ubuntu", ram_gb: int = 1, cpu: int = 1, disk_gb: int = 10):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Not allowed.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        ip = allocate_ip()
    except Exception as e:
        await interaction.followup.send(f"❌ IP allocation failed: {e}", ephemeral=True)
        return

    release = "22.04" if os_type == "ubuntu" else "12"
    code, _, err = await run_cmd(
        "lxc-create", "-t", "download", "-n", name, "--",
        "-d", os_type, "-r", release, "-a", "amd64", f"-D", f"{disk_gb}G"
    )
    if code != 0:
        await interaction.followup.send(f"❌ VPS create failed: {err}", ephemeral=True)
        return

    await setup_lxc_config(name, ip, ram_gb, cpu)
    code, _, err = await run_cmd("lxc-start", "-n", name, "-d")
    if code != 0:
        await interaction.followup.send(f"❌ VPS start failed: {err}", ephemeral=True)
        await run_cmd("lxc-destroy", "-n", name)
        return

    # Wait for boot
    await asyncio.sleep(10)

    code, _, err = await run_cmd("lxc-attach", "-n", name, "--", "bash", "-lc", f"echo 'root:{password}' | chpasswd")
    if code != 0:
        await interaction.followup.send(f"⚠️ Password set failed: {err} (but VPS created)", ephemeral=True)

    db = load_db()
    db[name] = {
        "owner_id": owner.id, "ip": ip, "password": password, "name": name,
        "ram_gb": ram_gb, "cpu": cpu, "disk_gb": disk_gb, "os": os_type
    }
    save_db(db)

    try:
        dm = await owner.create_dm()
        embed = discord.Embed(
            title="🌐 Your VPS is Ready!",
            description="Here are your premium server details:",
            color=discord.Color.green()
        )
        embed.add_field(name="🖥️ VPS Name", value=f"`{name}`", inline=False)
        embed.add_field(name="🌍 IP Address", value=f"`{ip}`", inline=False)
        embed.add_field(name="🔑 Root Password", value=f"`{password}`", inline=False)
        embed.add_field(name="💻 SSH Login", value=f"`ssh root@{ip}`", inline=False)
        embed.add_field(name="🛠️ Resources", value=f"`{ram_gb}GB RAM | {cpu} CPU | {disk_gb}GB Disk`", inline=False)
        embed.set_footer(text="🚀 Powered by PowerDev")
        await dm.send(embed=embed)
    except:
        await interaction.followup.send("⚠️ Could not DM owner.", ephemeral=True)

    await interaction.followup.send(f"✅ VPS `{name}` created for {owner.mention} (IP: {ip})", ephemeral=True)


@bot.tree.command(name="manage", description="Manage your VPS")
async def manage(interaction: discord.Interaction, name: str):
    db = load_db()
    vps = db.get(name)
    if not vps:
        await interaction.response.send_message("❌ VPS not found.", ephemeral=True)
        return
    if interaction.user.id != vps["owner_id"]:
        await interaction.response.send_message("❌ You are not the owner.", ephemeral=True)
        return

    status = await get_status(name)
    embed = discord.Embed(
        title=f"⚙️ VPS Manager: {name}",
        description="Control your VPS with the buttons below:",
        color=discord.Color.blurple()
    )
    embed.add_field(name="📡 Status", value=f"`{status}`", inline=False)
    embed.add_field(name="💻 SSH", value=f"`ssh root@{vps['ip']}`", inline=False)
    embed.add_field(name="🔑 Root Password", value=f"`{vps['password']}`", inline=False)
    ram_gb = vps.get("ram_gb", 1)
    cpu = vps.get("cpu", 1)
    disk_gb = vps.get("disk_gb", 10)
    embed.add_field(name="🛠️ Resources", value=f"`{ram_gb}GB RAM | {cpu} CPU | {disk_gb}GB Disk`", inline=False)
    embed.set_footer(text="🚀 Powered by PowerDev")

    await interaction.response.send_message(embed=embed, view=ManageView(name, vps["ip"], vps["owner_id"]), ephemeral=True)


@bot.tree.command(name="delete-vps", description="Admin: Delete a VPS")
async def delete_vps(interaction: discord.Interaction, name: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Not allowed.", ephemeral=True)
        return
    db = load_db()
    if name not in db:
        await interaction.response.send_message("❌ VPS not found.", ephemeral=True)
        return
    await run_cmd("lxc-stop", "-n", name, "-t", "30")
    await run_cmd("lxc-destroy", "-n", name)
    db.pop(name, None)
    save_db(db)
    await interaction.response.send_message(f"🗑️ VPS `{name}` deleted.", ephemeral=True)


@bot.tree.command(name="list", description="List your VPS")
async def list_vps(interaction: discord.Interaction):
    db = load_db()
    user_vps = [n for n, v in db.items() if v["owner_id"] == interaction.user.id]
    if not user_vps:
        await interaction.response.send_message("📭 You have no VPS.", ephemeral=True)
        return
    embed = discord.Embed(title="📋 Your VPS List", color=discord.Color.green())
    embed.add_field(name="Servers", value="\n".join(f"`{n}`" for n in user_vps), inline=False)
    embed.set_footer(text="🚀 Powered by PowerDev")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! `{round(bot.latency*1000)}ms`", ephemeral=True)


bot.run(TOKEN)
