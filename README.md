# mesh-vnode

A virtual node that remembers messages, for use with Meshtastic® meshes.

<a href="https://meshtastic.org"><img src="docs/images/m-powered.png" alt="Meshtastic Powered" height="22"></a>

It holds your node's single TCP connection, stores what it hears in SQLite, and presents
itself to the Meshtastic app as a node on TCP. An app that connects gets the node's config
and then the messages it missed.

```
                                     ┌── Meshtastic app   (TCP 4404)
  node ──(WiFi, TCP 4403)── vnode ───┼── another app      (TCP 4404)
                             SQLite  └── web UI           (HTTP 8080)
```

## What it gives you

- **A backlog that survives.** The node buffers only 8-32 packets for a disconnected app,
  in RAM. This store is on disk, and each app has its own cursor.
- **Several apps at once.** A node serves one TCP client; this multiplexes many onto that
  one link.
- **A traffic filter.** Position, telemetry and node info sent to apps can be limited to
  favourites or switched off, so a phone does not wake for every packet.
- **A web UI.** Messages, node list, map with tracks, telemetry graphs, traceroutes. Behind
  Home Assistant it is reachable wherever HA is, with HA's login.
- **Nothing to operate.** One process, one SQLite file.

## Getting started

| Mode | Description |
| --- | --- |
| [Home Assistant add-on](docs/install.md#home-assistant-add-on) | installed from the add-on store; the UI sits in the HA sidebar behind your HA login |
| [Standalone](docs/install.md#standalone) | a Raspberry Pi or any Linux box, started with `start.sh` or as a systemd service |
| [Docker Compose](docs/install.md#docker-compose) | a container, for machines that already run everything that way |

Installation steps for each are in [docs/install.md](docs/install.md). Standalone in short:

```sh
./start.sh --node <your-node-ip>
```

Web UI on <http://localhost:8080>. In the Meshtastic app, add a node over **Network**: this
machine, port 4404. Settings: [docs/configuration.md](docs/configuration.md).

## Limitations

- WiFi nodes only (the upstream link is TCP), one node per instance.
- Nothing else may connect to the node directly - see
  [install.md](docs/install.md#before-you-start).
- No authentication of its own: trusted LAN only, **do not forward its ports**
  ([security.md](docs/security.md)).
- English only. Developed against the Android app; iOS is untested.

## Credits

- **[Meshtastic firmware](https://github.com/meshtastic/firmware)** - `PhoneAPI.cpp` is
  the real specification of the phone protocol.
- **[Meshtastic Android app](https://github.com/meshtastic/Meshtastic-Android)** - what a
  client expects on connect.
- **[meshtastic-python](https://github.com/meshtastic/python)** - the upstream link and the
  protobufs.
- **[MeshMonitor](https://github.com/Yeraze/meshmonitor)** by Yeraze - also has a virtual
  node, and was the main inspiration.
- **[MapLibre GL JS](https://github.com/maplibre/maplibre-gl-js)** with
  **[OpenFreeMap](https://openfreemap.org)** / [OpenMapTiles](https://openmaptiles.org) or
  [CARTO](https://carto.com/basemaps/) tiles, on
  © [OpenStreetMap](https://www.openstreetmap.org/copyright) data.
- FastAPI, Uvicorn, Pydantic, Typer, React, Vite, Tailwind CSS and Recharts.

Meshtastic® is a registered trademark of Meshtastic LLC. This project is not affiliated
with or endorsed by the Meshtastic project, and comes with no warranty. The Meshtastic
Powered badge is from the
[Meshtastic design repository](https://github.com/meshtastic/design/tree/master/Meshtastic%20Powered%20Logo)
(GPL-3.0) and marks compatibility, not endorsement.

## Written by an AI

The code, tests and docs were written with [Claude Code](https://claude.com/claude-code),
directed and tested on real hardware by a human. It runs well, but nobody has audited it line by line.

## Development

[docs/development.md](docs/development.md) - tests, linting, and a fake node for working
without hardware.
