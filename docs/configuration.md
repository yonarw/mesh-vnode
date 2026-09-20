# Configuration

Most installations set one thing - the node's address - and never come back here. The web
UI (⚙) and, in Home Assistant, the add-on's options page cover everything an ordinary
install needs; this page is the full reference behind them.

## The settings that matter

| setting | default | |
| --- | --- | --- |
| `VNODE_UPSTREAM_HOST` | `meshtastic.local` | your node. **Use its IP** under Docker and Home Assistant |
| `VNODE_UPSTREAM_PORT` | `4403` | |
| `VNODE_LISTEN_PORT` | `4404` | the port the phone app connects to |
| `VNODE_WEB_PORT` | `8080` | the web UI |
| `VNODE_RETENTION_DAYS` | `30` | how long stored packets are kept |
| `VNODE_ALLOW_ADMIN` | `false` | let connected apps change the node's own settings |
| `VNODE_LOG_LEVEL` | `INFO` | `DEBUG` explains every routing decision |

## Where settings come from

Highest wins:

1. **Command line** - `--node`, `--port`, `--web-port`, `--db`, `--replay`, `--log-level`
   on `mesh-vnode run`. `./start.sh --node ...` passes that one through.
2. **Environment** - `VNODE_*` variables, or a `.env` file in the working directory (copy
   [.env.example](../.env.example)).
3. **Config file** - optional, `VNODE_CONFIG_FILE` pointing at YAML or JSON.

The file sits *below* the environment on purpose: a `VNODE_*` variable baked into an image
or set in a compose file still wins, which is what lets one image serve every deployment.
A path that is not there is simply not read.

```yaml
# vnode.yaml - VNODE_CONFIG_FILE=vnode.yaml
upstream_host: 10.0.0.42
retention_days: 30
```

Keys are the setting names without the prefix. JSON works too, which is how the Home
Assistant add-on runs: the Supervisor writes `/data/options.json` and the image reads it,
with no translation layer in between.

A few settings live in the **web UI** instead, stored in the database, because they are
choices about this install rather than about the deployment: the node's address, the
basemap and its key, whether apps may change the node's settings, muted conversations and
how telemetry is shown. For the node address the order is `--node`, then the web UI, then
`VNODE_UPSTREAM_HOST`.

## Ports

Both listening ports are settings, so nothing is stuck on 4404 or 8080:

- **Standalone:** `VNODE_LISTEN_PORT` / `VNODE_WEB_PORT`, or `--port` / `--web-port`.
- **Docker Compose:** the same two variables set the published host ports; inside the
  container the defaults stay put.
- **Home Assistant:** the add-on's *Network* panel maps the host port for 4404. The UI has
  no host port at all - it goes through ingress.

`VNODE_LISTEN_HOST` and `VNODE_WEB_HOST` default to `0.0.0.0`. Setting
`VNODE_WEB_HOST=127.0.0.1` keeps the UI local to the machine, which is what you want
behind a reverse proxy that authenticates.

## Everything else

Tuning, debugging and behaviour you are unlikely to need. The defaults are the answer.

| variable | default | meaning |
| --- | --- | --- |
| `VNODE_LISTEN_HOST` / `VNODE_WEB_HOST` | `0.0.0.0` | interfaces to listen on |
| `VNODE_DB_PATH` | `data/vnode.sqlite3` | the store |
| `VNODE_REPLAY_MODE` | `cursor` | `cursor` replays what a client has not seen; `none` turns replay off and leaves a plain multiplexer |
| `VNODE_REPLAY_LIMIT` | `200` | most messages replayed in one connect |
| `VNODE_REPLAY_MAX_AGE_DAYS` | `30` | ignore anything older when replaying |
| `VNODE_REPLAY_SKIP_OWN` | `true` | do not replay a client's own messages to it |
| `VNODE_REPLAY_PACE_MS` | `20` | pause between replayed messages; some clients choke on a burst |
| `VNODE_STORE_OUTGOING` | `true` | store what the apps send, so the web UI and other apps see it |
| `VNODE_CLIENT_KEY_MODE` | `ip` | how a returning client is recognised: `ip`, `ip_port` (every connection is new), `shared` (one cursor for all) |
| `VNODE_ALLOW_ADMIN_READS` | `true` | let apps send read-only admin requests (`get_config`, `get_owner`, …) while writes stay blocked |
| `VNODE_CLIENT_IDLE_TIMEOUT_S` | `900` | drop an app connection silent for this long |
| `VNODE_TRACE_FRAMES` | `false` | log every frame on both sockets with a hex dump. Very loud, and it dumps key material - see [security.md](security.md#what-the-store-contains) |
| `VNODE_MESHTASTIC_LOG_LEVEL` | `WARNING` | the upstream library's own logging |
| `VNODE_LOG_FILE` | unset | rotating log file (`start.sh` sets `data/vnode.log`) |
| `VNODE_LOG_CONSOLE` | `true` | also log to the console (`start.sh` turns it off) |
| `VNODE_LOG_MAX_BYTES` / `VNODE_LOG_BACKUPS` | `8000000` / `3` | log rotation |

## The map

The map is [MapLibre GL JS](https://maplibre.org/) over vector tiles. MapLibre is free
software, but tiles always come from a provider, so the provider is a setting (Settings →
Map):

- **OpenFreeMap** (default) - OpenStreetMap data, no key, no account, no sign-up. Styles:
  Dark, Liberty, Bright, Positron, Fiord. [Self-hostable](https://openfreemap.org/) if you
  would rather not depend on a public service.
- **CARTO** - the styles are nicer to some eyes. Tiles load without a key, but CARTO may
  mark or throttle them; a free key from
  [carto.com/basemaps/apikey](https://carto.com/basemaps/apikey) removes that. The key is
  stored in the database, sent by the *browser* with each tile request, and only ever to
  `*.basemaps.cartocdn.com`.

Basemap tiles are the only thing in this project that reaches the internet. No message,
node or position ever leaves the machine; tile requests do tell the provider which part of
the world you are looking at.

## Notifications

Browser notifications only work when the page is opened over HTTPS or on `localhost` - a
browser rule, not a setting here. Through Home Assistant's ingress that means HA itself
has to be reached over HTTPS.
