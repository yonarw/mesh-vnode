# Development

```sh
uv sync
uv run pytest                 # no hardware needed
uv run ruff check src tests
npm --prefix webui run dev    # UI with hot reload, proxies /api to :8080
```

## Layout

```
src/mesh_vnode/
  cli.py            typer entry points (run, capture, probe, fakenode, inspect)
  config.py         every VNODE_* setting; env > config file
  upstream.py       the one TCP link to the real node, and its reconnect logic
  vnode_server.py   the TCP server the phone apps connect to; replay and cursors
  protocol.py       framing-level knowledge of the Meshtastic phone protocol
  db.py             SQLite store: packets, nodes, config frames, clients, prefs
  web.py            FastAPI app: JSON API, websocket fan-out, serves webui/dist
  series.py         telemetry and position history for the graphs
  fakenode.py       a simulated node, for tests and for running without hardware
webui/              Vite + React + TypeScript, Tailwind, MapLibre, Recharts
tests/              pytest; no hardware, no network
addon/              Home Assistant add-on manifest and its documentation
```

## Commands

| command | what it does |
| --- | --- |
| `run` | the service: upstream link, virtual node on 4404, web UI on 8080 |
| `capture` | connect to a node, capture the handshake, log traffic, exit (no server) |
| `probe` | act like the phone app against the virtual node and print what arrives |
| `fakenode` | a fake node on TCP 4403 for testing without hardware |
| `inspect` | dump the store: config frames, clients, cursors, recent texts |

`./start.sh` and `./stop.sh` wrap `run` for everyday use; the rest are
`uv run mesh-vnode <command>`.

## Without hardware

`fakenode` is a simulated node and `probe` a simulated phone app, so the whole path can
be exercised with nothing attached:

```sh
uv run mesh-vnode fakenode --interval 5 &   # terminal 1
./start.sh --node 127.0.0.1                       # terminal 2
uv run mesh-vnode probe --seconds 6         # terminal 3, run it twice
```

The second `probe` run should show far fewer messages than the first. That is the
per-client cursor working.

The fake node also answers what it is asked: a traceroute comes back over a relay with
per-hop SNR, and position, telemetry and node info requests are answered by the three
chatty peers but never by the crowd, so the "no answer" case can be seen too. It asks
this node for something itself now and then, which is what an inbound request looks like
in the node card.

## Web UI

The Python process serves `webui/dist` when that directory exists, so a build is needed
for anything but `npm run dev`:

```sh
npm --prefix webui install
npm --prefix webui run build
```

`start.sh` does this on first run.

## Debugging

- `VNODE_LOG_LEVEL=DEBUG` explains every routing decision.
- `VNODE_TRACE_FRAMES=true` hex-dumps every frame on both sockets. Very loud, and the
  dump contains key material - see [security.md](security.md#what-the-store-contains)
  before sharing one.
- `uv run mesh-vnode inspect` shows what the store actually holds.

## Packaging the add-on locally

A **local add-on** needs no repository, no registry and no image tag: the Supervisor builds
any folder it finds in `/addons`. `./addon_local.sh` packages a checkout as one, which is
how to try add-on changes before publishing an image.

```sh
./addon_local.sh          # -> build/mesh_vnode/
./addon_local.sh --zip    # -> build/mesh_vnode.zip
```

Copy the folder to `/addons` on the Home Assistant machine - the *Samba* add-on shares it
as `\\<ha>\addons`, or `scp -r build/mesh_vnode root@<ha>:/addons/` - then
**Add-on store → ⋮ → Check for updates** and it appears under *Local add-ons*. After a
re-copy use **⋮ → Rebuild**, since the version has not changed.

The packaging step is not only a copy: it drops the `image:` key (that key is what makes
the Supervisor pull a published tag instead of building the `Dockerfile` beside it), puts
`config.yaml` on top, and leaves `.venv`, `node_modules` and `data` out.

## Releasing the add-on image

`addon/config.yaml` names `<image>:<version>` and the Supervisor pulls it, so the tag
built by `.github/workflows/addon-image.yml` (on a `v*` tag) and `version` in that file
have to move together.

## Conventions

- Comments explain *why*, and only where the reason is not obvious from the code.
- Tests are named as sentences (`test_a_style_the_provider_does_not_have_falls_back...`).
- No node IDs, names, addresses or positions from a real mesh in the repository; the
  fixtures are invented and the documentation uses `192.0.2.0/24` or `10.0.0.42`.
