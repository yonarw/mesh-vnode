# Mesh vnode

A virtual node that remembers messages, for use with Meshtastic® meshes.

It holds the single TCP connection to your real node, stores every text message
in SQLite, and presents itself to the Meshtastic phone app as a node on TCP.
When the app connects it gets the node's config and then the messages it missed,
so the phone no longer has to hold a socket open all day to avoid losing DMs.

## Setting it up

1. Set **Node address** (`upstream_host`) to your node's **IP address**, then
   start the add-on.
2. Open the web UI from the sidebar. The Status page should show the node
   connected and the config captured.
3. In the Meshtastic app, add a node over **Network**: host is the machine
   running Home Assistant, port **4404**.
4. Stop anything else that connects to the node directly - see Troubleshooting.

The host port for 4404 can be changed in the add-on's *Network* panel if something else
on the machine already uses it.

### Use the IP, not `meshtastic.local`

mDNS names do not resolve inside the add-on's container. The default is
`meshtastic.local` only because that is the standalone default; it will not work
here. Give the node a static lease on your router and use that address.

The address can also be changed later in the web UI (⚙), which then wins over
this option until you clear it there.

## Where things are reachable

**The web UI** is served through Home Assistant's ingress: it appears in the
sidebar and is protected by your Home Assistant login. Its port is deliberately
not published to the network - the only way in is through Home Assistant.

**Port 4404** is a real port on your Home Assistant machine, because the phone
app speaks raw TCP and ingress cannot proxy that. It is reachable from your LAN
and, like the node's own port 4403, it is not authenticated: anything that can
reach it can read the stored history and send to the mesh. That is fine on a
trusted network; do not forward it from the internet.

## Options

| Option | What it does |
| --- | --- |
| `upstream_host`, `upstream_port` | The real node. Use an IP (see above). |
| `allow_admin` | Let connected apps change the node's settings through the virtual node. Off by default, and there is a switch for it in the web UI too. |
| `retention_days` | How long stored messages are kept. |
| `log_level` | `DEBUG` is very loud. |

Everything else is a default that has not needed changing. Should you ever need one, the
full list is in the repository's `docs/configuration.md`; the add-on reads any `VNODE_*`
variable as well.

## Data

Messages, settings and per-client cursors live in the add-on's `/data`, so they
survive restarts and updates. Removing the add-on deletes them.

The add-on also keeps the node's config handshake verbatim, because that is what
it replays to a connecting app. That capture includes your channel keys, the
node's private key and the WiFi credentials from its network config - the same
set the node hands any phone that connects to it, but here it sits in a file.
Treat a copy of `/data` like a password store, and never attach a log taken with
`log_level: DEBUG` and frame tracing to a bug report.

## The map

The map needs no account: it uses OpenFreeMap (OpenStreetMap data) by default.
CARTO basemaps are available under Settings in the web UI and can take a free
key. Map tiles are the only thing that leaves your network, and only the browser
fetches them.

## Troubleshooting

**Messages are missing, or the node's battery drains.** Something else is talking to the
node at the same time. A node serves one TCP client and hands the link to whoever
connects last, so MeshMonitor or a phone pointed straight at the node will fight this
add-on for it. Point them at port 4404 instead.

**The app sees duplicates after reconnecting.** The client is not being
recognised again - check whether its IP changed, and give the phone a static
lease.

**Upstream never connects.** Almost always `meshtastic.local` in the node
address. Use the IP.

---

Meshtastic® is a registered trademark of Meshtastic LLC. Meshtastic software components are released under various licenses, see [GitHub](https://github.com/meshtastic) for details. No warranty is provided - use at your own risk.

This add-on is not affiliated with or endorsed by the Meshtastic project.
