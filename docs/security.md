# Security

mesh-vnode has no authentication of its own and is meant for a trusted LAN.

## The open ports

| Port | What it is |
| --- | --- |
| **4404/tcp** | the virtual node, for the Meshtastic app |
| **8080/tcp** | the web UI and its JSON API |

Neither asks for a password, just like the node's own port 4403. Anything that reaches
them can read the stored history, node list and positions, send messages as your node,
make it transmit requests, change settings and clear the store.

- **Do not forward these ports** to the internet.
- For remote access use a VPN or a reverse proxy that authenticates;
  `VNODE_WEB_HOST=127.0.0.1` keeps the UI local to the machine for that.
- The Home Assistant add-on serves the UI through ingress, behind HA's login, and does not
  publish 8080. Port 4404 stays a plain port, because the app speaks raw TCP.

## What the store contains

The database (`data/vnode.sqlite3`, `/data` in the add-on) is unencrypted SQLite with the
messages, nodes, positions and telemetry. It also holds the node's config exactly as the
node sends it to an app, which includes **key material**: the channel keys, the node's
private key and its WiFi password.

- Treat the database file, and any backup of it, like a password store.
- The same goes for logs taken with `VNODE_TRACE_FRAMES=true`. Never attach one to an issue.
- The API never returns the keys, only a channel's encryption type.

## What connected apps may do

- **Changing the node's settings is blocked** (`allow_admin=false`). Reading them is
  allowed, because apps ask right after connecting.
- **Direct messages stay end-to-end encrypted.** Android leaves the sender field of a
  PKI direct message empty for its node to fill in. mesh-vnode fills in the node number,
  as the node would, and leaves the encryption untouched.

Apps are recognised by IP address (`client_key_mode=ip`) so each gets its own replay
cursor. This is not a security boundary: addresses are easy to spoof on a LAN.

## What leaves your network

Only map tiles, fetched by the browser from OpenFreeMap or CARTO, which reveals the map
area you look at. A CARTO key is sent to `*.basemaps.cartocdn.com` only. The service itself
connects to nothing but your node.

## Reporting a problem

Open an issue. If it needs discretion, say so without the details and we will find
another channel. Please never attach a database or a frame trace.
