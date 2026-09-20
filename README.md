# mesh-vnode

A virtual node that remembers messages, for use with Meshtastic® meshes.

<a href="https://meshtastic.org"><img src="docs/m-powered.png" alt="Meshtastic Powered" height="22"></a>

It sits between your Meshtastic node and the apps that talk to it: it holds the node's
single TCP connection, stores what it hears in SQLite, and presents itself to the
Meshtastic phone app as a node on TCP. When the app connects it gets the node's config
and then the messages it missed - so the phone no longer has to hold a socket open all
day to avoid losing direct messages.

```
                                     ┌── Meshtastic app   (TCP 4404)
  node ──(WiFi, TCP 4403)── vnode ───┼── another app      (TCP 4404)
                             SQLite  └── web UI           (HTTP 8080)
```

## What it gives you

The point of it is the store and the socket:

- **No missed messages.** Every stored packet is replayed to an app that has not seen it
  yet. Each client has its own cursor, so reconnecting does not mean re-reading the same
  backlog.
- **More than one app at a time.** A node serves a single TCP client and a second
  connection takes the link away from the first; the virtual node accepts several and
  multiplexes them onto the one link it holds.
- **A traffic filter.** Position, telemetry and node-info forwarding to connected apps
  can be narrowed to favourites or switched off, so a phone does not wake for every
  packet on the mesh.
- **Nothing to operate.** One process, one SQLite file. No broker, no account, no cloud.

There is also a **web UI** - messages, node list, map with position tracks, telemetry
graphs - but treat it as a convenience, not the reason to run this. Meshtastic's own web
client and [MeshMonitor](https://github.com/Yeraze/meshmonitor) are far richer, and this
one exists mostly because the data was already here. Where it does earn its keep is Home
Assistant: through the add-on it rides your existing remote access to HA, so you get an
authenticated view of your mesh from outside the house without exposing anything.

## Operating modes

| Mode | Good for | Documentation |
| --- | --- | --- |
| **Home Assistant add-on** | HA OS or Supervised; the UI appears in the sidebar behind your HA login | [addon/DOCS.md](addon/DOCS.md) |
| **Standalone** | a Raspberry Pi or any Linux box, with systemd for always-on | [docs/install.md](docs/install.md#standalone) |
| **Docker Compose** | machines that already run everything in containers | [docs/install.md](docs/install.md#docker-compose) |

Standalone, from a checkout:

```sh
./start.sh --node <your-node-ip>     # syncs deps, builds the UI, starts in the background
./stop.sh
```

Web UI on <http://localhost:8080>, virtual node on TCP 4404 (both ports are settings -
see [docs/configuration.md](docs/configuration.md#ports)). In the Meshtastic app add a
node over **Network**: host is the machine running this, port 4404.

## Settings

Everything is a `VNODE_*` environment variable, optionally a YAML or JSON config file,
and a handful of things live in the web UI (⚙) because they are per-install choices: the
node's address, the basemap, whether apps may change the node's settings, muted
conversations, telemetry display.

Full list, precedence and the interesting ones explained: **[docs/configuration.md](docs/configuration.md)**.

## Limitations

- **WiFi nodes only.** The upstream link is TCP. BLE and USB serial would probably not
  be hard - `meshtastic-python`, which this already uses, has `BLEInterface` and
  `SerialInterface` - but nothing here uses them and the reconnect logic assumes a host
  and port. Not implemented, not tested.
- **One node per instance.** Run a second instance on other ports for a second node.
- **No authentication of its own.** Port 4404 and the web UI are as open as the node's
  own port 4403: fine on a trusted LAN, never exposed to the internet. The Home
  Assistant add-on puts the UI behind HA's login; port 4404 stays raw TCP because the
  phone app cannot speak anything else.
- **One thing may talk to the node at a time.** A Meshtastic node serves a single TCP
  client, and a new connection force-closes the previous one
  ([firmware `ServerAPI.h`](https://github.com/meshtastic/firmware/blob/master/src/mesh/api/ServerAPI.h)).
  So MeshMonitor, a phone pointed straight at the node, or a second instance of this will
  fight this one for the link - with dropped messages and a node that never sleeps as the
  result. Point everything at the virtual node instead. (BLE and USB are separate
  transports and can be used at the same time.)
- **English only.** Dates and numbers follow the browser's locale, the text does not.
- **Developed against the Android app.** iOS speaks the same protocol but is untested
  here.
- It is not a mesh participant: no radio of its own, no channel or key management, and
  admin writes from apps are blocked unless you turn them on.

## Security

Short version, in full in **[docs/security.md](docs/security.md)**:

- The virtual node on 4404 and the web UI on 8080 are **unauthenticated**. Whatever can
  reach them can read the stored history and send messages to the mesh. Keep them on a
  trusted network; do not forward them.
- The store is a plain SQLite file - message text at rest, not encrypted.
- Letting apps change the node's settings is off by default (`allow_admin`).
- Direct messages stay end-to-end encrypted: the one repair this makes to them is the
  stamp the node itself would have applied.
- Only the basemap needs the internet, and only the browser talks to it.

## Credits

This exists because other people published theirs:

- **[Meshtastic firmware](https://github.com/meshtastic/firmware)** - the actual
  specification of the phone protocol. `PhoneAPI.cpp` answered every question about what
  a node sends and when.
- **[Meshtastic Android app](https://github.com/meshtastic/Meshtastic-Android)** - what a
  client expects on connect, and the source of the direct-message quirk this works
  around.
- **[meshtastic-python](https://github.com/meshtastic/python)** - the upstream TCP link
  and the generated protobufs.
- **[MeshMonitor](https://github.com/Yeraze/meshmonitor)** by Yeraze - prior art for
  proxying a node and keeping its history. No code is taken from it.
- **[MapLibre GL JS](https://github.com/maplibre/maplibre-gl-js)** with basemaps from
  **[OpenFreeMap](https://openfreemap.org)** / [OpenMapTiles](https://openmaptiles.org)
  or [CARTO](https://carto.com/basemaps/), on
  © [OpenStreetMap](https://www.openstreetmap.org/copyright) data.
- FastAPI, Uvicorn, Pydantic, Typer, React, Vite, Tailwind CSS and Recharts.

Meshtastic® is a registered trademark of Meshtastic LLC. Meshtastic software
components are released under various licenses, see
[GitHub](https://github.com/meshtastic) for details. No warranty is provided - use
at your own risk.

This project is not affiliated with or endorsed by the Meshtastic project.

The Meshtastic Powered badge comes from the [Meshtastic design
repository](https://github.com/meshtastic/design/tree/master/Meshtastic%20Powered%20Logo)
(GPL-3.0). It marks technical compatibility and does not imply endorsement or
sponsorship by the Meshtastic project.

## Written by an AI

Every line of code, test and documentation here was written by **Claude Opus 5**
(Anthropic) in [Claude Code](https://claude.com/claude-code), from prompts and review by
a human who ran it against real hardware and decided what it should do. Commits carry a
`Co-Authored-By` trailer naming the model. Read it the way you would read any code from
a source you do not know yet: the tests pass and it has been running for days on a real
node, but nobody has audited it line by line.

## Development

`uv run pytest`, `uv run ruff check src tests`, and a fake node plus a fake phone app so
the whole thing can be exercised with no hardware attached:
**[docs/development.md](docs/development.md)**.
