# Installing

## Before you start

- You need the node's **IP address**, with its WiFi TCP API enabled (the default). Give it
  a static lease on your router. Use the IP, not `meshtastic.local`: mDNS does not resolve
  inside Docker or Home Assistant.
- **Nothing else may connect to the node directly.** A node serves one TCP client and drops
  the old one when a new one connects, so MeshMonitor, a phone pointed at the node or a
  second copy of this will keep taking the link from each other: messages go missing and
  the node's battery drains. Point everything at port 4404 instead. Bluetooth and USB are
  not affected.

## Home Assistant add-on

**Settings → Add-ons → Add-on store → ⋮ → Repositories**, add
`https://github.com/yonarw/mesh-vnode`, then install *Mesh vnode*. Setup and options are in
[addon/DOCS.md](../addon/DOCS.md), which is also shown on the add-on's Documentation tab.

## Standalone

```sh
git clone https://github.com/yonarw/mesh-vnode.git && cd mesh-vnode
./start.sh --node 10.0.0.42    # needs uv; builds the web UI on first run
./stop.sh
./start.sh --fg                # in the foreground instead
```

It runs in the background and logs to `data/vnode.log` (rotated). With
`VNODE_UPSTREAM_HOST` in a `.env` file (see `.env.example`), `--node` can be left out.

### As a systemd service

Run `uv sync --frozen --no-dev` in the project directory, copy `deploy/mesh-vnode.service`
to `/etc/systemd/system/`, adjust its paths and node address, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now mesh-vnode
journalctl -u mesh-vnode -f
```

## Docker Compose

```sh
VNODE_UPSTREAM_HOST=10.0.0.42 docker compose up -d
```

The store goes to `./data`. The published ports come from `VNODE_LISTEN_PORT` and
`VNODE_WEB_PORT` (defaults 4404 and 8080).

## After starting

1. Open the web UI (<http://localhost:8080>, or the sidebar in Home Assistant). The Status
   page should show the node connected.
2. In the Meshtastic app, add a node over **Network**: this machine, port **4404**.
