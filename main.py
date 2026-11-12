import os
import json
import shutil
import asyncio
import subprocess
import logging
import time
import glob
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(".env")

import discord
from discord import app_commands
from discord.ext import tasks
import docker

BACKUP_BASE = "/games/backups"
try:
    KEEP_COUNT = max(1, int(os.getenv("KEEP_COUNT", "8")))
except ValueError:
    KEEP_COUNT = 8
TIMEZONE = os.getenv('TZ', 'America/Chicago')
STATUS_CHANNEL_ID = int(os.getenv("STATUS_CHANNEL_ID", "0"))
TOKEN = os.getenv("BOT_TOKEN")
GAME_ROLES = [int(os.getenv("GAME_ROLE_1", 0)), int(os.getenv("GAME_ROLE_2", 0))]
JELLY_ROLES = [int(os.getenv("JELLY_ROLE_1", 0)), int(os.getenv("JELLY_ROLE_2", 0))]
# Optional: directory containing a docker-compose.yml for Jellyfin. If unset, Jellyfin is restarted as a normal container.
JELLYFIN_COMPOSE_DIR = os.getenv("JELLYFIN_COMPOSE_DIR", "").strip()

def load_servers():
    json_path = "/app/config/servers.json"
    try:
        if not os.path.exists(json_path):
            logging.error(f"❌ FILE NOT FOUND: {json_path}")
            return {}
        with open(json_path, "r") as f:
            data = json.load(f)
            logging.info(f"✅ Loaded {len(data)} servers.")
            return data
    except Exception as e:
        logging.error(f"❌ JSON LOAD ERROR: {e}")
        return {}

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
SERVERS = load_servers()
os.environ['TZ'] = TIMEZONE

try:
    d_client = docker.from_env()
    d_client.ping()
    logging.info(">>> [DOCKER] Connected.")
except Exception as e:
    logging.error(f">>> [DOCKER ERROR] {e}")
    d_client = None

def has_fleet_permission(interaction: discord.Interaction, server_name: str) -> bool:
    user_role_ids = [role.id for role in interaction.user.roles]
    if server_name.lower() == "jellyfin" and any(JELLY_ROLES):
        return any(rid in user_role_ids for rid in JELLY_ROLES if rid != 0)
    return all(rid in user_role_ids for rid in GAME_ROLES if rid != 0)

def perform_backup(target_key=None):
    results = []
    targets = {k: v for k, v in SERVERS.items() if k == target_key} if target_key else SERVERS
    for name, config in targets.items():
        base_source = config.get("source")
        if not base_source or not os.path.exists(base_source): continue
        if os.path.isdir(base_source):
            files = glob.glob(os.path.join(base_source, "*"))
            if not files: continue
            actual_source = max(files, key=os.path.getmtime)
        else:
            actual_source = base_source
        game_backup_dir = os.path.join(BACKUP_BASE, name)
        ts_str = datetime.now().strftime("%Y%m%d_%H%M")
        temp_folder = os.path.join(game_backup_dir, ts_str)
        dest_zip = os.path.join(game_backup_dir, f"{name}_{ts_str}.tar.gz")
        try:
            os.makedirs(temp_folder, exist_ok=True)
            if os.path.isdir(actual_source):
                shutil.copytree(actual_source, os.path.join(temp_folder, os.path.basename(actual_source)), dirs_exist_ok=True)
            else:
                shutil.copy2(actual_source, temp_folder)
            subprocess.run(["tar", "-czf", dest_zip, "-C", game_backup_dir, ts_str], check=True)
            shutil.rmtree(temp_folder)
            backups = sorted([os.path.join(game_backup_dir, f) for f in os.listdir(game_backup_dir) if f.endswith(".tar.gz")])
            while len(backups) > KEEP_COUNT: os.remove(backups.pop(0))
            results.append(name)
            logging.info(f"✅ Backup completed for {name}")
        except Exception as e:
            logging.error(f"Backup FAILED for {name}: {e}")
            if os.path.exists(temp_folder): shutil.rmtree(temp_folder)
    return results

def get_remote_digest(image_name: str) -> str | None:
    """HEAD request against the registry manifest — no image data downloaded."""
    tag = "latest"
    if ":" in image_name.split("/")[-1]:
        image_name, tag = image_name.rsplit(":", 1)
    parts = image_name.split("/")
    if len(parts) == 1:
        registry, repo = "registry-1.docker.io", f"library/{parts[0]}"
    elif "." in parts[0] or ":" in parts[0]:
        registry, repo = parts[0], "/".join(parts[1:])
    else:
        registry, repo = "registry-1.docker.io", "/".join(parts)
    try:
        auth_url = (f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
                    if registry == "registry-1.docker.io"
                    else f"https://{registry}/token?scope=repository:{repo}:pull&service={registry}")
        token_resp = requests.get(auth_url, timeout=10)
        token = token_resp.json().get("token") if token_resp.ok else None
        headers = {"Accept": "application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.index.v1+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = requests.head(f"https://{registry}/v2/{repo}/manifests/{tag}", headers=headers, timeout=10)
        return resp.headers.get("Docker-Content-Digest") if resp.ok else None
    except Exception as e:
        logging.error(f"get_remote_digest failed for {image_name}:{tag} — {e}")
        return None

def snapshot_container(attrs: dict) -> dict:
    """Capture a container's full config so it can be recreated identically."""
    cfg, hcfg = attrs['Config'], attrs['HostConfig']
    port_bindings = {
        port: [{"HostIp": b.get("HostIp", ""), "HostPort": b.get("HostPort", "")} for b in bindings]
        for port, bindings in (hcfg.get('PortBindings') or {}).items() if bindings
    }
    volumes = {
        m['Source']: {"bind": m['Destination'], "mode": m.get('Mode', 'rw') or 'rw'}
        for m in (attrs.get('Mounts') or []) if m.get('Type') == 'bind'
    }
    rp = hcfg.get('RestartPolicy') or {}
    return {
        "image": cfg['Image'], "name": attrs['Name'].lstrip('/'),
        "environment": cfg.get('Env') or [], "ports": port_bindings, "volumes": volumes,
        "network_mode": hcfg.get('NetworkMode', 'bridge'),
        "restart_policy": {"Name": rp.get('Name', ''), "MaximumRetryCount": rp.get('MaximumRetryCount', 0)},
        "cap_add": hcfg.get('CapAdd') or [],
        "devices": [{"PathOnHost": d["PathOnHost"], "PathInContainer": d["PathInContainer"],
                     "CgroupPermissions": d.get("CgroupPermissions", "rwm")} for d in (hcfg.get('Devices') or [])],
        "sysctls": hcfg.get('Sysctls') or {}, "labels": cfg.get('Labels') or {}, "detach": True,
    }

async def do_update(name: str, config: dict) -> str:
    """
    Check remote digest, pull only if newer, recreate container.
    Returns a status string. Runs blocking docker calls in an executor.
    """
    loop = asyncio.get_event_loop()
    container_name = config['container']
    container = d_client.containers.get(container_name)
    attrs = container.attrs
    image_name = attrs['Config']['Image']

    local_digest = next(
        (d.split('@')[-1] for d in d_client.images.get(attrs['Image']).attrs.get('RepoDigests', [])), None
    )
    remote_digest = await loop.run_in_executor(None, get_remote_digest, image_name)

    if remote_digest and local_digest and remote_digest == local_digest:
        return f"✅ **{name}** already up to date."

    if config.get('source'):
        await loop.run_in_executor(None, perform_backup, name)

    await loop.run_in_executor(None, lambda: d_client.images.pull(image_name))

    snap = snapshot_container(attrs)
    was_running = container.status == "running"
    if was_running:
        await loop.run_in_executor(None, container.stop)
    await loop.run_in_executor(None, container.remove)
    new = d_client.containers.create(**snap)
    if was_running:
        new.start()

    logging.info(f"✅ Updated {name} ({image_name})")
    return f"🆕 **{name}** updated and restarted."


class FleetBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.status_msg = None
        self.container_start_times = {}
        self.completed_backups = set()

    async def setup_hook(self):
        self.tree.add_command(ServerManagement())
        await self.tree.sync()
        asyncio.create_task(self.startup_update_check())

    async def startup_update_check(self):
        if not d_client:
            return
        logging.info("🔍 Checking all containers for image updates...")
        for name, config in SERVERS.items():
            try:
                result = await do_update(name, config)
                logging.info(result)
            except docker.errors.NotFound:
                logging.warning(f"⚠️  [{name}] Container not found, skipping.")
            except Exception as e:
                logging.error(f"❌ [{name}] Startup update failed: {e}")

    async def on_ready(self):
        logging.info(f"✅ Bot ready! Logged in as {self.user}")
        logging.info(f"✅ Connected to {len(self.guilds)} guild(s)")
        for guild in self.guilds:
            logging.info(f"  Guild: {guild.name} (ID: {guild.id})")
            channel = guild.get_channel(STATUS_CHANNEL_ID)
            logging.info(f"    {'✅' if channel else '❌'} Status channel {STATUS_CHANNEL_ID}")
        if not self.status_loop.is_running():
            self.status_loop.start()
        if not self.runtime_monitor.is_running():
            self.runtime_monitor.start()

    @tasks.loop(seconds=60)
    async def status_loop(self):
        await self.wait_until_ready()
        if not STATUS_CHANNEL_ID:
            return
        try:
            channel = self.get_channel(STATUS_CHANNEL_ID) or await self.fetch_channel(STATUS_CHANNEL_ID)
            embed = self.create_fleet_embed()
            if self.status_msg:
                try:
                    await self.status_msg.edit(embed=embed)
                    return
                except (discord.NotFound, discord.HTTPException):
                    self.status_msg = None
            async for message in channel.history(limit=100):
                if message.author == self.user and message.embeds and message.embeds[0].title == "🖥️ Server Status Monitor":
                    self.status_msg = message
                    await self.status_msg.edit(embed=embed)
                    return
            self.status_msg = await channel.send(embed=embed)
        except Exception as e:
            logging.error(f"❌ Status Loop Error: {e}")

    @tasks.loop(minutes=5)
    async def runtime_monitor(self):
        if not d_client:
            return
        for name, config in SERVERS.items():
            try:
                container = d_client.containers.get(config['container'])
                if container.status != "running":
                    self.container_start_times.pop(name, None)
                    continue
                container.reload()
                start_time = datetime.fromisoformat(container.attrs['State']['StartedAt'].replace('Z', '+00:00'))
                runtime_hours = (datetime.now(start_time.tzinfo) - start_time).total_seconds() / 3600
                self.container_start_times[name] = start_time
                if runtime_hours >= 24 and config.get('source'):
                    backup_key = f"{name}_{start_time.isoformat()}_{int(runtime_hours / 24) * 24}"
                    if backup_key not in self.completed_backups:
                        logging.info(f"🕐 {name} running {runtime_hours:.1f}h — triggering backup")
                        perform_backup(name)
                        self.completed_backups.add(backup_key)
            except Exception as e:
                logging.error(f"Runtime monitor error for {name}: {e}")

    def create_fleet_embed(self):
        embed = discord.Embed(title="🖥️ Server Status Monitor", color=discord.Color.blue())
        embed.description = f"**Last Updated:** <t:{int(time.time())}:R>\n\n"
        lines = []
        for name, config in SERVERS.items():
            try:
                status = "✅" if d_client and d_client.containers.get(config['container']).status == "running" else "❌"
            except:
                status = "⚠️"
            lines.append(f"{status} **{name.replace('_', ' ').title()}**")
        embed.description += "\n".join(lines)
        return embed


class ServerManagement(app_commands.Group):
    def __init__(self):
        super().__init__(name="server", description="Fleet control")

    async def server_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=k, value=k) for k in SERVERS.keys() if current.lower() in k.lower()]

    @app_commands.command(name="start")
    @app_commands.autocomplete(server_name=server_autocomplete)
    async def start(self, interaction: discord.Interaction, server_name: str):
        if not has_fleet_permission(interaction, server_name):
            return await interaction.response.send_message("⛔ Denied.", ephemeral=True)
        await interaction.response.send_message(f"🚀 Starting **{server_name.title()}**...", ephemeral=True)
        try:
            d_client.containers.get(SERVERS[server_name]['container']).start()
            await interaction.followup.send(f"✅ Started **{server_name.title()}**.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="stop")
    @app_commands.autocomplete(server_name=server_autocomplete)
    async def stop(self, interaction: discord.Interaction, server_name: str):
        if not has_fleet_permission(interaction, server_name):
            return await interaction.response.send_message("⛔ Denied.", ephemeral=True)
        await interaction.response.send_message(f"⏸️ Stopping **{server_name.title()}**...", ephemeral=True)
        try:
            if SERVERS[server_name]['source']:
                await interaction.followup.send("💾 Backing up...", ephemeral=True)
                perform_backup(server_name)
            d_client.containers.get(SERVERS[server_name]['container']).stop()
            await interaction.followup.send(f"✅ Stopped **{server_name.title()}**.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="restart")
    @app_commands.autocomplete(server_name=server_autocomplete)
    async def restart(self, interaction: discord.Interaction, server_name: str):
        if not has_fleet_permission(interaction, server_name):
            return await interaction.response.send_message("⛔ Denied.", ephemeral=True)
        await interaction.response.send_message(f"🔄 Restarting **{server_name.title()}**...", ephemeral=True)
        try:
            if SERVERS[server_name]['source']:
                await interaction.followup.send("💾 Backing up...", ephemeral=True)
                perform_backup(server_name)
            if server_name.lower() == "jellyfin" and JELLYFIN_COMPOSE_DIR:
                subprocess.run(["docker-compose", "-f", os.path.join(JELLYFIN_COMPOSE_DIR, "docker-compose.yml"), "restart"], cwd=JELLYFIN_COMPOSE_DIR, check=True)
            else:
                d_client.containers.get(SERVERS[server_name]['container']).restart()
            await interaction.followup.send(f"✅ Restarted **{server_name.title()}**.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="update", description="Check for a newer image and update if available")
    @app_commands.autocomplete(server_name=server_autocomplete)
    async def update(self, interaction: discord.Interaction, server_name: str):
        if not has_fleet_permission(interaction, server_name):
            return await interaction.response.send_message("⛔ Denied.", ephemeral=True)
        if server_name not in SERVERS:
            return await interaction.response.send_message(f"❌ Unknown server: **{server_name}**", ephemeral=True)
        await interaction.response.send_message(f"🔍 Checking for updates to **{server_name.title()}**...", ephemeral=True)
        try:
            result = await do_update(server_name, SERVERS[server_name])
            await interaction.followup.send(result, ephemeral=True)
        except docker.errors.NotFound:
            await interaction.followup.send(f"❌ Container not found.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Update failed: {e}", ephemeral=True)
            logging.error(f"Update error for {server_name}: {e}")


bot = FleetBot()
bot.run(TOKEN)
