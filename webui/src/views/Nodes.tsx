import { useEffect, useMemo, useState } from "react";
import { api, useResource, type Node } from "../api";
import { Empty, Pill, batteryTone, nodeLabel, relTime } from "../ui";
import NodeSheet from "./NodeSheet";

type Heard = "1h" | "24h" | "7d" | "any";
type Sort = "heard" | "name" | "snr" | "hops";

interface Filters {
  q: string;
  favoritesOnly: boolean;
  heard: Heard;
  directOnly: boolean;
  hideMqtt: boolean;
  sort: Sort;
}

const DEFAULTS: Filters = {
  q: "",
  favoritesOnly: false,
  heard: "24h",
  directOnly: false,
  hideMqtt: false,
  sort: "heard",
};

const HEARD_SECONDS: Record<Heard, number | null> = { "1h": 3600, "24h": 86400, "7d": 604800, any: null };
const PAGE = 60;
const STORAGE_KEY = "vnode.nodeFilters";

// Filters are a per-viewer convenience: remembered in this browser only, and
// the page works the same if storage is unavailable.
function loadFilters(): Filters {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? { ...DEFAULTS, ...JSON.parse(raw), q: "" } : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

function saveFilters(f: Filters) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...f, q: "" }));
  } catch {
    /* private window, blocked storage: fine */
  }
}

export default function Nodes({
  tick,
  onMessage,
  onShowOnMap,
}: {
  tick: number;
  onMessage: (n: Node) => void;
  onShowOnMap: (n: Node) => void;
}) {
  const { data, loading, refresh } = useResource<Node[]>(() => api.nodes(), [tick]);
  // The node opened as a card: everything about one node, and where it is
  // asked for a traceroute or its position.
  const [opened, setOpened] = useState<Node | null>(null);
  const [filters, setFilters] = useState<Filters>(loadFilters);
  const [shown, setShown] = useState(PAGE);
  // Stars flipped here before the node confirms, keyed by node number.
  const [pending, setPending] = useState<Record<number, boolean>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => saveFilters(filters), [filters]);
  useEffect(() => setShown(PAGE), [filters]);

  const update = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }));

  const nodes = useMemo(
    () => (data ?? []).map((n) => (n.node_num in pending ? { ...n, is_favorite: pending[n.node_num] ? 1 : 0 } : n)),
    [data, pending],
  );

  const { pinned, rest, total } = useMemo(() => applyFilters(nodes, filters), [nodes, filters]);

  const toggleFavorite = async (n: Node) => {
    const next = !n.is_favorite;
    setPending((p) => ({ ...p, [n.node_num]: next }));
    setError(null);
    try {
      await api.setFavorite(n.node_num, next);
      await refresh();
    } catch (e) {
      setError(`Could not ${next ? "star" : "unstar"} ${nodeLabel(n)}: ${e instanceof Error ? e.message : e}`);
    } finally {
      setPending((p) => {
        const { [n.node_num]: _, ...others } = p;
        return others;
      });
    }
  };

  if (loading && !data) return <Empty>Loading…</Empty>;
  if (!data?.length) return <Empty>No nodes yet. They arrive when the node sends its database.</Empty>;

  const visibleRest = rest.slice(0, Math.max(0, shown - pinned.length));
  const matched = pinned.length + rest.length;

  return (
    <div className="space-y-3">
      <Toolbar filters={filters} update={update} matched={matched} total={total} />

      {error && (
        <div className="rounded-lg border border-alert-400/40 bg-alert-400/10 px-3 py-2 text-xs text-alert-400">
          {error}
        </div>
      )}

      {matched === 0 ? (
        <Empty>No node matches these filters.</Empty>
      ) : (
        <>
          {pinned.length > 0 && (
            <Section title="This node & favourites">
              {pinned.map((n) => (
                <NodeRow key={n.node_num} node={n} onStar={toggleFavorite} onMessage={onMessage} onShowOnMap={onShowOnMap} onOpen={setOpened} busy={n.node_num in pending} />
              ))}
            </Section>
          )}
          {visibleRest.length > 0 && (
            <Section title={pinned.length ? "Everyone else" : undefined}>
              {visibleRest.map((n) => (
                <NodeRow key={n.node_num} node={n} onStar={toggleFavorite} onMessage={onMessage} onShowOnMap={onShowOnMap} onOpen={setOpened} busy={n.node_num in pending} />
              ))}
            </Section>
          )}
          {pinned.length + visibleRest.length < matched && (
            <button
              onClick={() => setShown((s) => s + PAGE)}
              className="w-full rounded-lg border border-ink-700 bg-ink-800/70 py-2.5 text-xs font-medium text-mist-400 hover:text-mist-200"
            >
              Show more ({matched - pinned.length - visibleRest.length} left)
            </button>
          )}
        </>
      )}

      {opened && (
        <NodeSheet
          nodeNum={opened.node_num}
          fallback={{
            short: opened.short_name,
            long: opened.long_name,
            id: opened.node_id,
          }}
          tick={tick}
          onClose={() => setOpened(null)}
        />
      )}
    </div>
  );
}

function applyFilters(nodes: Node[], f: Filters) {
  const now = Date.now() / 1000;
  const window = HEARD_SECONDS[f.heard];
  const q = f.q.trim().toLowerCase();

  const matches = (n: Node) => {
    if (q) {
      const hay = `${n.long_name ?? ""} ${n.short_name ?? ""} ${n.node_id}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    if (f.favoritesOnly && !n.is_favorite && !n.is_local) return false;
    if (f.directOnly && n.hops_away !== 0) return false;
    if (f.hideMqtt && n.via_mqtt) return false;
    return true;
  };

  const sorters: Record<Sort, (a: Node, b: Node) => number> = {
    heard: (a, b) => (b.last_heard ?? 0) - (a.last_heard ?? 0),
    name: (a, b) => nodeLabel(a).localeCompare(nodeLabel(b)),
    snr: (a, b) => (b.snr ?? -999) - (a.snr ?? -999),
    hops: (a, b) => (a.hops_away ?? 99) - (b.hops_away ?? 99),
  };

  const matched = nodes.filter(matches);
  // Favourites and this node are pinned regardless of how recently they were
  // heard: a starred node that went quiet is exactly the one you look for.
  const pinned = matched
    .filter((n) => n.is_local || n.is_favorite)
    .sort((a, b) => Number(b.is_local) - Number(a.is_local) || sorters[f.sort](a, b));
  const rest = matched
    .filter((n) => !n.is_local && !n.is_favorite)
    .filter((n) => window === null || (n.last_heard !== null && now - n.last_heard <= window))
    .sort(sorters[f.sort]);

  return { pinned, rest, total: nodes.length };
}

function Toolbar({
  filters,
  update,
  matched,
  total,
}: {
  filters: Filters;
  update: (p: Partial<Filters>) => void;
  matched: number;
  total: number;
}) {
  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <input
          value={filters.q}
          onChange={(e) => update({ q: e.target.value })}
          placeholder="Search name, short name or !id"
          className="min-w-0 flex-1 rounded-lg border border-ink-700 bg-ink-800 px-3 py-2 text-sm text-mist-200 outline-none placeholder:text-mist-400 focus:border-accent-400/60"
        />
        <select
          value={filters.sort}
          onChange={(e) => update({ sort: e.target.value as Sort })}
          aria-label="Sort by"
          className="rounded-lg border border-ink-700 bg-ink-800 px-2 py-2 text-xs text-mist-200 outline-none"
        >
          <option value="heard">Last heard</option>
          <option value="name">Name</option>
          <option value="snr">Signal (SNR)</option>
          <option value="hops">Hops</option>
        </select>
      </div>

      <div className="no-scrollbar -mx-1 flex items-center gap-1.5 overflow-x-auto px-1">
        <Chip on={filters.favoritesOnly} onClick={() => update({ favoritesOnly: !filters.favoritesOnly })}>
          ★ Favourites
        </Chip>
        <Chip on={filters.directOnly} onClick={() => update({ directOnly: !filters.directOnly })}>
          Direct only
        </Chip>
        <Chip on={filters.hideMqtt} onClick={() => update({ hideMqtt: !filters.hideMqtt })}>
          Hide MQTT
        </Chip>
        <span className="mx-1 h-4 w-px shrink-0 bg-ink-700" />
        {(["1h", "24h", "7d", "any"] as Heard[]).map((h) => (
          <Chip key={h} on={filters.heard === h} onClick={() => update({ heard: h })}>
            {h === "any" ? "Any time" : `Heard ${h}`}
          </Chip>
        ))}
      </div>

      <p className="text-[11px] text-mist-400">
        {matched} of {total} nodes kept here · more than the radio itself lists, which holds a fixed
        maximum and forgets the rest
        {filters.heard !== "any" && " · favourites are shown whenever they were last heard"}
      </p>
    </div>
  );
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      aria-pressed={on}
      className={`shrink-0 rounded-full border px-3 py-1.5 text-xs font-medium ${
        on ? "border-accent-400/60 bg-accent-400/15 text-accent-400" : "border-ink-700 bg-ink-800/70 text-mist-400"
      }`}
    >
      {children}
    </button>
  );
}

function Section({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <section>
      {title && <h2 className="mb-1.5 px-1 text-[11px] font-semibold uppercase tracking-wider text-mist-400">{title}</h2>}
      <ul className="divide-y divide-ink-700 overflow-hidden rounded-xl border border-ink-700 bg-ink-900/80">{children}</ul>
    </section>
  );
}

function NodeRow({
  node: n,
  onStar,
  onMessage,
  onShowOnMap,
  onOpen,
  busy,
}: {
  node: Node;
  onStar: (n: Node) => void;
  onMessage: (n: Node) => void;
  onShowOnMap: (n: Node) => void;
  onOpen: (n: Node) => void;
  busy: boolean;
}) {
  const hasPosition = n.latitude !== null && n.longitude !== null;
  return (
    <li className={`flex items-start gap-2 px-3 py-2.5 ${n.is_ignored ? "opacity-50" : ""}`}>
      {n.is_local ? (
        <span className="mt-0.5 w-8 shrink-0 text-center text-[10px] font-semibold text-accent-400" title="The node this service is connected to">
          ME
        </span>
      ) : (
        <button
          onClick={() => onStar(n)}
          disabled={busy}
          aria-label={n.is_favorite ? `Unstar ${nodeLabel(n)}` : `Star ${nodeLabel(n)}`}
          title={n.is_favorite ? "Favourite on the node - tap to remove" : "Mark as favourite on the node"}
          className={`-m-1 mt-0 w-8 shrink-0 p-1 text-lg leading-none disabled:opacity-40 ${
            n.is_favorite ? "text-warn-400" : "text-mist-400/50 hover:text-mist-400"
          }`}
        >
          {n.is_favorite ? "★" : "☆"}
        </button>
      )}

      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <button
            onClick={() => onOpen(n)}
            title="Show this node"
            className="min-w-0 truncate text-left text-sm font-semibold text-mist-200 hover:text-accent-400"
          >
            {nodeLabel(n)}
          </button>
          <span className="shrink-0 text-[11px] text-mist-400">{relTime(n.last_heard)}</span>
        </div>
        <div className="mt-0.5 truncate font-mono text-[11px] text-mist-400">
          {n.node_id}
          {n.short_name && ` · ${n.short_name}`}
          {n.role && ` · ${n.role}`}
          {n.hw_model && ` · ${n.hw_model}`}
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {n.battery_level !== null && (
            <Pill tone={batteryTone(n.battery_level)}>
              {n.battery_level > 100 ? "powered" : `${n.battery_level}%`}
              {n.voltage !== null && ` · ${n.voltage.toFixed(2)} V`}
            </Pill>
          )}
          {n.snr !== null && <Pill>{n.snr.toFixed(1)} dB</Pill>}
          {n.hops_away !== null && <Pill>{n.hops_away === 0 ? "direct" : `${n.hops_away} hop${n.hops_away > 1 ? "s" : ""}`}</Pill>}
          {n.via_mqtt ? <Pill tone="warn">MQTT</Pill> : null}
          {n.is_ignored ? <Pill>ignored</Pill> : null}
          <span className="ml-auto flex gap-1.5">
            {hasPosition && (
              <button
                onClick={() => onShowOnMap(n)}
                className="rounded-full border border-ink-600 bg-ink-800 px-3 py-1 text-[11px] font-semibold text-mist-200"
              >
                Map
              </button>
            )}
            {!n.is_local && (
              <button
                onClick={() => onMessage(n)}
                className="rounded-full border border-accent-400/40 bg-accent-400/10 px-3 py-1 text-[11px] font-semibold text-accent-400"
              >
                Message
              </button>
            )}
          </span>
        </div>
      </div>
    </li>
  );
}
