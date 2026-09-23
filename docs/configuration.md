# Configuration

Most installs set only the node's address. The web UI (⚙) covers the rest of what an
ordinary install changes.

## Where settings come from

Highest wins:

1. **Command line** on `mesh-vnode run`: `--node`, `--node-port`, `--port`, `--web-port`,
   `--db`, `--replay`, `--log-level`.
2. **Environment**: `VNODE_*` variables, or a `.env` file in the working directory.
3. **Config file**: `VNODE_CONFIG_FILE` pointing at YAML or JSON, with the setting names
   below as keys, without the prefix. The Home Assistant add-on uses this with its
   `/data/options.json`.

```yaml
upstream_host: 10.0.0.42
retention_days: 30
```

The **web UI** stores its own settings in the database: the node's address, whether apps
may change the node's settings, what is forwarded to apps, the map, muted conversations
and the telemetry layout. For the node's address, `--node` wins over the web UI, which
wins over `VNODE_UPSTREAM_HOST`.

## Settings

| variable | default | meaning |
| --- | --- | --- |
| `VNODE_UPSTREAM_HOST` | `meshtastic.local` | the node; use its IP |
| `VNODE_UPSTREAM_PORT` | `4403` | |
| `VNODE_LISTEN_PORT` | `4404` | the port apps connect to |
| `VNODE_WEB_PORT` | `8080` | the web UI |
| `VNODE_LISTEN_HOST` / `VNODE_WEB_HOST` | `0.0.0.0` | interfaces to listen on; `127.0.0.1` for local only, e.g. behind a reverse proxy |
| `VNODE_DB_PATH` | `data/vnode.sqlite3` | the store |
| `VNODE_RETENTION_DAYS` | `30` | how long anything stored is kept |
| `VNODE_ALLOW_ADMIN` | `false` | let apps change the node's settings |
| `VNODE_ALLOW_ADMIN_READS` | `true` | let apps read them while writes stay blocked |
| `VNODE_REPLAY_MODE` | `cursor` | `cursor` replays what an app has not seen; `none` turns replay off |
| `VNODE_REPLAY_LIMIT` | `200` | most messages replayed per connect |
| `VNODE_REPLAY_MAX_AGE_DAYS` | `30` | older messages are not replayed |
| `VNODE_REPLAY_SKIP_OWN` | `true` | do not replay an app's own messages to it |
| `VNODE_REPLAY_PACE_MS` | `20` | pause between replayed messages |
| `VNODE_STORE_OUTGOING` | `true` | store what apps send, so the web UI and other apps see it |
| `VNODE_CLIENT_KEY_MODE` | `ip` | how a returning app is recognised: `ip`, `ip_port` (every connection is new), `shared` (one cursor for all) |
| `VNODE_CLIENT_IDLE_TIMEOUT_S` | `900` | drop an app that has been silent this long |
| `VNODE_LOG_LEVEL` | `INFO` | `DEBUG` explains every routing decision |
| `VNODE_TRACE_FRAMES` | `false` | hex-dump every frame; contains key material, see [security.md](security.md#what-the-store-contains) |
| `VNODE_MESHTASTIC_LOG_LEVEL` | `WARNING` | the meshtastic library's own logging |
| `VNODE_LOG_FILE` | unset | rotating log file (`start.sh` uses `data/vnode.log`) |
| `VNODE_LOG_CONSOLE` | `true` | also log to the console |
| `VNODE_LOG_MAX_BYTES` / `VNODE_LOG_BACKUPS` | `8000000` / `3` | log rotation |

Under Docker Compose, `VNODE_LISTEN_PORT` and `VNODE_WEB_PORT` set the published host
ports. In Home Assistant the host port for 4404 is set in the add-on's *Network* panel.

## The map

Settings → Map picks the tile provider:

- **OpenFreeMap** (default): OpenStreetMap data, no key, can be
  [self-hosted](https://openfreemap.org/).
- **CARTO**: works without a key but may be throttled; a free key from
  [carto.com/basemaps/apikey](https://carto.com/basemaps/apikey) avoids that.

## Notifications

Browser notifications need the page on HTTPS or `localhost`. Behind Home Assistant, that
means HA itself must be reached over HTTPS.
