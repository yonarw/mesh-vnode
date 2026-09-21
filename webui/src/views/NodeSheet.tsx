/** One node, everywhere: opened from the node list, the map and a name in a
 *  conversation. It is also where a node is asked for something - a
 *  traceroute, its position, its telemetry or its node info - because those
 *  belong to the node rather than to any one view. */

import { useEffect, useState } from "react";
import {
  api,
  useResource,
  type Channel,
  type Exchange,
  type Node,
  type NodeName,
} from "../api";
import { Empty, Pill, batteryTone, clockTime, nodeLabel, relTime } from "../ui";

const KINDS: { kind: Exchange["kind"]; label: string; hint: string }[] = [
  { kind: "traceroute", label: "Traceroute", hint: "Which nodes repeat a message on its way there and back" },
  { kind: "position", label: "Position", hint: "Send this node our position and ask for its own" },
  { kind: "telemetry", label: "Telemetry", hint: "Ask for battery, voltage and channel use" },
  { kind: "nodeinfo", label: "Node info", hint: "Ask for the name, hardware and role" },
];

// The firmware rate-limits these and every one of them costs everyone in range
// airtime, so the button stays down for a while after it was pressed.
const COOLDOWN_MS = 30_000;

const displayName = (n: NodeName) => n.short || n.long || n.id;
const fullName = (n: NodeName) => (n.long && n.short ? `${n.long} (${n.short})` : n.long || n.short || n.id);

export default function NodeSheet({
  nodeNum,
  fallback,
  tick,
  onClose,
}: {
  nodeNum: number;
  /** The name a message carried, for a node the database does not have yet. */
  fallback: NodeName;
  tick: number;
  onClose: () => void;
}) {
  const nodes = useResource<Node[]>(() => api.nodes(), [tick]);
  const channels = useResource<Channel[]>(() => api.channels(), []);
  const exchanges = useResource<Exchange[]>(() => api.exchanges(nodeNum, 20), [nodeNum, tick]);
  const [channel, setChannel] = useState(0);
  const [busy, setBusy] = useState<Exchange["kind"] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [asked, setAsked] = useState<Record<string, number>>({});
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Only while something is cooling down: the buttons come back by themselves.
  useEffect(() => {
    if (!Object.keys(asked).length) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [asked]);

  const n = nodes.data?.find((x) => x.node_num === nodeNum) ?? null;
  const mine = n?.is_local ?? false;
  const hasPosition = n !== null && n.latitude !== null && n.longitude !== null;

  const ask = async (kind: Exchange["kind"]) => {
    if (busy) return;
    setBusy(kind);
    setError(null);
    try {
      await api.exchange({ kind, node: nodeNum, channel });
      setAsked((a) => ({ ...a, [kind]: Date.now() }));
      void exchanges.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const cooling = (kind: Exchange["kind"]) => {
    const since = asked[kind];
    if (since === undefined) return 0;
    return Math.max(0, Math.ceil((COOLDOWN_MS - (now - since)) / 1000));
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 sm:items-center" onClick={onClose}>
      <div
        role="dialog"
        aria-label="Node"
        onClick={(e) => e.stopPropagation()}
        className="safe-bottom max-h-[85vh] w-full max-w-md overflow-y-auto rounded-t-2xl border border-ink-700 bg-ink-900 p-4 sm:rounded-2xl"
      >
        <div className="mb-3 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-mist-200">
              {n ? nodeLabel(n) : fullName(fallback)}
            </div>
            <div className="truncate font-mono text-[11px] text-mist-400">
              {n?.node_id ?? fallback.id}
              {n?.short_name && ` · ${n.short_name}`}
              {n?.role && ` · ${n.role}`}
              {n?.hw_model && ` · ${n.hw_model}`}
            </div>
          </div>
          <button onClick={onClose} className="rounded-full px-2 py-1 text-mist-400 hover:text-mist-200" aria-label="Close">
            ✕
          </button>
        </div>

        {!n ? (
          <Empty>{nodes.loading ? "Loading…" : "Not in the node database yet."}</Empty>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-1.5">
              {mine && <Pill tone="accent">this node</Pill>}
              {n.is_favorite ? <Pill tone="warn">favourite</Pill> : null}
              {n.battery_level !== null && (
                <Pill tone={batteryTone(n.battery_level)}>
                  {n.battery_level > 100 ? "powered" : `${n.battery_level}%`}
                  {n.voltage !== null && ` · ${n.voltage.toFixed(2)} V`}
                </Pill>
              )}
              {n.snr !== null && <Pill>{n.snr.toFixed(1)} dB</Pill>}
              {n.hops_away !== null && (
                <Pill>{n.hops_away === 0 ? "direct" : `${n.hops_away} hop${n.hops_away > 1 ? "s" : ""}`}</Pill>
              )}
              {n.via_mqtt ? <Pill tone="warn">MQTT</Pill> : null}
              {n.is_ignored ? <Pill>ignored</Pill> : null}
            </div>
            <div className="mt-2 space-y-0.5 text-xs text-mist-400">
              <div>Heard {relTime(n.last_heard)}</div>
              {hasPosition && (
                <div>
                  {n.latitude?.toFixed(5)}, {n.longitude?.toFixed(5)}
                  {n.altitude !== null && ` · ${n.altitude} m`}
                  {n.position_time && ` · from ${relTime(n.position_time)}`}
                </div>
              )}
            </div>
          </>
        )}

        {/* Both go through the hash, so this works from every view that shows
            a node without any of them having to know about the others. */}
        <div className="mt-3 flex flex-wrap gap-2">
          {!mine && (
            <button
              onClick={() => {
                onClose();
                location.hash = `messages/dm:${nodeNum}`;
              }}
              className="rounded-full border border-accent-400/40 bg-accent-400/10 px-3 py-1 text-[11px] font-semibold text-accent-400"
            >
              Message
            </button>
          )}
          {hasPosition && (
            <button
              onClick={() => {
                onClose();
                location.hash = `map/${nodeNum}`;
              }}
              className="rounded-full border border-ink-600 bg-ink-800 px-3 py-1 text-[11px] font-semibold text-mist-200"
            >
              Map
            </button>
          )}
        </div>

        {!mine && (
          <section className="mt-4">
            <div className="mb-2 flex items-center justify-between gap-2">
              <h3 className="text-xs font-semibold text-mist-200">Ask this node</h3>
              <label className="flex items-center gap-1.5 text-[11px] text-mist-400">
                on
                <select
                  value={channel}
                  onChange={(e) => setChannel(Number(e.target.value))}
                  className="rounded-lg border border-ink-700 bg-ink-800 px-2 py-1 text-[11px] text-mist-200 outline-none focus:border-accent-400/60"
                >
                  {(channels.data ?? [{ index: 0, name: "primary" } as Channel]).map((c) => (
                    <option key={c.index} value={c.index}>
                      {c.index}: {c.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {KINDS.map(({ kind, label, hint }) => {
                const left = cooling(kind);
                return (
                  <button
                    key={kind}
                    onClick={() => void ask(kind)}
                    disabled={busy !== null || left > 0}
                    title={hint}
                    className="rounded-full border border-ink-600 bg-ink-800 px-3 py-1.5 text-[11px] font-semibold text-mist-200 hover:border-accent-400/50 disabled:opacity-40"
                  >
                    {busy === kind ? "…" : left > 0 ? `${label} (${left}s)` : label}
                  </button>
                );
              })}
            </div>
            {error && (
              <div className="mt-2">
                <Pill tone="bad">{error}</Pill>
              </div>
            )}
            <p className="mt-2 text-[11px] text-mist-400">
              The answer comes back over the mesh and can take minutes. A node without the channel
              picked here cannot read the question.
            </p>
          </section>
        )}

        {(exchanges.data?.length ?? 0) > 0 && (
          <section className="mt-4">
            <h3 className="mb-2 text-xs font-semibold text-mist-200">Requests</h3>
            <ul className="space-y-2">
              {exchanges.data?.map((e) => (
                <li key={e.id} className="text-xs">
                  <div className="text-mist-200">{exchangeLine(e)}</div>
                  <div className="text-[11px] text-mist-400">
                    {clockTime(e.ts)} · channel {e.channel}
                    {e.origin && e.origin !== "webui" && e.direction === "out" && " · from an app"}
                  </div>
                  {e.kind === "traceroute" && e.status === "answered" && <RouteLines exchange={e} />}
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}

const ASKED: Record<Exchange["kind"], string> = {
  traceroute: "traced the route",
  position: "asked for a position",
  telemetry: "asked for telemetry",
  nodeinfo: "asked for node info",
};

// What the same request reads like when it came the other way. The node
// answered it by itself, so these lines never have an outcome.
const WAS_ASKED: Record<Exchange["kind"], string> = {
  traceroute: "traced a route to this node",
  position: "asked this node for its position",
  telemetry: "asked this node for telemetry",
  nodeinfo: "asked this node for its node info",
};

/** One line for an exchange, as the card and the conversation both show it. */
export function exchangeLine(e: Exchange): string {
  if (e.direction === "in") return `${displayName(e.node)} ${WAS_ASKED[e.kind]}`;
  const who = e.origin && e.origin !== "webui" ? "Your app" : "You";
  return `${who} ${ASKED[e.kind]}${outcome(e)}`;
}

function outcome(e: Exchange): string {
  switch (e.status) {
    case "sent":
      return " · waiting for an answer";
    case "failed":
      return ` · failed${e.error ? ` (${e.error})` : ""}`;
    case "expired":
      return " · no answer";
    case "heard":
      return "";
    case "answered":
      break;
  }
  const r = e.result;
  if (!r) return " · answered";
  if (e.kind === "traceroute") {
    const hops = r.route?.length ?? 0;
    return hops === 0 ? " · direct, no relay" : ` · ${hops} relay${hops > 1 ? "s" : ""}`;
  }
  if (e.kind === "telemetry" && r.battery_level !== undefined) {
    return ` · ${r.battery_level > 100 ? "powered" : `${r.battery_level}%`}${
      r.voltage !== undefined ? `, ${r.voltage.toFixed(2)} V` : ""
    }`;
  }
  if (e.kind === "position" && r.latitude !== undefined) {
    return ` · ${r.latitude.toFixed(4)}, ${r.longitude?.toFixed(4)}`;
  }
  return " · answered";
}

/** The path out and the path home, each hop with the signal it was heard at.
 *  They are listed separately because they need not be the same route: the
 *  answer can come back a different way from the way the question went. */
function RouteLines({ exchange: e }: { exchange: Exchange }) {
  const r = e.result;
  if (!r) return null;
  const peer = displayName(e.node);
  const leg = (
    start: string,
    names: NodeName[] | undefined,
    snr: (number | null)[] | undefined,
    end: string,
  ) => {
    const hops = names ?? [];
    if (!hops.length && !snr?.length) return null;
    return [
      start,
      ...hops.map((name, i) => `${displayName(name)}${fmtSnr(snr?.[i])}`),
      `${end}${fmtSnr(snr?.[hops.length])}`,
    ].join(" → ");
  };
  const there = leg("this node", r.route_names, r.snr_towards, peer);
  const back = leg(peer, r.route_back_names, r.snr_back, "this node");
  return (
    <div className="mt-0.5 space-y-0.5 text-[11px] text-mist-400">
      {there && <div>→ {there}</div>}
      {back && <div>← {back}</div>}
    </div>
  );
}

function fmtSnr(snr: number | null | undefined): string {
  return snr === null || snr === undefined ? "" : ` (${snr.toFixed(1)} dB)`;
}
