# Security considerations

This is a LAN tool. It adds no authentication of its own, and it keeps a copy of things
your node would otherwise only hand to a phone over a single cable-like connection. None
of that is a problem on a home network you control; all of it matters if you put it
somewhere else. What follows is the honest list.

## The two open ports

| Port | What it is | Who can use it |
| --- | --- | --- |
| **4404/tcp** | the virtual node, the Meshtastic protocol | anything that can open a socket |
| **8080/tcp** | the web UI and its JSON API | anything that can send an HTTP request |

Neither asks for a password, exactly like the node's own port 4403. Whatever reaches
them can read the entire stored history, see your node list and positions, and **send
messages to the mesh as your node**. `POST /api/clear` deletes the store,
`PUT /api/prefs` changes settings.

- Keep both on a trusted network. **Do not port-forward them**, and do not put them on a
  guest or IoT VLAN you consider hostile.
- Bind them narrower if you want: `VNODE_WEB_HOST=127.0.0.1` makes the UI local-only and
  leaves it reachable through a reverse proxy that does authenticate.
- For remote access, put it behind something that authenticates - a VPN (WireGuard,
  Tailscale) or a reverse proxy with a login. The Home Assistant add-on does this for the
  UI: ingress means Home Assistant's own login, and the UI port is not published at all.
  Port 4404 still has to be a real port, because the phone app speaks nothing but raw
  TCP.

## What the store contains

The database (`data/vnode.sqlite3`, or `/data` in the add-on) is an unencrypted SQLite
file. It holds the message text, node names, positions and telemetry - and, because the
node's config handshake is captured verbatim so it can be replayed to clients, it also
holds **key material**:

- your channels' pre-shared keys,
- the node's private key from its security config,
- the WiFi SSID and password in the node's network config.

That is the same set a phone receives when it connects to the node directly - but here it
is at rest in a file. So:

- Treat the database file like a password store. Do not commit it, do not put it in a
  backup you share, do not paste it into an issue.
- The same applies to logs captured with `VNODE_TRACE_FRAMES=true`, which hex-dumps every
  frame on both sockets. Never attach one of those to a bug report.
- `data/` is in `.gitignore` for this reason. The JSON API does not expose any of it: it
  reports a channel's *encryption class* ("aes256"), never the key.

Deleting everything is one call - `POST /api/clear`, or the button in the web UI - and it
keeps your settings.

## What connected apps may do

By default, an app connected to 4404 can do everything a phone normally does: read, send
text, set favourites. Two things are gated:

- **Admin writes are blocked.** `allow_admin=false` (the default) drops admin messages
  bound for the node, so an app cannot change the node's settings, channels or keys
  through the virtual node. Read-only admin requests still pass
  (`allow_admin_reads=true`), because apps ask for those right after connecting and the
  answers are already in the handshake.
- **Direct messages stay end-to-end encrypted.** Android sends PKI direct messages with
  the sender field left at zero, expecting its own node to fill it in; through a proxy
  that never happens and the node drops them. The only repair made here is stamping in
  the node number, exactly as the node would have - the sealing is untouched, so a DM sent
  through this service is as private as one sent from a directly connected app. There is
  no setting that weakens this.

## Clients are identified by IP

`client_key_mode=ip` (the default) recognises a returning app by its address. It is what
makes "only the messages you missed" work across reconnects, and it is not a security
boundary: two devices behind the same address share a cursor, and an address is
trivially spoofable on a LAN. `ip_port` treats every connection as new; `shared` gives
all clients one cursor.

## What leaves your network

Only basemap tiles, and only from the browser: the map style, tiles, glyphs and sprites
come from OpenFreeMap (default) or CARTO. Tile requests reveal which part of the world
you are looking at to that provider. No message, node, position or telemetry is ever sent
anywhere - the Python process makes no outbound connection other than to your node.

If even that is too much, OpenFreeMap can be self-hosted, and MapLibre will happily point
at your own tile server; that is a one-line change in `webui/src/views/Map.tsx`.

A CARTO key, if you set one, is stored in the database and sent by the browser with each
tile request to `*.basemaps.cartocdn.com` only.

## Reporting something

Open an issue - or, if it looks like it needs discretion, say so in the issue without the
details and we will find a better channel. Please include what you did, not a database
dump.
