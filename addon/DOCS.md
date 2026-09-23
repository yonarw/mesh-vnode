# Mesh vnode

A virtual node that remembers messages, for use with Meshtastic® meshes. It holds the
connection to your node, stores the messages, and serves them to the Meshtastic app on
port 4404 - including the ones the app missed while it was away.

## Setting it up

1. Set `upstream_host` to your node's **IP address** (`meshtastic.local` does not resolve
   inside the add-on) and start the add-on.
2. Open the web UI from the sidebar. The Status page should show the node connected.
3. In the Meshtastic app, add a node over **Network**: your Home Assistant machine, port
   **4404**.
4. Stop anything else that connects to the node directly and point it at 4404 instead.

## Options

| Option | What it does |
| --- | --- |
| `upstream_host`, `upstream_port` | The node. The web UI (⚙) can override the address. |
| `allow_admin` | Let apps change the node's settings. Also a switch in the web UI. |
| `retention_days` | How long stored messages are kept. |
| `log_level` | `DEBUG` is very loud. |

The add-on reads any `VNODE_*` setting as well - see
[Configuration](https://github.com/yonarw/mesh-vnode/blob/main/docs/configuration.md). The
host port for 4404 is set in the *Network* panel.

## Security

The web UI is behind your Home Assistant login. Port 4404 is not: anything on your LAN can
use it, so do not forward it. The data in `/data` includes your channel keys and the
node's private key; treat a copy of it like a password store. Details:
[Security](https://github.com/yonarw/mesh-vnode/blob/main/docs/security.md).

## Troubleshooting

- **Messages are missing, or the node's battery drains:** something else is connected to
  the node directly. Point it at port 4404.
- **The app gets duplicates after reconnecting:** the phone's IP changed; give it a static
  lease.
- **The node never connects:** use its IP, not `meshtastic.local`.

---

Meshtastic® is a registered trademark of Meshtastic LLC. This add-on is not affiliated with
or endorsed by the Meshtastic project, and comes with no warranty.
