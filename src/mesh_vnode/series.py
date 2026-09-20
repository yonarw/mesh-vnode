"""Shaping stored telemetry into chartable series.

Two jobs. The local node's packet counters are cumulative since boot, so they are
turned into per-hour rates here, where consecutive samples are still adjacent.
Then everything is averaged into a bounded number of time buckets: one GPS sample
a minute is ~43k points over 30 days, far more than a phone should draw.
"""

from __future__ import annotations

from typing import Any

# Cumulative counters in local_stats -> the rate field derived from each.
COUNTERS = {
    "num_packets_rx": "rx_per_hour",
    "num_packets_tx": "tx_per_hour",
    "num_tx_relay": "relay_per_hour",
    "num_packets_rx_bad": "bad_per_hour",
    "num_rx_dupe": "dupe_per_hour",
}

# Fields that are labels, not measurements, and must not be averaged.
NOT_AVERAGED = {"id", "node_num", "rx_time", "kind", "fix_type", "fix_quality", "precision_bits"}


def add_rates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive per-hour rates from consecutive local_stats samples.

    A counter that goes down means the node rebooted; that interval is skipped
    rather than reported as a huge negative rate.
    """
    prev: dict[str, Any] | None = None
    for row in rows:
        if row.get("kind") != "local":
            continue
        if prev is not None:
            dt = row["rx_time"] - prev["rx_time"]
            if dt > 0:
                for counter, rate in COUNTERS.items():
                    a, b = prev.get(counter), row.get(counter)
                    if a is not None and b is not None and b >= a:
                        row[rate] = round((b - a) * 3600 / dt, 1)
        prev = row
    return rows


def bucket(rows: list[dict[str, Any]], span_s: int, points: int) -> list[dict[str, Any]]:
    """Average rows into at most `points` time buckets, per kind.

    Kinds are kept apart so a bucket holding one GPS and one device sample does
    not blend them into a row that looks like both came from the same instant.
    Labels (fix type, precision bits) take the latest value in the bucket.
    """
    if not rows or points <= 0:
        return rows
    width = max(1, span_s // points)
    if len(rows) <= points:
        return rows

    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row.get("kind", ""), row["rx_time"] // width)
        groups.setdefault(key, []).append(row)

    out = []
    for (kind, slot), members in groups.items():
        merged: dict[str, Any] = {"kind": kind, "rx_time": slot * width + width // 2}
        merged["node_num"] = members[0].get("node_num")
        keys = {k for m in members for k in m if k not in ("kind", "rx_time", "node_num", "id")}
        for key in keys:
            values = [m[key] for m in members if m.get(key) is not None]
            if not values:
                continue
            if key in NOT_AVERAGED or not all(isinstance(v, (int, float)) for v in values):
                merged[key] = values[-1]
            else:
                merged[key] = round(sum(values) / len(values), 3)
        merged["samples"] = len(members)
        out.append(merged)
    out.sort(key=lambda r: r["rx_time"])
    return out
