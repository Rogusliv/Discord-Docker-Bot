# Server Bot

A Discord bot that manages a fleet of Docker containers (game servers, media servers, etc.) from slash commands. It posts a live status board, backs up save data, and keeps container images up to date.

## Features

- **Slash commands:** `/server start`, `/server stop`, `/server restart`, `/server update`, with autocomplete for server names.
- **Live status board:** a single embed in a channel of your choice, refreshed every 60 seconds (✅ running, ❌ stopped, ⚠️ container not found).
- **Automatic backups:** saves are archived to `.tar.gz` before stop/restart/update, and again for any server that has been running 24+ hours. The newest 8 backups per server are kept.
- **Image updates:** compares the local image digest with the registry and only pulls and recreates the container when a newer image exists. The container's ports, volumes, env vars, network mode, restart policy, devices and labels are preserved. All servers are checked once at bot startup.
- **Role-based access:** only members with the configured Discord roles can use the commands.

## Requirements

- Docker and Docker Compose on the host
- A Discord bot application with a token (see below)

## Discord setup

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications) and add a bot.
2. Under **Bot → Privileged Gateway Intents**, enable **Server Members Intent** (required for role checks).
3. Invite the bot to your server with the `bot` and `applications.commands` scopes. It needs permission to view the status channel, send messages and embed links.
4. Copy the bot token. Never commit it.

## Configuration

### 1. Environment variables

Copy `.env.example` to `.env` and fill it in:

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Your Discord bot token |
| `STATUS_CHANNEL_ID` | No | Channel ID for the status board. `0` disables it |
| `GAME_ROLE_1`, `GAME_ROLE_2` | Recommended | Role IDs. A user must have **all** configured roles to control game servers. Set to `0` to skip one |
| `JELLY_ROLE_1`, `JELLY_ROLE_2` | No | Role IDs for the `jellyfin` entry. A user needs **any one** of them. If both are `0`, the game roles apply |
| `TZ` | No | Timezone, e.g. `America/Chicago` |
| `KEEP_COUNT` | No | How many backups to keep per server. Older ones are deleted. Default `8` |
| `JELLYFIN_COMPOSE_DIR` | No | Only if Jellyfin is managed by its own `docker-compose.yml`. Restarts use `docker-compose restart` in that folder instead of restarting the container |

> ⚠️ **If no role IDs are set, every member of your Discord server can control your containers.** Always set at least `GAME_ROLE_1`.

To get an ID, enable Developer Mode in Discord (Settings → Advanced), then right-click a role or channel and choose **Copy ID**.

### 2. Server list

Edit `config/servers.json`. Each entry maps a name to a container and, optionally, a folder or file to back up:

```json
{
  "game_name": {"source": "/games_source/your/path", "container": "container-name"},
  "container_without_backup": {"source": null, "container": "container-name"}
}
```

- `container`: the Docker container name.
- `source`: the save data path **as seen inside the bot's container**, or `null` for no backups. If it points to a folder, the most recently modified item inside it is backed up.
- Any service you don't want to back up (Jellyfin, for example) is just an entry with `"source": null`. Delete the placeholder entries and list your own.

### 3. Volumes

Edit `docker-compose.yml` to match your host:

| Mount | Purpose |
|---|---|
| `/var/run/docker.sock` | Lets the bot control containers |
| `/games:/games_source:ro` | Your game save data, read-only |
| `/games/backup:/games/backups` | Where backups are written |
| `.../config/:/app/config/` | Folder containing `servers.json` |

## Running

Two compose files are included:

The published image is `ghcr.io/rogusliv/discord-docker-bot:latest`. To build it yourself instead, run `docker compose up -d --build`.

**Docker Compose** (uses your `.env` file):

```bash
cp .env.example .env   # then edit it
docker compose up -d
docker compose logs -f
```

**Portainer** (`docker-compose.portainer.yml`, no `.env` file needed):

1. In Portainer, create a stack from `docker-compose.portainer.yml`.
2. Under **Environment variables**, set `BOT_TOKEN` (required) and any optional settings from the table above.
3. Change `/path/to/Server_Bot/config` in the file to the real folder holding your `servers.json`, then deploy.

On startup the bot logs the servers it loaded, connects to Discord, syncs its slash commands and checks every container for image updates.

## Commands

| Command | What it does |
|---|---|
| `/server start <name>` | Starts the container |
| `/server stop <name>` | Backs up (if configured), then stops the container |
| `/server restart <name>` | Backs up (if configured), then restarts the container |
| `/server update <name>` | Backs up, pulls the newer image if one exists, recreates the container |

## Security notes

- Mounting `/var/run/docker.sock` gives the bot **full control of Docker on the host**. Only run this bot on a server you trust, keep your role checks configured, and keep the token private.
- If your token is ever exposed, reset it immediately in the Developer Portal.
- Startup auto-updates **every** server in `servers.json`, so pin image tags for anything you don't want updated.
