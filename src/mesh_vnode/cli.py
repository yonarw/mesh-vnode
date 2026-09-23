"""Command line entry points."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import typer
import uvicorn

from . import protocol as proto
from .config import Settings, config_file, load_settings
from .db import Database
from .framing import FrameDecoder, encode_frame
from .logging_setup import configure

app = typer.Typer(
    add_completion=False,
    help="A virtual node with a message store, for use with Meshtastic meshes.",
)


def _settings(**kw) -> Settings:
    s = load_settings(**kw)
    configure(
        level=s.log_level,
        trace_frames=s.trace_frames,
        meshtastic_level=s.meshtastic_log_level,
        log_file=s.log_file,
        max_bytes=s.log_max_bytes,
        backups=s.log_backups,
        console=s.log_console,
    )
    # Only now is there somewhere for it to go. Silence here would mean a typo in
    # VNODE_CONFIG_FILE looks exactly like a setting that did not take effect.
    path = config_file()
    if path is not None:
        logging.getLogger(__name__).info("vnode: reading settings from %s", path)
    return s


@app.command()
def run(
    upstream_host: str | None = typer.Option(None, "--node", help="Real node hostname or IP"),
    upstream_port: int | None = typer.Option(None, "--node-port"),
    listen_port: int | None = typer.Option(None, "--port", help="Virtual node TCP port"),
    web_port: int | None = typer.Option(None, "--web-port"),
    db_path: Path | None = typer.Option(None, "--db"),
    replay_mode: str | None = typer.Option(None, "--replay", help="cursor | none"),
    log_level: str | None = typer.Option(None, "--log-level"),
) -> None:
    """Run the service: upstream link, virtual node and web UI."""
    from .web import create_app

    s = _settings(
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        listen_port=listen_port,
        web_port=web_port,
        db_path=db_path,
        replay_mode=replay_mode,
        log_level=log_level,
    )
    debug = s.log_level.upper() == "DEBUG"
    # log_config=None keeps uvicorn on our handlers and format. The access log
    # is one line per web-UI poll, so it is off unless debugging: uvicorn resets
    # its own logger levels at startup, so silencing it any other way does not
    # stick.
    uvicorn.run(
        create_app(s, cli_upstream=upstream_host is not None),
        host=s.web_host,
        port=s.web_port,
        log_level="debug" if debug else "info",
        log_config=None,
        access_log=debug,
    )


@app.command()
def capture(
    upstream_host: str | None = typer.Option(None, "--node"),
    upstream_port: int | None = typer.Option(None, "--node-port"),
    db_path: Path | None = typer.Option(None, "--db"),
    seconds: int = typer.Option(60, "--seconds", "-s", help="How long to listen after config"),
) -> None:
    """Connect to a node, capture the handshake and log what arrives.

    No downstream server, no phone. Confirms the raw-bytes capture works against
    a given node before anything else is pointed at it.
    """
    from .upstream import Upstream

    s = _settings(upstream_host=upstream_host, upstream_port=upstream_port, db_path=db_path)
    db = Database(s.db_path)
    up = Upstream(s, db)

    def on_frame(raw: bytes, ctx) -> None:
        fr = proto.parse_from_radio(raw)
        which = fr.WhichOneof("payload_variant")
        if which == "packet" and fr.packet.HasField("decoded"):
            port = int(fr.packet.decoded.portnum)
            note = f" text={fr.packet.decoded.payload[:80]!r}" if port == proto.PORT_TEXT else ""
            typer.echo(
                f"[{time.strftime('%H:%M:%S')}] packet id={fr.packet.id} "
                f"from={proto.node_id(fr.packet.__getattribute__('from'))} "
                f"to={proto.node_id(fr.packet.to)} port={port} seq={ctx.seq}{note}"
            )
        else:
            typer.echo(f"[{time.strftime('%H:%M:%S')}] {which} ({len(raw)} bytes)")

    up.add_raw_sink(on_frame)
    up.add_state_sink(lambda state, detail: typer.echo(f"upstream: {state} - {detail}"))
    up.start()

    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        up.stop()

    frames = db.config_frames()
    typer.echo(f"\ncaptured {len(frames)} config frames:")
    for row in frames:
        typer.echo(
            f"  {row['ord']:>3} {row['kind']:<13} {row['key']:<28} {len(row['raw']):>4} bytes"
        )
    typer.echo(f"\nstore: {json.dumps(db.counts())}")
    db.close()


@app.command()
def fakenode(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(4403, "--port"),
    interval: float = typer.Option(10.0, "--interval", help="Seconds between fake packets"),
) -> None:
    """Run a fake node on TCP so the service can be tested without hardware."""
    from .fakenode import FakeNode

    configure(level="INFO")
    asyncio.run(FakeNode(host, port, interval).serve())


@app.command()
def probe(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(4404, "--port"),
    nonce: int = typer.Option(0, "--nonce", help="want_config_id to send (0 = random)"),
    seconds: float = typer.Option(8.0, "--seconds", "-s"),
    send: str | None = typer.Option(None, "--send", help="Send this text after the handshake"),
) -> None:
    """Act like the phone app: connect, ask for config, print everything received.

    A phone app stand-in for testing replay without a phone in hand: run it twice
    and compare what the second run receives.
    """
    import random

    from meshtastic.protobuf import mesh_pb2

    configure(level="INFO")
    cfg_nonce = nonce or random.randint(1, 0xFFFFFFF0)

    async def main() -> None:
        reader, writer = await asyncio.open_connection(host, port)
        tr = mesh_pb2.ToRadio()
        tr.want_config_id = cfg_nonce
        writer.write(encode_frame(tr.SerializeToString()))
        await writer.drain()
        typer.echo(f"-> want_config_id={cfg_nonce}")

        decoder = FrameDecoder()
        counts: dict[str, int] = {}
        texts: list[str] = []
        deadline = time.time() + seconds
        sent = False

        while time.time() < deadline:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=0.5)
            except TimeoutError:
                if send and not sent:
                    pkt = mesh_pb2.ToRadio()
                    pkt.packet.to = proto.BROADCAST_NUM
                    pkt.packet.id = random.randint(1, 0xFFFFFFF0)
                    pkt.packet.decoded.portnum = proto.PORT_TEXT
                    pkt.packet.decoded.payload = send.encode()
                    pkt.packet.want_ack = True
                    writer.write(encode_frame(pkt.SerializeToString()))
                    await writer.drain()
                    typer.echo(f"-> text {send!r} (id={pkt.packet.id})")
                    sent = True
                continue
            if not data:
                break
            for payload in decoder.feed(data):
                fr = mesh_pb2.FromRadio()
                fr.ParseFromString(payload)
                # None: a variant newer than the package (e.g. region_presets).
                which = fr.WhichOneof("payload_variant") or "unknown"
                counts[which] = counts.get(which, 0) + 1
                if which == "packet" and fr.packet.HasField("decoded"):
                    portnum = int(fr.packet.decoded.portnum)
                    if portnum == proto.PORT_TEXT:
                        text = fr.packet.decoded.payload.decode("utf-8", "replace")
                        texts.append(text)
                        typer.echo(
                            f"<- text id={fr.packet.id} "
                            f"from={proto.node_id(fr.packet.__getattribute__('from'))} "
                            f"to={proto.node_id(fr.packet.to)} "
                            f"hops={fr.packet.hop_start}/{fr.packet.hop_limit} {text!r}"
                        )
                    else:
                        typer.echo(f"<- packet port={portnum} id={fr.packet.id}")
                elif which == "config_complete_id":
                    typer.echo(f"<- config_complete_id={fr.config_complete_id}")
                else:
                    typer.echo(f"<- {which}")

        writer.close()
        typer.echo(f"\nframe types: {json.dumps(counts)}")
        typer.echo(f"text messages received: {len(texts)}")
        dupes = len(texts) - len(set(texts))
        if dupes:
            typer.echo(f"duplicate texts in this session: {dupes}")

    asyncio.run(main())


@app.command()
def inspect(db_path: Path | None = typer.Option(None, "--db")) -> None:
    """Show what is in the store."""
    s = _settings(db_path=db_path)
    db = Database(s.db_path)
    typer.echo(f"db: {s.db_path}")
    typer.echo(f"counts: {json.dumps(db.counts())}")
    typer.echo(f"max seq: {db.max_seq()}")

    typer.echo("\nconfig frames:")
    for row in db.config_frames():
        typer.echo(
            f"  {row['ord']:>3} {row['kind']:<13} {row['key']:<28} {len(row['raw']):>4} bytes"
        )

    typer.echo("\nclients:")
    for row in db.clients():
        seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["last_seen"]))
        typer.echo(
            f"  {row['client_key']:<24} cursor={row['last_seq']:<6} "
            f"connects={row['connects']:<4} replayed={row['replayed']:<5} last_seen={seen}"
        )

    typer.echo("\nlast 15 texts:")
    for row in reversed(db.messages(limit=15)):
        ts = time.strftime("%m-%d %H:%M", time.localtime(row["rx_time"]))
        to = "^all" if row["to_num"] == proto.BROADCAST_NUM else proto.node_id(row["to_num"])
        typer.echo(
            f"  [{row['seq']:>5}] {ts} {proto.node_id(row['from_num'])} -> {to}: {row['text']}"
        )
    db.close()


if __name__ == "__main__":
    app()
