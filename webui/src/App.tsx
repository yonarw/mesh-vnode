import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import { api, useLiveEvents, useResource, type Channel, type Node, type Status as StatusT } from "./api";
import { TicksContext, useTicks } from "./live";
import { maybeNotify, onNotificationOpen } from "./notify";
import { PrefsProvider, parseConvKey, usePrefs, type Target } from "./prefs";
import { Empty, Pill } from "./ui";
import Messages from "./views/Messages";
import Nodes from "./views/Nodes";
import Settings from "./views/Settings";
import Status from "./views/Status";

// Recharts and MapLibre are most of the bundle; the phone only pays for them
// on their own tabs.
const Telemetry = lazy(() => import("./views/Telemetry"));
const MapView = lazy(() => import("./views/Map"));

const TABS = [
  { id: "messages", label: "Messages" },
  { id: "nodes", label: "Nodes" },
  { id: "map", label: "Map" },
  { id: "telemetry", label: "Telemetry" },
  { id: "status", label: "Status" },
] as const;

type TabId = (typeof TABS)[number]["id"] | "settings";

const WIDE_TABS = new Set<TabId>(["map", "telemetry"]);

/** The hash is `#tab`, `#messages/<conversation>` (ch:1, dm:123456) or
 *  `#map/<node number>`. */
function parseHash(): { tab: TabId; conv: Target | null; mapNode: number | null } {
  const [id, conv] = location.hash.replace("#", "").split("/");
  const tab: TabId = id === "settings" || TABS.some((t) => t.id === id) ? (id as TabId) : "messages";
  const mapNode = tab === "map" && /^\d+$/.test(conv ?? "") ? Number(conv) : null;
  return { tab, conv: tab === "messages" ? parseConvKey(conv) : null, mapNode };
}

export default function App() {
  return (
    <PrefsProvider>
      <Shell />
    </PrefsProvider>
  );
}

function Shell() {
  // The tab lives in the hash so the phone's back button works and a reload
  // lands where you were.
  const [tab, setTab] = useState<TabId>(() => parseHash().tab);
  const [requestedChat, setRequestedChat] = useState<Target | null>(() => parseHash().conv);
  const [mapNode, setMapNode] = useState<number | null>(() => parseHash().mapNode);

  useEffect(() => {
    const sync = () => {
      const { tab, conv, mapNode } = parseHash();
      setTab(tab);
      if (conv) setRequestedChat(conv);
      setMapNode(mapNode);
    };
    window.addEventListener("hashchange", sync);
    const stop = onNotificationOpen((hash) => {
      location.hash = hash;
    });
    return () => {
      window.removeEventListener("hashchange", sync);
      stop();
    };
  }, []);
  const go = (id: TabId) => {
    location.hash = id;
    setTab(id);
  };

  const { ticks, bump } = useTicks();
  const status = useResource<StatusT>(() => api.status(), [ticks.status]);
  const { prefs } = usePrefs();
  const channels = useResource<Channel[]>(() => api.channels(), [status.data?.upstream.config_captured_at]);
  // The conversation on screen, so a message there does not also ping.
  const [viewing, setViewing] = useState<string | null>(null);

  const openDm = useCallback((n: Node) => {
    setRequestedChat({ kind: "dm", node: n.node_num, name: n.short_name || n.long_name || n.node_id });
    location.hash = "messages";
    setTab("messages");
  }, []);

  // The live handler reads these through a ref: it is registered once.
  const live = useRef({ myNum: null as number | null, muted: [] as string[], channels: [] as Channel[], viewing });
  live.current = {
    myNum: status.data?.upstream.my_node_num ?? null,
    muted: prefs?.muted ?? [],
    channels: channels.data ?? [],
    viewing: tab === "messages" ? viewing : null,
  };
  const online = useLiveEvents(
    useCallback(
      (ev) => {
        bump(ev.event);
        if (ev.event === "packet" && typeof ev.payload.seq === "number") {
          const c = live.current;
          void maybeNotify(ev.payload.seq, {
            myNum: c.myNum,
            muted: c.muted,
            viewing: c.viewing,
            channelName: (i) => c.channels.find((ch) => ch.index === i)?.name ?? `channel ${i}`,
          });
        }
      },
      [bump],
    ),
  );

  // Fallback poll, in case the socket is up but silent.
  useEffect(() => {
    const id = window.setInterval(() => bump(), 30000);
    return () => window.clearInterval(id);
  }, [bump]);

  const up = status.data?.upstream;
  const me = up?.my_short_name || up?.my_long_name || up?.my_node_id;

  useEffect(() => {
    document.title = me ? `${me} · mesh-vnode` : "mesh-vnode";
  }, [me]);

  return (
    <TicksContext.Provider value={ticks}>
      <div
        className={`mx-auto flex h-full flex-col overflow-x-hidden px-4 ${
          // Messages and lists read better in a column; the map and the charts
          // are the two views that actually want the pixels. Capped rather than
          // unbounded so an ultrawide does not stretch the header across a metre.
          WIDE_TABS.has(tab) ? "max-w-[1600px]" : "max-w-3xl"
        }`}
      >
        <header className="safe-top flex items-center justify-between gap-3 py-3">
          <div className="min-w-0">
            <h1 className="truncate text-base font-semibold text-mist-200" title={up?.my_node_id ?? undefined}>
              {me ?? "mesh-vnode"}
              {up?.my_long_name && up.my_short_name && (
                <span className="ml-2 text-xs font-normal text-mist-400">{up.my_long_name}</span>
              )}
            </h1>
            <p className="truncate text-[11px] text-mist-400">
              mesh-vnode · {status.data?.counts.texts ?? 0} messages stored
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {up?.connected ? <Pill tone="good">node</Pill> : <Pill tone="bad">node</Pill>}
            {online ? <Pill tone="good">live</Pill> : <Pill tone="warn">live</Pill>}
            <button
              onClick={() => go("settings")}
              aria-label="Settings"
              title="Settings"
              className={`ml-1 rounded-full p-1.5 text-base leading-none ${
                tab === "settings" ? "bg-accent-400/15 text-accent-400" : "text-mist-400 hover:text-mist-200"
              }`}
            >
              ⚙
            </button>
          </div>
        </header>

        <nav className="mb-3 flex gap-1 rounded-xl border border-ink-700 bg-ink-900/80 p-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => go(t.id)}
              className={`min-w-0 flex-1 truncate rounded-lg px-1.5 py-2 text-xs font-medium transition ${
                tab === t.id ? "bg-accent-400/15 text-accent-400" : "text-mist-400 hover:text-mist-200"
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>

        <main className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto pb-4">
          {status.error && (
            <div className="mb-3 rounded-lg border border-alert-400/40 bg-alert-400/10 px-3 py-2 text-xs text-alert-400">
              API unreachable: {status.error}
            </div>
          )}
          {tab === "messages" && (
            <Messages myNodeNum={up?.my_node_num ?? null} requested={requestedChat} onViewing={setViewing} />
          )}
          {tab === "nodes" && <Nodes onMessage={openDm} onShowOnMap={(n) => (location.hash = `map/${n.node_num}`)} />}
          {tab === "map" && (
            <Suspense fallback={<Empty>Loading map…</Empty>}>
              <MapView onMessage={openDm} focus={mapNode} />
            </Suspense>
          )}
          {tab === "telemetry" && (
            <Suspense fallback={<Empty>Loading charts…</Empty>}>
              <Telemetry />
            </Suspense>
          )}
          {tab === "status" && <Status status={status.data} />}
          {tab === "settings" && <Settings status={status.data} channels={channels.data ?? []} />}
        </main>
      </div>
    </TicksContext.Provider>
  );
}
