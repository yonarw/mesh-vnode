# Installing

Three ways to run it. They are the same program with the same settings; only where the
settings come from differs.

- [Before you start](#before-you-start)
- [Home Assistant add-on](#home-assistant-add-on)
- [Standalone](#standalone)
- [Docker Compose](#docker-compose)

## Before you start

You need your node's **IP address**, and its WiFi TCP API enabled (the default). Give the
node a static lease on your router: the address ends up in the configuration, and
reconnecting apps are recognised by their own address too.

> **Only one thing may talk to the node at a time.** A Meshtastic node serves a single
> TCP client, and when something else connects the firmware force-closes the connection
> it already had ([`ServerAPI.h`](https://github.com/meshtastic/firmware/blob/master/src/mesh/api/ServerAPI.h)).
> If MeshMonitor, a phone pointed straight at the node, or a second copy of this service
> is running at the same time, the two will take the link from each other in a loop. The
> visible symptoms are **messages missing from the store** (nothing is listening at the
> moment they arrive) and a **node that drains its battery** far faster than usual,
> because reconnect traffic and WiFi keep it awake.
>
> So: point everything at the virtual node on port 4404 instead of at the node, and shut
> down whatever else was using 4403. Bluetooth and USB are separate transports on the
> node and are not affected - a BLE phone can stay connected while this runs.

## Home Assistant add-on

Add this repository under **Settings → Add-ons → Add-on store → ⋮ → Repositories**, paste
`https://github.com/yonarw/mesh-vnode`, then install *Mesh vnode* from the list that
appears. [addon/DOCS.md](../addon/DOCS.md) is the add-on's own documentation and is what
you see on its Documentation tab.

What is different in that mode, and nothing else is:

- The web UI is served through **ingress**: it appears in the sidebar and Home Assistant
  authenticates it. Its port is deliberately not published, so the unauthenticated API is
  not on your LAN - and if you already reach Home Assistant from outside, you reach your
  mesh the same way, with the same login.
- **Port 4404 is published**, because the phone app speaks raw TCP and ingress cannot
  proxy that. The host port is yours to change in the add-on's *Network* panel if 4404 is
  taken.
- Settings come from the add-on's options form, which the Supervisor writes to
  `/data/options.json`.
- **Use the node's IP, not `meshtastic.local`** - mDNS does not resolve inside the
  container. (True of Docker as well.)

The add-on pulls a published image rather than building on your machine, so
`addon/config.yaml`'s `version` and the tag pushed by `.github/workflows/addon-image.yml`
have to move together. Building it locally for development is in
[development.md](development.md#packaging-the-add-on-locally).

## Standalone

Plain Python is enough. `meshtastic`, `fastapi` and `uvicorn` are pure Python or have
wheels for 64-bit ARM, so the same install works on Raspberry Pi OS as anywhere else -
nothing to compile, no container needed.

```sh
git clone https://github.com/yonarw/mesh-vnode.git && cd mesh-vnode
./start.sh --node 10.0.0.42
```

`start.sh` syncs dependencies with [uv](https://docs.astral.sh/uv/), builds the web UI on
first run, stops an instance already running and starts a new one in the background.

```sh
./stop.sh          # stop it
tail -f data/vnode.log
./start.sh --fg    # foreground instead, Ctrl-C to quit
```

In the background the service writes and rotates `data/vnode.log` itself (8 MB, three old
files). `data/vnode.stderr.log` only catches what happens before logging starts, or a
crash.

Put `VNODE_UPSTREAM_HOST` in a `.env` file (copy `.env.example`) and `--node` becomes
unnecessary. `start.sh` is only a wrapper; by hand it is:

```sh
uv sync
VNODE_UPSTREAM_HOST=10.0.0.42 uv run mesh-vnode run
```

### As a systemd service

Create the environment once in the project directory (`uv sync --frozen --no-dev`; uv
comes from `curl -LsSf https://astral.sh/uv/install.sh | sh`), copy
`deploy/mesh-vnode.service` to `/etc/systemd/system/`, edit the paths and the node
address in it, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now mesh-vnode
journalctl -u mesh-vnode -f
```

## Docker Compose

An option, not a requirement - useful if everything else on that machine is already in
containers.

```sh
VNODE_UPSTREAM_HOST=10.0.0.42 docker compose up -d
```

`docker-compose.yml` mounts `./data` for the store and publishes 4404 and 8080. Both host
ports come from `VNODE_LISTEN_PORT` and `VNODE_WEB_PORT`, so a `.env` with
`VNODE_WEB_PORT=18080` moves the UI without editing the file. Use the node's IP here too:
mDNS does not resolve inside the container.

## After starting

1. Open the web UI (<http://localhost:8080>, or the sidebar entry in Home Assistant). The
   Status page should show the node connected and its config captured.
2. In the Meshtastic app, add a node over **Network**: host is the machine running this,
   port **4404**.
