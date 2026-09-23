# mesh-vnode

A virtual node that remembers messages, for use with Meshtastic® meshes.

<a href="https://meshtastic.org"><img src="docs/images/m-powered.png" alt="Meshtastic Powered" height="22"></a>

It sits between your Meshtastic node and the apps that talk to it: it holds the node's
single TCP connection, stores what it hears in SQLite, and presents itself to the
Meshtastic phone app as a node on TCP. When the app connects it gets the node's config
and then the messages it missed - so the backlog outlives the node's own short queue,
and more than one app can read it.

```
                                     ┌── Meshtastic app   (TCP 4404)
  node ──(WiFi, TCP 4403)── vnode ───┼── another app      (TCP 4404)
                             SQLite  └── web UI           (HTTP 8080)
```

## What it gives you

- **A backlog that survives.** The node queues packets for a disconnected app too, but
  only 8 of them on a classic ESP32, 32 elsewhere, and only in RAM. This store is on
  disk, bounded by retention rather than slots, and each client has its own cursor.
- **More than one app at a time.** A node serves a single TCP client and a second
  connection takes the link from the first; this accepts several and multiplexes them
  onto the one link it holds.
- **A traffic filter.** Position, telemetry and node-info forwarding can be narrowed to
  favourites or switched off, so a phone does not wake for every packet on the mesh.
- **Nothing to operate.** One process, one SQLite file. No broker, no account, no cloud.

### What the node already does

It does not lose your DMs to mesh chatter: when the queue to the phone is full,
[`sendToPhone`](https://github.com/meshtastic/firmware/blob/master/src/mesh/MeshService.cpp)
evicts an old *text* to make room for a new one and drops incoming position and telemetry
instead. What it lacks is depth
([`MAX_RX_TOPHONE`](https://github.com/meshtastic/firmware/blob/master/src/mesh/mesh-pb-constants.h)
is 8 or 32), survival across a reboot, and more than one connected app. That is the gap
this fills - if your phone is away an hour at a time and you run one app, the firmware is
enough.

There is also a **web UI** - messages, node list, map with position tracks, telemetry
graphs, and per-node requests (traceroute, position, telemetry, node info, on a channel
you pick) - but treat it as a convenience; Meshtastic's own web client and
[MeshMonitor](https://github.com/Yeraze/meshmonitor) are far richer. Where it earns its
keep is Home Assistant: the add-on rides your existing remote access to HA, so your mesh
is reachable from outside the house behind the login you already have.

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

Everything is a `VNODE_*` environment variable or a YAML/JSON config file. A few
per-install choices live in the web UI (⚙): the node's address, the basemap, whether apps
may change the node's settings, muted conversations, telemetry display. Full list and
precedence: **[docs/configuration.md](docs/configuration.md)**.

## Limitations

- **WiFi nodes only.** The upstream link is TCP. `meshtastic-python` has `BLEInterface`
  and `SerialInterface`, but nothing here uses them and the reconnect logic assumes a
  host and port.
- **One node per instance.** Run a second instance on other ports for a second node.
- **No authentication of its own.** Trusted LAN only - see [Security](#security).
- **One thing may talk to the node at a time.** A node serves a single TCP client, and a
  new connection force-closes the previous one
  ([`ServerAPI.h`](https://github.com/meshtastic/firmware/blob/master/src/mesh/api/ServerAPI.h)).
  MeshMonitor, a phone pointed straight at the node, or a second instance of this will
  fight this one for the link - dropped messages, and a node that never sleeps. Point
  everything at 4404 instead. (BLE and USB are separate transports and are unaffected.)
- **English only.** Dates and numbers follow the browser's locale, the text does not.
- **Developed against the Android app.** iOS speaks the same protocol but is untested.
- **Not a mesh participant.** No radio of its own, no channel or key management, and
  admin writes from apps are blocked unless you turn them on.

## Security

Short version, in full in **[docs/security.md](docs/security.md)**:

- The virtual node on 4404 and the web UI on 8080 are **unauthenticated**: whatever can
  reach them can read the history and send to the mesh. Do not forward those ports.
- The store is a plain SQLite file - message text at rest, not encrypted.
- Letting apps change the node's settings is off by default (`allow_admin`).
- Direct messages stay end-to-end encrypted; the one repair this makes is the stamp the
  node itself would have applied.
- Only the basemap needs the internet, and only the browser talks to it.

## Credits

This exists because other people published theirs:

- **[Meshtastic firmware](https://github.com/meshtastic/firmware)** - the actual
  specification of the phone protocol; `PhoneAPI.cpp` answered every question.
- **[Meshtastic Android app](https://github.com/meshtastic/Meshtastic-Android)** - what a
  client expects on connect, and the source of the DM quirk this works around.
- **[meshtastic-python](https://github.com/meshtastic/python)** - the upstream TCP link
  and the generated protobufs.
- **[MeshMonitor](https://github.com/Yeraze/meshmonitor)** by Yeraze - also has a virtual node and
  was the main inspiration for this project.
- **[MapLibre GL JS](https://github.com/maplibre/maplibre-gl-js)** with basemaps from
  **[OpenFreeMap](https://openfreemap.org)** / [OpenMapTiles](https://openmaptiles.org)
  or [CARTO](https://carto.com/basemaps/), on
  © [OpenStreetMap](https://www.openstreetmap.org/copyright) data.
- FastAPI, Uvicorn, Pydantic, Typer, React, Vite, Tailwind CSS and Recharts.

Meshtastic® is a registered trademark of Meshtastic LLC. Meshtastic software components
are released under various licenses, see [GitHub](https://github.com/meshtastic) for
details. No warranty is provided - use at your own risk. This project is not affiliated
with or endorsed by the Meshtastic project. The Meshtastic Powered badge comes from the
[Meshtastic design
repository](https://github.com/meshtastic/design/tree/master/Meshtastic%20Powered%20Logo)
(GPL-3.0) and marks compatibility, not endorsement.

## Written by an AI

Every line of code, test and documentation here was written by **Claude Opus 5**
(Anthropic) in [Claude Code](https://claude.com/claude-code), from prompts and review by
a human who ran it against real hardware. Commits carry a `Co-Authored-By` trailer naming
the model. The tests pass and it has run for days on a real node, but nobody has audited
it line by line.

## Development

Tests, linting, and a fake node plus a fake phone app to run it all without hardware:
**[docs/development.md](docs/development.md)**.
