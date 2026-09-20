import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import { api, useLiveEvents, useResource, type Channel, type Node, type Status as StatusT } from "./api";
import { maybeNotify, onNotificationOpen } from "./notify";
import { PrefsProvider, usePrefs } from "./prefs";
import { Pill } from "./ui";
import Messages, { type Target } from "./views/Messages";
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

/** The hash is `#tab`, `#messages/<conversation>` (ch:1, dm:123456) or
 *  `#map/<node number>`. */
function parseHash(): { tab: TabId; conv: Target | null; mapNode: number | null } {
  const [id, conv] = location.hash.replace("#", "").split("/");
  const tab: TabId = id === "settings" || TABS.some((t) => t.id === id) ? (id as TabId) : "messages";
  let target: Target | null = null;
  const m = conv?.match(/^(ch|dm):(\d+)$/);
  if (tab === "messages" && m) {
    target = m[1] === "ch" ? { kind: "channel", index: Number(m[2]), name: "" } : { kind: "dm", node: Number(m[2]), name: "" };
  }
  const mapNode = tab === "map" && /^\d+$/.test(conv ?? "") ? Number(conv) : null;
  return { tab, conv: target, mapNode };
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

  // Bumped whenever something happens upstream; views re-fetch on change.
  const [tick, setTick] = useState(0);
  const bump = useCallback(() => setTick((t) => t + 1), []);

  const status = useResource<StatusT>(() => api.status(), [tick]);
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
        bump();
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
    const id = window.setInterval(bump, 30000);
    return () => window.clearInterval(id);
  }, [bump]);

  const up = status.data?.upstream;
  const me = up?.my_short_name || up?.my_long_name || up?.my_node_id;

  useEffect(() => {
    document.title = me ? `${me} · mesh-vnode` : "mesh-vnode";
  }, [me]);

  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col overflow-x-hidden px-4">
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
          <Messages
            myNodeNum={up?.my_node_num ?? null}
            tick={tick}
            requested={requestedChat}
            onViewing={setViewing}
          />
        )}
        {tab === "nodes" && (
          <Nodes tick={tick} onMessage={openDm} onShowOnMap={(n) => (location.hash = `map/${n.node_num}`)} />
        )}
        {tab === "map" && (
          <Suspense fallback={<p className="py-8 text-center text-sm text-mist-400">Loading map…</p>}>
            <MapView tick={tick} onMessage={openDm} focus={mapNode} />
          </Suspense>
        )}
        {tab === "telemetry" && (
          <Suspense fallback={<p className="py-8 text-center text-sm text-mist-400">Loading charts…</p>}>
            <Telemetry tick={tick} />
          </Suspense>
        )}
        {tab === "status" && <Status status={status.data} tick={tick} />}
        {tab === "settings" && <Settings status={status.data} channels={channels.data ?? []} />}
      </main>
    </div>
  );
}
