"""SQLite storage.

Two things live here that matter for correctness:

* `packets.raw` holds the **original FromRadio bytes** exactly as the node sent
  them. Replay writes those bytes back out untouched. MeshMonitor rebuilt packets
  from its own tables and got hop counts wrong; storing the originals avoids that
  whole class of problem.
* `clients.last_seq` is the per-client cursor into `packets.seq`, so a reconnect
  only replays what that client has not already been handed.

Access is from both the upstream reader thread and the asyncio loop, so every
statement goes through one connection guarded by a lock. Volumes here are a few
thousand rows a day; this is not a bottleneck.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from . import protocol as proto

SCHEMA_VERSION = 4

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Every stored FromRadio that carries a mesh packet we care about.
CREATE TABLE IF NOT EXISTS packets (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id    INTEGER NOT NULL,
    from_num     INTEGER NOT NULL,
    to_num       INTEGER NOT NULL,
    channel      INTEGER NOT NULL DEFAULT 0,
    portnum      INTEGER NOT NULL,
    rx_time      INTEGER NOT NULL,   -- device clock, seconds
    stored_at    INTEGER NOT NULL,   -- our clock, seconds
    text         TEXT,               -- decoded payload for TEXT_MESSAGE_APP
    want_ack     INTEGER NOT NULL DEFAULT 0,
    hop_limit    INTEGER NOT NULL DEFAULT 0,
    hop_start    INTEGER NOT NULL DEFAULT 0,
    rx_snr       REAL,
    rx_rssi      INTEGER,
    pki          INTEGER NOT NULL DEFAULT 0,
    origin       TEXT,               -- client key that sent it, NULL if from the mesh
    raw          BLOB NOT NULL,
    reply_id     INTEGER,            -- a reaction (emoji set) points at this packet id
    emoji        INTEGER NOT NULL DEFAULT 0,
    status       TEXT,               -- our messages: pending|sent|relayed|delivered|failed
    status_detail TEXT,
    status_at    INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS packets_dedup ON packets(from_num, packet_id);
CREATE INDEX IF NOT EXISTS packets_rx_time ON packets(rx_time);
CREATE INDEX IF NOT EXISTS packets_conv ON packets(channel, to_num, from_num);

-- Ordered capture of the config handshake, replayed verbatim to each client.
CREATE TABLE IF NOT EXISTS config_frames (
    key        TEXT PRIMARY KEY,   -- e.g. "my_info", "channel:0", "config:lora"
    kind       TEXT NOT NULL,      -- my_info | metadata | channel | config |
                                   -- moduleConfig | node_info | other
    ord        INTEGER NOT NULL,   -- order the node sent it in
    raw        BLOB NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    node_num      INTEGER PRIMARY KEY,
    node_id       TEXT,
    long_name     TEXT,
    short_name    TEXT,
    hw_model      TEXT,
    role          TEXT,
    is_licensed   INTEGER NOT NULL DEFAULT 0,
    last_heard    INTEGER,
    snr           REAL,
    hops_away     INTEGER,
    battery_level INTEGER,
    voltage       REAL,
    latitude      REAL,
    longitude     REAL,
    altitude      INTEGER,
    precision_bits INTEGER,        -- 32 = exact position
    position_time INTEGER,
    is_favorite   INTEGER NOT NULL DEFAULT 0,
    is_ignored    INTEGER NOT NULL DEFAULT 0,
    via_mqtt      INTEGER NOT NULL DEFAULT 0,
    public_key    BLOB,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    node_num            INTEGER NOT NULL,
    rx_time             INTEGER NOT NULL,
    kind                TEXT NOT NULL,  -- device | environment | power
    battery_level       INTEGER,
    voltage             REAL,
    channel_utilization REAL,
    air_util_tx         REAL,
    uptime_seconds      INTEGER,
    temperature         REAL,
    relative_humidity   REAL,
    barometric_pressure REAL,
    extra               TEXT
);
CREATE INDEX IF NOT EXISTS telemetry_node_time ON telemetry(node_num, rx_time);
CREATE UNIQUE INDEX IF NOT EXISTS telemetry_dedup ON telemetry(node_num, rx_time, kind);

-- One row per downstream client (phone/tablet), holding its replay cursor.
CREATE TABLE IF NOT EXISTS clients (
    client_key TEXT PRIMARY KEY,
    label      TEXT,
    last_seq   INTEGER NOT NULL DEFAULT 0,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    connects   INTEGER NOT NULL DEFAULT 0,
    replayed   INTEGER NOT NULL DEFAULT 0
);

-- Packets heard per hour and portnum. Every packet is counted here, even the
-- ones not stored, so the traffic chart covers the whole mesh cheaply.
CREATE TABLE IF NOT EXISTS traffic (
    hour    INTEGER NOT NULL,
    portnum INTEGER NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour, portnum)
);

-- Small operational log, surfaced in the web UI so a phone can see node health.
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    level   TEXT NOT NULL,
    source  TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);

-- Everything the node told us about the fate of a message we sent: queued,
-- repeated by a neighbour (implicit ack), acked or refused by the recipient.
-- `status` on the packet is only the furthest point reached; this is the
-- evidence behind it, shown when a message's ticks are tapped.
CREATE TABLE IF NOT EXISTS delivery_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id  INTEGER NOT NULL,
    from_num   INTEGER NOT NULL,   -- our node: the sender of the message
    ts         INTEGER NOT NULL,
    event      TEXT NOT NULL,      -- queued | refused | implicit_ack | ack | nak
    ack_from   INTEGER,            -- who sent the ack or nak
    error      TEXT,
    relay_node INTEGER,            -- last byte of the node that repeated it
    rx_snr     REAL,
    rx_rssi    INTEGER,
    hops       INTEGER
);
CREATE INDEX IF NOT EXISTS delivery_log_packet ON delivery_log(from_num, packet_id);

-- Where the local node and its favourites have been: one row per position
-- report that moved. Everyone else keeps only their latest position in `nodes`.
CREATE TABLE IF NOT EXISTS positions (
    node_num       INTEGER NOT NULL,
    time           INTEGER NOT NULL,
    latitude       REAL NOT NULL,
    longitude      REAL NOT NULL,
    altitude       INTEGER,
    precision_bits INTEGER,
    PRIMARY KEY (node_num, time)
);

-- Requests that ask a node for something rather than telling it something:
-- a traceroute, or a position/telemetry/node info exchange. One row per
-- request, filled in when the answer arrives. `direction` is out for what we
-- or a connected app asked, in for what another node asked of our node - the
-- firmware answers those itself, so we only ever see the question.
CREATE TABLE IF NOT EXISTS exchanges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    kind        TEXT NOT NULL,     -- traceroute | position | telemetry | nodeinfo
    node_num    INTEGER NOT NULL,  -- the node at the other end
    direction   TEXT NOT NULL,     -- out | in
    channel     INTEGER NOT NULL DEFAULT 0,
    packet_id   INTEGER NOT NULL DEFAULT 0,
    origin      TEXT,              -- webui, or the client key of the app that asked
    status      TEXT NOT NULL,     -- sent | answered | failed | expired | heard
    response_ts INTEGER,
    result      TEXT,              -- JSON: the route, position or metrics that came back
    error       TEXT
);
CREATE INDEX IF NOT EXISTS exchanges_node ON exchanges(node_num, ts);
CREATE INDEX IF NOT EXISTS exchanges_open ON exchanges(status, packet_id);

-- Settings made in the web UI (node address, map key, muted conversations,
-- telemetry layout). JSON values. They override the VNODE_* environment, and
-- a command-line flag overrides them.
CREATE TABLE IF NOT EXISTS prefs (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


# Columns added before migrations were versioned; only databases older than
# version 4 can lack them.
LEGACY_COLUMNS = [
    ("nodes", "is_favorite", "INTEGER NOT NULL DEFAULT 0"),
    ("nodes", "is_ignored", "INTEGER NOT NULL DEFAULT 0"),
    ("nodes", "via_mqtt", "INTEGER NOT NULL DEFAULT 0"),
    ("nodes", "precision_bits", "INTEGER"),
    ("nodes", "position_time", "INTEGER"),
    ("nodes", "public_key", "BLOB"),
    ("packets", "reply_id", "INTEGER"),
    ("packets", "emoji", "INTEGER NOT NULL DEFAULT 0"),
    ("packets", "status", "TEXT"),
    ("packets", "status_detail", "TEXT"),
    ("packets", "status_at", "INTEGER"),
]

# Delivery states in the order they may advance. A late "sent" must never
# overwrite "delivered"; "failed" wins over anything short of delivered.
STATUS_RANK = {"pending": 0, "sent": 1, "relayed": 2, "delivered": 3}


def _now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._batch_depth = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            version = self._stored_version()
            self._conn.executescript(SCHEMA)
            self._migrate(version)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._commit()

    def _stored_version(self) -> int:
        """The schema version on disk: current for a new file, 0 for one that
        predates the version stamp."""
        tables = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master")}
        if "packets" not in tables:
            return SCHEMA_VERSION
        if "meta" not in tables:
            return 0
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row[0]) if row else 0

    def _migrate(self, version: int) -> None:
        if version < 4:
            for table, column, decl in LEGACY_COLUMNS:
                existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            # Version 1 also stored position, node info and telemetry packets.
            self._conn.execute("DELETE FROM packets WHERE portnum != ?", (proto.PORT_TEXT,))
            self._backfill_reply_fields()

    def _backfill_reply_fields(self) -> None:
        """Fill reply_id/emoji for rows stored before those columns existed,
        from the raw bytes kept for every packet."""
        rows = self._conn.execute("SELECT seq, raw FROM packets WHERE reply_id IS NULL").fetchall()
        for row in rows:
            try:
                decoded = proto.parse_from_radio(row["raw"]).packet.decoded
                reply_id, emoji = decoded.reply_id, decoded.emoji
            except Exception:
                reply_id, emoji = 0, 0
            self._conn.execute(
                "UPDATE packets SET reply_id = ?, emoji = ? WHERE seq = ?",
                (reply_id, emoji, row["seq"]),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- helpers

    @contextlib.contextmanager
    def batch(self):
        """Hold the lock and commit once at the end rather than per statement."""
        with self._lock:
            self._batch_depth += 1
            try:
                yield
            finally:
                self._batch_depth -= 1
                self._commit()

    def _commit(self) -> None:
        if not self._batch_depth:
            self._conn.commit()

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._commit()
            return cur

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---------------------------------------------------------------- packets

    def store_packet(self, *, raw: bytes, meta: dict[str, Any]) -> int | None:
        """Insert a packet. Returns its seq, or None if it was already stored.

        Duplicates are expected: the node re-delivers packets it has seen more
        than once over the mesh.
        """
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO packets
                    (packet_id, from_num, to_num, channel, portnum, rx_time, stored_at,
                     text, want_ack, hop_limit, hop_start, rx_snr, rx_rssi, pki, origin, raw,
                     reply_id, emoji, status, status_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    meta.get("packet_id", 0),
                    meta.get("from_num", 0),
                    meta.get("to_num", 0),
                    meta.get("channel", 0),
                    meta.get("portnum", 0),
                    meta.get("rx_time") or _now(),
                    _now(),
                    meta.get("text"),
                    int(bool(meta.get("want_ack"))),
                    meta.get("hop_limit", 0),
                    meta.get("hop_start", 0),
                    meta.get("rx_snr"),
                    meta.get("rx_rssi"),
                    int(bool(meta.get("pki"))),
                    meta.get("origin"),
                    raw,
                    meta.get("reply_id", 0),
                    meta.get("emoji", 0),
                    # Only our own messages have a delivery state to track.
                    "pending" if meta.get("origin") else None,
                    _now() if meta.get("origin") else None,
                ),
            )
            self._commit()
            return cur.lastrowid if cur.rowcount else None

    def _after_filter(
        self,
        seq: int,
        min_rx_time: int | None,
        portnums: Iterable[int] | None,
        exclude_origin: str | None,
    ) -> tuple[str, list[Any]]:
        sql = " FROM packets WHERE seq > ?"
        params: list[Any] = [seq]
        if min_rx_time is not None:
            sql += " AND rx_time >= ?"
            params.append(min_rx_time)
        if portnums is not None:
            nums = list(portnums)
            sql += f" AND portnum IN ({','.join('?' * len(nums))})"
            params.extend(nums)
        if exclude_origin is not None:
            sql += " AND (origin IS NULL OR origin != ?)"
            params.append(exclude_origin)
        return sql, params

    def count_after(
        self,
        seq: int,
        *,
        min_rx_time: int | None = None,
        portnums: Iterable[int] | None = None,
        exclude_origin: str | None = None,
    ) -> int:
        where, params = self._after_filter(seq, min_rx_time, portnums, exclude_origin)
        return int(self._query("SELECT COUNT(*) AS n" + where, params)[0]["n"])

    def packets_after(
        self,
        seq: int,
        *,
        limit: int,
        offset: int = 0,
        min_rx_time: int | None = None,
        portnums: Iterable[int] | None = None,
        exclude_origin: str | None = None,
    ) -> list[sqlite3.Row]:
        where, params = self._after_filter(seq, min_rx_time, portnums, exclude_origin)
        sql = "SELECT *" + where + " ORDER BY seq ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return self._query(sql, params)

    def find_own_packet(self, packet_id: int, from_num: int) -> sqlite3.Row | None:
        rows = self._query(
            "SELECT seq, status FROM packets WHERE packet_id = ? AND from_num = ? "
            "AND origin IS NOT NULL",
            (packet_id, from_num),
        )
        return rows[0] if rows else None

    def update_status(
        self, packet_id: int, from_num: int, status: str, detail: str | None = None
    ) -> dict[str, Any] | None:
        """Advance the delivery state of a message we sent.

        Returns the updated row's identity when something changed, so callers
        can tell the web UI; None when the packet is not ours or the new state
        would be a step backwards.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT seq, status FROM packets WHERE packet_id = ? AND from_num = ? "
                "AND origin IS NOT NULL",
                (packet_id, from_num),
            ).fetchone()
            if row is None:
                return None
            current = row["status"]
            if current == "delivered":
                return None
            if status != "failed":
                if current == "failed":
                    # Only a real ack after a failure counts - the message did
                    # get through late. A stray "sent" does not undo a failure.
                    if status not in ("relayed", "delivered"):
                        return None
                elif STATUS_RANK.get(status, -1) <= STATUS_RANK.get(current or "", -1):
                    return None
            self._conn.execute(
                "UPDATE packets SET status = ?, status_detail = ?, status_at = ? WHERE seq = ?",
                (status, detail, _now(), row["seq"]),
            )
            self._commit()
            return {"seq": row["seq"], "packet_id": packet_id, "status": status, "detail": detail}

    def max_seq(self) -> int:
        row = self._query("SELECT COALESCE(MAX(seq), 0) AS m FROM packets")[0]
        return int(row["m"])

    def messages(
        self,
        *,
        limit: int = 200,
        before_seq: int | None = None,
        channel: int | None = None,
        node_num: int | None = None,
    ) -> list[sqlite3.Row]:
        """Text messages for the web UI, newest first."""
        sql = "SELECT * FROM packets WHERE portnum = ?"
        params: list[Any] = [proto.PORT_TEXT]
        if before_seq is not None:
            sql += " AND seq < ?"
            params.append(before_seq)
        if channel is not None:
            sql += " AND channel = ? AND to_num = ?"
            params.extend([channel, proto.BROADCAST_NUM])
        if node_num is not None:
            sql += " AND (from_num = ? OR to_num = ?)"
            params.extend([node_num, node_num])
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        return self._query(sql, params)

    def dm_peers(self, my_num: int) -> list[sqlite3.Row]:
        """Everyone we have direct messages with, most recent first, with the
        latest text (SQLite takes bare columns from the MAX() row)."""
        return self._query(
            "SELECT CASE WHEN from_num = ? THEN to_num ELSE from_num END AS node_num, "
            "COUNT(*) AS count, MAX(rx_time) AS last_time, text AS last_text "
            "FROM packets WHERE portnum = ? AND emoji = 0 AND to_num != ? "
            "GROUP BY node_num ORDER BY last_time DESC",
            (my_num, proto.PORT_TEXT, proto.BROADCAST_NUM),
        )

    def count_traffic(self, rx_time: int, portnum: int) -> None:
        hour = (rx_time // 3600) * 3600
        self._exec(
            "INSERT INTO traffic(hour, portnum, count) VALUES (?, ?, 1) "
            "ON CONFLICT(hour, portnum) DO UPDATE SET count = count + 1",
            (hour, portnum),
        )

    def traffic(self, since: int) -> list[sqlite3.Row]:
        return self._query(
            "SELECT hour, portnum, count FROM traffic WHERE hour >= ? ORDER BY hour", (since,)
        )

    def counts(self) -> dict[str, int]:
        row = self._query(
            """
            SELECT
              (SELECT COUNT(*) FROM packets)                AS packets,
              (SELECT COUNT(*) FROM packets WHERE portnum = ?) AS texts,
              (SELECT COUNT(*) FROM nodes)                  AS nodes,
              (SELECT COUNT(*) FROM telemetry)              AS telemetry
            """,
            (proto.PORT_TEXT,),
        )[0]
        return {k: int(row[k]) for k in row.keys()}  # noqa: SIM118 - sqlite3.Row

    def prune(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = _now() - retention_days * 86400
        n = self._exec("DELETE FROM packets WHERE rx_time < ?", (cutoff,)).rowcount
        self._exec("DELETE FROM telemetry WHERE rx_time < ?", (cutoff,))
        self._exec("DELETE FROM events WHERE ts < ?", (cutoff,))
        self._exec("DELETE FROM traffic WHERE hour < ?", (cutoff,))
        self._exec("DELETE FROM delivery_log WHERE ts < ?", (cutoff,))
        self._exec("DELETE FROM positions WHERE time < ?", (cutoff,))
        self._exec("DELETE FROM exchanges WHERE ts < ?", (cutoff,))
        return n

    CLEARED_TABLES = (
        "packets",
        "nodes",
        "telemetry",
        "positions",
        "traffic",
        "delivery_log",
        "events",
        "clients",
        "exchanges",
    )

    def clear_preview(self) -> dict[str, int]:
        """Rows `clear_data` would delete, per table."""
        with self._lock:
            return {
                t: self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in self.CLEARED_TABLES
            }

    def clear_data(self) -> dict[str, int]:
        """Start over: delete messages, nodes, telemetry, tracks, traffic
        counts, the delivery log, events and every client with its cursor,
        and restart the message numbering at 1.

        Kept: web-UI settings, and the captured config (replaced by the next
        capture, so an app connecting meanwhile still gets a full handshake).
        Connected apps must be dropped too (VNodeServer.disconnect_all): their
        cursors are gone, and they reconnect from scratch.
        Returns the rows deleted per table.
        """
        removed: dict[str, int] = {}
        with self._lock:
            for table in self.CLEARED_TABLES:
                removed[table] = self._conn.execute(f"DELETE FROM {table}").rowcount
            self._conn.execute("DELETE FROM sqlite_sequence")
            self._commit()
            self._conn.execute("VACUUM")
        return removed

    # ---------------------------------------------------------- config frames

    def config_frames(self, kinds: Iterable[str] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM config_frames"
        params: list[Any] = []
        if kinds is not None:
            ks = list(kinds)
            sql += f" WHERE kind IN ({','.join('?' * len(ks))})"
            params.extend(ks)
        sql += " ORDER BY ord ASC"
        return self._query(sql, params)

    def replace_config_frames(self, frames: list[tuple[str, str, int, bytes]]) -> None:
        """Swap in a freshly captured handshake atomically."""
        with self._lock:
            self._conn.execute("DELETE FROM config_frames")
            self._conn.executemany(
                "INSERT INTO config_frames(key, kind, ord, raw, updated_at) VALUES (?,?,?,?,?)",
                [(k, kind, o, raw, _now()) for k, kind, o, raw in frames],
            )
            self._commit()

    # ------------------------------------------------------------------ nodes

    def upsert_node(self, node_num: int, fields: dict[str, Any]) -> None:
        fields = {k: v for k, v in fields.items() if v is not None}
        fields["updated_at"] = _now()
        cols = ", ".join(fields)
        placeholders = ", ".join("?" * len(fields))
        updates = ", ".join(f"{c}=excluded.{c}" for c in fields)
        self._exec(
            f"INSERT INTO nodes(node_num, {cols}) VALUES (?, {placeholders}) "
            f"ON CONFLICT(node_num) DO UPDATE SET {updates}",
            [node_num, *fields.values()],
        )

    def nodes(self) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM nodes ORDER BY is_favorite DESC, COALESCE(last_heard, 0) DESC"
        )

    def node_counts(self, since: int) -> dict[str, int]:
        """How many nodes are stored here, and how many were heard since `since`.

        The counterpart to the radio's own num_total_nodes/num_online_nodes,
        which only ever describe its fixed-size node database: once that is full
        it evicts the node heard longest ago, so its total stops at the limit
        while this one keeps growing. Recorded next to each local stats sample
        so the two can be read against each other over time.
        """
        row = self._query(
            "SELECT COUNT(*) AS total, COALESCE(SUM(last_heard >= ?), 0) AS heard FROM nodes",
            (since,),
        )[0]
        return {"num_nodes_here": row["total"], "num_heard_here": row["heard"]}

    def favorite_nums(self) -> set[int]:
        return {r["node_num"] for r in self._query("SELECT node_num FROM nodes WHERE is_favorite")}

    def set_node_flags(
        self, node_num: int, *, is_favorite: bool | None = None, is_ignored: bool | None = None
    ) -> None:
        """Change a node's flags here and in the captured handshake, so the next
        phone to connect sees the same star the node now has."""
        fields: dict[str, Any] = {}
        if is_favorite is not None:
            fields["is_favorite"] = int(is_favorite)
        if is_ignored is not None:
            fields["is_ignored"] = int(is_ignored)
        if not fields:
            return
        self.upsert_node(node_num, fields)

        key = f"node_info:{node_num}"
        with self._lock:
            row = self._conn.execute(
                "SELECT raw FROM config_frames WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                patched = proto.with_node_flags(
                    row["raw"], is_favorite=is_favorite, is_ignored=is_ignored
                )
                self._conn.execute(
                    "UPDATE config_frames SET raw = ?, updated_at = ? WHERE key = ?",
                    (patched, _now(), key),
                )
                self._commit()

    def store_position(self, node_num: int, fields: dict[str, Any]) -> bool:
        """Add a point to a node's track, unless it has not moved since the
        last one. Returns whether a row was added."""
        lat, lon = fields.get("latitude"), fields.get("longitude")
        if lat is None or lon is None:
            return False
        with self._lock:
            last = self._conn.execute(
                "SELECT latitude, longitude FROM positions WHERE node_num = ? "
                "ORDER BY time DESC LIMIT 1",
                (node_num,),
            ).fetchone()
            if last is not None and (last["latitude"], last["longitude"]) == (lat, lon):
                return False
            self._conn.execute(
                "INSERT OR REPLACE INTO positions"
                "(node_num, time, latitude, longitude, altitude, precision_bits) "
                "VALUES (?,?,?,?,?,?)",
                (
                    node_num,
                    fields.get("position_time") or _now(),
                    lat,
                    lon,
                    fields.get("altitude"),
                    fields.get("precision_bits"),
                ),
            )
            self._commit()
            return True

    def track(self, node_num: int, since: int) -> list[sqlite3.Row]:
        return self._query(
            "SELECT time, latitude, longitude, altitude, precision_bits FROM positions "
            "WHERE node_num = ? AND time >= ? ORDER BY time",
            (node_num, since),
        )

    def public_key(self, node_num: int) -> bytes | None:
        rows = self._query("SELECT public_key FROM nodes WHERE node_num = ?", (node_num,))
        return bytes(rows[0]["public_key"]) if rows and rows[0]["public_key"] else None

    def seal_own_dms(self, my_num: int, origin: str) -> int:
        """Mark DMs sent from `origin` without the PKI flag as PKI, where the
        recipient's key is known.

        The node seals such a DM itself on the way out, so the flag is what
        actually happened. Without it, a replayed copy lands in a second
        conversation in the Android app, which files PKI DMs under channel 8
        and plain ones under their channel number. Returns the rows fixed.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.seq, p.raw FROM packets p JOIN nodes n ON n.node_num = p.to_num "
                "WHERE p.origin = ? AND p.from_num = ? AND p.pki = 0 AND p.to_num != ? "
                "AND p.channel = 0 AND n.public_key IS NOT NULL",
                (origin, my_num, proto.BROADCAST_NUM),
            ).fetchall()
            for row in rows:
                fr = proto.parse_from_radio(row["raw"])
                fr.packet.pki_encrypted = True
                self._conn.execute(
                    "UPDATE packets SET pki = 1, raw = ? WHERE seq = ?",
                    (fr.SerializeToString(), row["seq"]),
                )
            self._commit()
            return len(rows)

    def refresh_node_frame(self, node_num: int, packet: Any, now: int) -> None:
        """Bring this node's entry in the stored handshake up to date with a
        live packet (see protocol.refreshed_node_info), or add it if new.

        A new node goes right after the last node entry, where the firmware
        lists it, ahead of anything that follows (file info).
        """
        key = f"node_info:{node_num}"
        with self._lock:
            if self._conn.execute("SELECT 1 FROM config_frames LIMIT 1").fetchone() is None:
                return  # nothing captured yet; the first handshake brings everything
            row = self._conn.execute(
                "SELECT raw FROM config_frames WHERE key = ?", (key,)
            ).fetchone()
            raw = proto.refreshed_node_info(row["raw"] if row else None, packet, now=now)
            if raw is None:
                return
            if row is not None:
                self._conn.execute(
                    "UPDATE config_frames SET raw = ?, updated_at = ? WHERE key = ?",
                    (raw, now, key),
                )
            else:
                last = self._conn.execute(
                    "SELECT COALESCE(MAX(ord), (SELECT MAX(ord) FROM config_frames)) AS o "
                    "FROM config_frames WHERE kind = 'node_info'"
                ).fetchone()["o"]
                self._conn.execute("UPDATE config_frames SET ord = ord + 1 WHERE ord > ?", (last,))
                self._conn.execute(
                    "INSERT INTO config_frames(key, kind, ord, raw, updated_at) VALUES (?,?,?,?,?)",
                    (key, "node_info", last + 1, raw, now),
                )
            self._commit()

    def node(self, node_num: int) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM nodes WHERE node_num = ?", (node_num,))
        return rows[0] if rows else None

    # -------------------------------------------------------------- telemetry

    def store_telemetry(
        self, node_num: int, rx_time: int, kind: str, values: dict[str, Any]
    ) -> None:
        known = (
            "battery_level",
            "voltage",
            "channel_utilization",
            "air_util_tx",
            "uptime_seconds",
            "temperature",
            "relative_humidity",
            "barometric_pressure",
        )
        cols = {k: values.get(k) for k in known}
        extra = {k: v for k, v in values.items() if k not in known and v is not None}
        self._exec(
            """
            INSERT OR IGNORE INTO telemetry
                (node_num, rx_time, kind, battery_level, voltage, channel_utilization,
                 air_util_tx, uptime_seconds, temperature, relative_humidity,
                 barometric_pressure, extra)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                node_num,
                rx_time,
                kind,
                cols["battery_level"],
                cols["voltage"],
                cols["channel_utilization"],
                cols["air_util_tx"],
                cols["uptime_seconds"],
                cols["temperature"],
                cols["relative_humidity"],
                cols["barometric_pressure"],
                json.dumps(extra) if extra else None,
            ),
        )

    def telemetry_rows(
        self, node_num: int | None, since: int, limit: int = 20000
    ) -> list[dict[str, Any]]:
        """Telemetry with the `extra` JSON folded back into top-level keys."""
        out = []
        for row in self.telemetry(node_num, since, limit):
            d = dict(row)
            extra = d.pop("extra", None)
            if extra:
                d.update(json.loads(extra))
            out.append(d)
        return out

    def telemetry(self, node_num: int | None, since: int, limit: int = 5000) -> list[sqlite3.Row]:
        if node_num is None:
            return self._query(
                "SELECT * FROM telemetry WHERE rx_time >= ? ORDER BY rx_time ASC LIMIT ?",
                (since, limit),
            )
        return self._query(
            "SELECT * FROM telemetry WHERE node_num = ? AND rx_time >= ? "
            "ORDER BY rx_time ASC LIMIT ?",
            (node_num, since, limit),
        )

    def telemetry_nodes(self) -> list[sqlite3.Row]:
        return self._query(
            """
            SELECT t.node_num, COUNT(*) AS samples, MAX(t.rx_time) AS last_time,
                   n.long_name, n.short_name, COALESCE(n.is_favorite, 0) AS is_favorite
            FROM telemetry t LEFT JOIN nodes n ON n.node_num = t.node_num
            GROUP BY t.node_num ORDER BY last_time DESC
            """
        )

    # ---------------------------------------------------------------- clients

    def get_or_create_client(self, client_key: str, label: str | None = None) -> sqlite3.Row:
        now = _now()
        self._exec(
            """
            INSERT INTO clients(client_key, label, last_seq, first_seen, last_seen, connects)
            VALUES (?,?,0,?,?,1)
            ON CONFLICT(client_key) DO UPDATE SET last_seen=excluded.last_seen,
                                                  connects=clients.connects+1,
                                                  label=COALESCE(clients.label, excluded.label)
            """,
            (client_key, label, now, now),
        )
        return self._query("SELECT * FROM clients WHERE client_key = ?", (client_key,))[0]

    def set_client_cursor(self, client_key: str, seq: int, replayed: int = 0) -> None:
        self._exec(
            "UPDATE clients SET last_seq = MAX(last_seq, ?), last_seen = ?, "
            "replayed = replayed + ? WHERE client_key = ?",
            (seq, _now(), replayed, client_key),
        )

    def reset_client_cursor(self, client_key: str) -> None:
        self._exec("UPDATE clients SET last_seq = 0 WHERE client_key = ?", (client_key,))

    def clients(self) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM clients ORDER BY last_seen DESC")

    # ----------------------------------------------------------------- events

    def log_event(self, level: str, source: str, message: str) -> None:
        self._exec(
            "INSERT INTO events(ts, level, source, message) VALUES (?,?,?,?)",
            (_now(), level, source, message),
        )

    def events(self, limit: int = 100) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ----------------------------------------------------------- delivery log

    def log_delivery(self, packet_id: int, from_num: int, event: str, **fields: Any) -> None:
        self._exec(
            "INSERT INTO delivery_log(packet_id, from_num, ts, event, ack_from, error, "
            "relay_node, rx_snr, rx_rssi, hops) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                packet_id,
                from_num,
                fields.get("ts") or _now(),
                event,
                fields.get("ack_from"),
                fields.get("error"),
                fields.get("relay_node"),
                fields.get("rx_snr"),
                fields.get("rx_rssi"),
                fields.get("hops"),
            ),
        )

    def delivery_log(self, packet_id: int, from_num: int) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM delivery_log WHERE packet_id = ? AND from_num = ? ORDER BY id",
            (packet_id, from_num),
        )

    def packet(self, seq: int) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM packets WHERE seq = ?", (seq,))
        return rows[0] if rows else None

    # -------------------------------------------------------------- exchanges

    def log_exchange(
        self,
        *,
        kind: str,
        node_num: int,
        direction: str,
        status: str,
        channel: int = 0,
        packet_id: int = 0,
        origin: str | None = None,
        ts: int | None = None,
        result: dict[str, Any] | None = None,
    ) -> int:
        cur = self._exec(
            "INSERT INTO exchanges(ts, kind, node_num, direction, channel, packet_id, "
            "origin, status, result) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                ts or _now(),
                kind,
                node_num,
                direction,
                channel,
                packet_id,
                origin,
                status,
                json.dumps(result) if result else None,
            ),
        )
        return int(cur.lastrowid or 0)

    def open_exchange(self, packet_id: int) -> sqlite3.Row | None:
        """The request a response with this request_id belongs to, if it is
        still waiting for one."""
        if not packet_id:
            return None
        rows = self._query(
            "SELECT * FROM exchanges WHERE packet_id = ? AND direction = 'out' "
            "AND status IN ('sent', 'failed') ORDER BY id DESC LIMIT 1",
            (packet_id,),
        )
        return rows[0] if rows else None

    def resolve_exchange(
        self,
        exchange_id: int,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> sqlite3.Row | None:
        """Close a request off with what came back. Returns the finished row."""
        self._exec(
            "UPDATE exchanges SET status = ?, response_ts = ?, result = COALESCE(?, result), "
            "error = ? WHERE id = ?",
            (status, _now(), json.dumps(result) if result else None, error, exchange_id),
        )
        rows = self._query("SELECT * FROM exchanges WHERE id = ?", (exchange_id,))
        return rows[0] if rows else None

    def expire_exchanges(self, timeouts: dict[str, int], default: int = 60) -> int:
        """Give up on requests nothing answered. An answer can still arrive
        later - a traceroute crawls back hop by hop - and resolve_exchange
        accepts it, so this only stops the UI showing a spinner forever."""
        cases = " ".join("WHEN ? THEN ?" for _ in timeouts)
        params: list[Any] = [_now()]
        for kind, seconds in timeouts.items():
            params.extend([kind, int(seconds)])
        params.append(int(default))
        return self._exec(
            "UPDATE exchanges SET status = 'expired' WHERE status = 'sent' "
            f"AND ? - ts > CASE kind {cases} ELSE ? END",
            params,
        ).rowcount

    def exchanges(
        self, node_num: int | None = None, *, limit: int = 50, since: int | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM exchanges"
        params: list[Any] = []
        where = []
        if node_num is not None:
            where.append("node_num = ?")
            params.append(node_num)
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        return self._query(sql, params)

    def exchange(self, exchange_id: int) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM exchanges WHERE id = ?", (exchange_id,))
        return rows[0] if rows else None

    def last_exchange_ts(self, kind: str, node_num: int) -> int | None:
        rows = self._query(
            "SELECT MAX(ts) AS ts FROM exchanges WHERE kind = ? AND node_num = ? "
            "AND direction = 'out'",
            (kind, node_num),
        )
        return rows[0]["ts"] if rows and rows[0]["ts"] else None

    # ------------------------------------------------------------------ prefs

    def prefs(self) -> dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in self._query("SELECT * FROM prefs")}

    def set_pref(self, key: str, value: Any) -> None:
        """Store a preference; None removes it, so the default applies again."""
        if value is None:
            self._exec("DELETE FROM prefs WHERE key = ?", (key,))
            return
        self._exec(
            "INSERT INTO prefs(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), _now()),
        )
