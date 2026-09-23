"""Channels, messages, conversations and sending."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from .. import protocol as proto
from ..app import VNodeApp
from .common import Names, Vnode, require_upstream, rows

router = APIRouter()


# What fits in one text packet's payload: the firmware's 233-byte Data limit
# minus the port number and the payload's own field header.
MAX_TEXT_BYTES = 228


class SendRequest(BaseModel):
    text: str = Field(min_length=1)
    channel: int = 0
    to: int | str = proto.BROADCAST_NUM
    want_ack: bool = True
    # A reaction: `text` is the emoji and `reply_id` the packet it reacts to.
    reply_id: int | None = None
    emoji: bool = False

    @field_validator("text")
    @classmethod
    def _fits_one_packet(cls, text: str) -> str:
        if len(text.encode()) > MAX_TEXT_BYTES:
            raise ValueError(f"longer than {MAX_TEXT_BYTES} bytes")
        return text


def lora_config(vnode: VNodeApp):
    for row in vnode.db.config_frames(kinds=["config"]):
        fr = proto.parse_from_radio(row["raw"])
        if fr.config.WhichOneof("payload_variant") == "lora":
            return fr.config.lora
    return None


@router.get("/channels")
def channels(vnode: Vnode) -> list[dict[str, Any]]:
    frames = [proto.parse_from_radio(r["raw"]).channel for r in vnode.db.config_frames(["channel"])]
    return proto.channel_info(frames, lora_config(vnode))


@router.get("/messages")
def messages(
    vnode: Vnode,
    limit: int = Query(200, ge=1, le=1000),
    before_seq: int | None = None,
    channel: int | None = None,
    node: int | None = None,
) -> list[dict[str, Any]]:
    """Messages oldest first, with sender names and reactions folded in.

    A reaction is its own text packet (emoji set, reply_id pointing at the
    target). One whose target is not in this page stays in the list, flagged.
    """
    found = rows(
        vnode.db.messages(limit=limit, before_seq=before_seq, channel=channel, node_num=node)
    )
    names = Names(vnode.db)
    for r in found:
        r["from_id"] = proto.node_id(r["from_num"])
        r["to_id"] = "^all" if r["to_num"] == proto.BROADCAST_NUM else proto.node_id(r["to_num"])
        r["is_dm"] = r["to_num"] != proto.BROADCAST_NUM
        r["sender"] = names.label(r["from_num"])
        r["reactions"] = []

    by_packet = {r["packet_id"]: r for r in found if not r["emoji"]}
    out = []
    for r in reversed(found):
        target = by_packet.get(r["reply_id"]) if r["emoji"] and r["reply_id"] else None
        if target is not None:
            target["reactions"].append(
                {"emoji": r["text"], "from_num": r["from_num"], "sender": r["sender"]}
            )
            continue
        r["is_reaction"] = bool(r["emoji"])
        out.append(r)
    return out


@router.get("/messages/{seq}")
def message_details(vnode: Vnode, seq: int) -> dict[str, Any]:
    """One message with its reception details, or the delivery log for one we sent."""
    row = vnode.db.packet(seq)
    if row is None:
        raise HTTPException(404, "no such message")
    msg = rows([row])[0]
    names = Names(vnode.db)
    msg["sender"] = names.label(msg["from_num"])
    msg["is_dm"] = msg["to_num"] != proto.BROADCAST_NUM
    msg["recipient"] = names.label(msg["to_num"]) if msg["is_dm"] else None
    log = []
    for entry in rows(vnode.db.delivery_log(msg["packet_id"], msg["from_num"])):
        if entry["ack_from"] is not None:
            entry["ack_from_name"] = names.label(entry["ack_from"])
        if entry["relay_node"]:
            # Only the last byte of the relayer's number travels in the packet.
            # Direct neighbours first: they are the likely ones.
            matches = [
                n for n in names.nodes.values() if n["node_num"] & 0xFF == entry["relay_node"]
            ]
            matches.sort(key=lambda n: (n["hops_away"] != 0, -(n["last_heard"] or 0)))
            entry["relay_candidates"] = [names.label(n["node_num"]) for n in matches[:5]]
        log.append(entry)
    msg["delivery"] = log
    return msg


@router.get("/conversations")
def conversations(vnode: Vnode) -> dict[str, Any]:
    names = Names(vnode.db)
    direct = []
    for peer in vnode.db.dm_peers(vnode.upstream.my_node_num or 0):
        node = names.nodes.get(peer["node_num"])
        direct.append(
            {
                **dict(peer),
                "long_name": node["long_name"] if node else None,
                "short_name": node["short_name"] if node else None,
                "node_id": proto.node_id(peer["node_num"]),
            }
        )
    return {"channels": channels(vnode), "direct": direct}


@router.post("/send")
async def send(vnode: Vnode, req: SendRequest) -> dict[str, Any]:
    require_upstream(vnode)
    try:
        packet = await asyncio.to_thread(
            vnode.upstream.send_text,
            req.text,
            destination=req.to,
            channel_index=req.channel,
            want_ack=req.want_ack,
            reply_id=req.reply_id,
            emoji=req.emoji,
        )
    except Exception as exc:
        raise HTTPException(502, f"send failed: {exc}") from exc
    seq = await asyncio.to_thread(vnode.upstream.store_client_packet, packet, "webui")
    if seq is not None:
        vnode.server.mirror_outgoing(packet, seq)
    return {"status": "ok", "packet_id": packet.id, "seq": seq}
