import { useEffect, useState } from "react";
import {
  api,
  useResource,
  type AppForward,
  type Channel,
  type Conversations,
  type MapProvider,
  type MapStyle,
  type Status,
} from "../api";
import { notifyState, setNotify, type NotifyState } from "../notify";
import { usePrefs } from "../prefs";
import { Card, Chip, Empty, Pill, Segmented, Sheet, nodeIdHex } from "../ui";

const input =
  "w-full min-w-0 rounded-lg border border-ink-700 bg-ink-800 px-3 py-2 text-sm text-mist-200 outline-none placeholder:text-mist-400 focus:border-accent-400/60 disabled:opacity-50";
const button =
  "shrink-0 rounded-full border border-accent-400/40 bg-accent-400/10 px-4 py-1.5 text-xs font-semibold text-accent-400 disabled:opacity-40";
const quiet =
  "shrink-0 rounded-full border border-ink-700 bg-ink-800/70 px-3 py-1.5 text-xs text-mist-400 hover:text-mist-200 disabled:opacity-40";

export default function Settings({ status, channels }: { status: Status | null; channels: Channel[] }) {
  const { prefs, error } = usePrefs();
  if (!prefs) return <Empty>{error ?? "Loading…"}</Empty>;
  return (
    <div className="space-y-3">
      <NodeCard status={status} />
      <AppAccessCard />
      <AppForwardCard />
      <MapCard />
      <NotificationsCard />
      <MutedCard channels={channels} />
      <p className="px-1 text-[11px] text-mist-400">
        How each telemetry value is shown (graph, number or hidden) is set on the Telemetry tab under “Customise”.
        Settings here are stored by the service, so every browser sees the same ones.
      </p>
      <DangerZone />
    </div>
  );
}

function useSaver() {
  const { save } = usePrefs();
  const [state, setState] = useState<{ busy: boolean; msg: string | null; ok: boolean }>({
    busy: false,
    msg: null,
    ok: true,
  });
  const run = async (patch: Parameters<typeof save>[0], done = "Saved") => {
    setState({ busy: true, msg: null, ok: true });
    try {
      await save(patch);
      setState({ busy: false, msg: done, ok: true });
    } catch (e) {
      setState({ busy: false, msg: e instanceof Error ? e.message : String(e), ok: false });
    }
  };
  return { ...state, run };
}

function Feedback({ msg, ok }: { msg: string | null; ok: boolean }) {
  if (!msg) return null;
  return <p className={`mt-2 text-xs ${ok ? "text-signal-400" : "text-alert-400"}`}>{msg}</p>;
}

function NodeCard({ status }: { status: Status | null }) {
  const { prefs } = usePrefs();
  const savedHost = prefs!.upstream_host;
  const savedPort = prefs!.upstream_port;
  const [host, setHost] = useState(savedHost);
  const [port, setPort] = useState(String(savedPort));
  const saver = useSaver();
  useEffect(() => {
    setHost(savedHost);
    setPort(String(savedPort));
  }, [savedHost, savedPort]);

  const locked = prefs!.upstream_source === "command line";
  const portNum = Number(port);
  const valid = host.trim().length > 0 && Number.isInteger(portNum) && portNum > 0 && portNum < 65536;
  const changed = host.trim() !== prefs!.upstream_host || portNum !== prefs!.upstream_port;
  const up = status?.upstream;

  return (
    <Card
      title="Home node"
      right={up?.connected ? <Pill tone="good">connected</Pill> : <Pill tone="bad">not connected</Pill>}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (valid && changed)
            void saver.run({ upstream_host: host.trim(), upstream_port: portNum }, "Saved - reconnecting");
        }}
        className="space-y-2"
      >
        <input
          className={input}
          value={host}
          onChange={(e) => setHost(e.target.value)}
          placeholder="IP address or hostname"
          disabled={locked}
          aria-label="Node address"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
        />
        <div className="flex items-center gap-2">
          <label className="text-xs text-mist-400" htmlFor="node-port">
            Port
          </label>
          <input
            id="node-port"
            className={`${input.replace("w-full ", "")} w-24`}
            value={port}
            onChange={(e) => setPort(e.target.value)}
            inputMode="numeric"
            disabled={locked}
          />
          <button className={`${button} ml-auto`} disabled={locked || !valid || !changed || saver.busy}>
            Save
          </button>
        </div>
      </form>
      <p className="mt-2 text-[11px] text-mist-400">
        {locked
          ? "Set with --node on the command line, which wins over this setting. Restart without --node to change it here."
          : prefs!.upstream_source === "web ui"
            ? "Set here. Saving connects to the new address right away; the phone app stays connected to this service."
            : "From VNODE_UPSTREAM_HOST (or its default). Saving here overrides it."}
        {up?.last_error && !up.connected && (
          <span className="block text-alert-400">
            Last error: {up.last_error}
            {/not known|nodename|resolve/i.test(up.last_error) &&
              ` - "${prefs!.upstream_host}" does not resolve on this network. Enter the node's IP address instead.`}
          </span>
        )}
      </p>
      {/* Dropping the setting here does not stop at "unset": the link has to
          point somewhere, and with no VNODE_UPSTREAM_HOST that is a built-in
          default which resolves on no network at all. Name the address the
          button would move to, so it cannot quietly break the connection. */}
      {prefs!.upstream_source === "web ui" && (
        <div className="mt-2">
          <button
            className={quiet}
            disabled={saver.busy}
            onClick={() =>
              void saver.run({ upstream_host: null, upstream_port: null }, `Back to ${prefs!.upstream_fallback}`)
            }
          >
            Use {prefs!.upstream_fallback} instead
          </button>
          <p className="mt-1 text-[11px] text-mist-400">
            {prefs!.upstream_fallback_set
              ? "From VNODE_UPSTREAM_HOST."
              : "Nothing sets VNODE_UPSTREAM_HOST here, so this is the built-in default - an mDNS name that does not resolve in Docker or Home Assistant."}
          </p>
        </div>
      )}
      <Feedback msg={saver.msg} ok={saver.ok} />
    </Card>
  );
}

function AppAccessCard() {
  const { prefs } = usePrefs();
  const saver = useSaver();
  const on = prefs!.allow_admin;
  return (
    <Card
      title="Node settings from the app"
      right={<Pill tone={on ? "warn" : "neutral"}>{on ? "allowed" : "blocked"}</Pill>}
    >
      <p className="text-xs text-mist-400">
        {on
          ? "Apps connected to this service can change the node's settings (radio, channels, owner, modules) as if connected directly."
          : "Apps connected to this service can read the node's settings and star or ignore nodes, but changes are refused - the app shows an error."}
      </p>
      <button
        className={`${button} mt-3`}
        disabled={saver.busy}
        onClick={() => void saver.run({ allow_admin: !on }, on ? "Blocked" : "Allowed")}
      >
        {on ? "Block changes" : "Allow changes"}
      </button>
      <p className="mt-2 text-[11px] text-mist-400">
        Anyone who can reach the virtual node's port on your network gets this access. After a change the service
        re-reads the node's settings a few seconds later, so other apps see the new ones.
      </p>
      <Feedback msg={saver.msg} ok={saver.ok} />
    </Card>
  );
}

const FORWARD_ROWS: { key: keyof AppForward; label: string; hint: string; favorites: boolean }[] = [
  { key: "position", label: "Positions", hint: "most of the traffic", favorites: true },
  { key: "telemetry", label: "Telemetry", hint: "battery, channel use, sensors", favorites: true },
  { key: "nodeinfo", label: "Node info", hint: "names and keys of other nodes", favorites: false },
  { key: "other", label: "Everything else", hint: "waypoints, neighbour info, store & forward, …", favorites: false },
];

function AppForwardCard() {
  const { prefs } = usePrefs();
  const saver = useSaver();
  const current = prefs!.app_forward;
  const set = (key: keyof AppForward, value: string) =>
    void saver.run({ app_forward: { ...current, [key]: value } as AppForward });
  const label = { all: "Everyone", favorites: "Favourites", none: "Off" };

  return (
    <Card title="What to send to Meshtastic apps">
      <p className="mb-2 text-xs text-mist-400">
        Live traffic from other nodes, while an app is connected. Every packet wakes the phone, so less here saves
        battery.
      </p>
      <ul className="divide-y divide-ink-700">
        {FORWARD_ROWS.map((row) => {
          const options = (row.favorites ? (["all", "favorites", "none"] as const) : (["all", "none"] as const)).map(
            (value) => ({ value, label: label[value] }),
          );
          return (
            <li key={row.key} className="flex items-center justify-between gap-2 py-2">
              <div className="min-w-0">
                <div className="text-xs text-mist-200">{row.label}</div>
                <div className="truncate text-[11px] text-mist-400">{row.hint}</div>
              </div>
              <Segmented
                options={options}
                value={current[row.key]}
                onChange={(o) => set(row.key, o)}
                disabled={saver.busy}
              />
            </li>
          );
        })}
      </ul>
      <p className="mt-2 text-[11px] text-mist-400">
        Always sent: text messages and reactions, anything to or from your own node (acks, answers to what the app asked
        for), and the node's status. What is held back still reaches the app's node list the next time it connects: the
        list it gets on connect is kept up to date here.
      </p>
      <Feedback msg={saver.msg} ok={saver.ok} />
    </Card>
  );
}

/** Per provider: what to call it, its styles, and whether it wants a key.
 *  Switching provider also sends a style that provider has - the stored pair
 *  stays one the backend can serve back unchanged. */
const PROVIDERS: { id: MapProvider; label: string; note: string; styles: { id: MapStyle; label: string }[] }[] = [
  {
    id: "openfreemap",
    label: "OpenFreeMap",
    note: "OpenStreetMap data, free, no key and no account.",
    styles: [
      { id: "dark", label: "Dark" },
      { id: "liberty", label: "Liberty" },
      { id: "bright", label: "Bright" },
      { id: "positron", label: "Positron (light)" },
      { id: "fiord", label: "Fiord" },
    ],
  },
  {
    id: "carto",
    label: "CARTO",
    note: "Loads without a key, but CARTO may mark or throttle the tiles.",
    styles: [
      { id: "dark-matter", label: "Dark Matter" },
      { id: "positron", label: "Positron (light)" },
      { id: "voyager", label: "Voyager" },
    ],
  },
];

function MapCard() {
  const { prefs } = usePrefs();
  const savedKey = prefs!.carto_api_key;
  const [key, setKey] = useState(savedKey);
  const [show, setShow] = useState(false);
  const saver = useSaver();
  useEffect(() => setKey(savedKey), [savedKey]);

  const current = PROVIDERS.find((p) => p.id === prefs!.map_provider) ?? PROVIDERS[0];

  return (
    <Card title="Map">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-1 text-xs text-mist-400">Basemap</span>
        {PROVIDERS.map((p) => (
          <Chip
            key={p.id}
            on={current.id === p.id}
            onClick={() => void saver.run({ map_provider: p.id, map_style: p.styles[0].id })}
            title={p.note}
          >
            {p.label}
          </Chip>
        ))}
      </div>
      <p className="mt-2 text-[11px] text-mist-400">{current.note}</p>

      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <span className="mr-1 text-xs text-mist-400">Style</span>
        {current.styles.map((st) => (
          <Chip key={st.id} on={prefs!.map_style === st.id} onClick={() => void saver.run({ map_style: st.id })}>
            {st.label}
          </Chip>
        ))}
      </div>

      {current.id === "carto" && (
        <>
          <label className="mb-1 mt-4 block text-xs text-mist-400" htmlFor="carto-key">
            CARTO basemap API key (optional)
          </label>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void saver.run({ carto_api_key: key.trim() });
            }}
            className="flex gap-2"
          >
            <input
              id="carto-key"
              className={`${input} font-mono`}
              type={show ? "text" : "password"}
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="not set"
              autoComplete="off"
              spellCheck={false}
            />
            <button type="button" className={quiet} onClick={() => setShow((s) => !s)}>
              {show ? "Hide" : "Show"}
            </button>
            <button className={button} disabled={saver.busy || key.trim() === prefs!.carto_api_key}>
              Save
            </button>
          </form>
          <p className="mt-2 text-[11px] text-mist-400">
            Get one at{" "}
            <a
              className="text-accent-400 underline"
              href="https://carto.com/basemaps/apikey"
              target="_blank"
              rel="noreferrer"
            >
              carto.com/basemaps/apikey
            </a>
            . The browser sends it with every map tile, so if the key is restricted to certain domains, add the address
            you open this page at. It is only ever sent to CARTO.
          </p>
        </>
      )}
      <Feedback msg={saver.msg} ok={saver.ok} />
    </Card>
  );
}

function NotificationsCard() {
  const [state, setState] = useState<NotifyState>(notifyState);
  const [busy, setBusy] = useState(false);

  const toggle = async () => {
    setBusy(true);
    try {
      setState(await setNotify(state !== "on"));
    } finally {
      setBusy(false);
    }
  };

  const text: Record<NotifyState, string> = {
    on: "On in this browser: new messages notify you unless the conversation is muted or already on screen.",
    off: "Off in this browser.",
    denied: "This browser has blocked notifications for this site. Allow them in the site settings, then come back.",
    unsupported: "This browser does not support notifications.",
    insecure:
      "Browsers only allow notifications on a secure page: https, or http://localhost. This page is plain http on a " +
      "network address, so it cannot ask. Put it behind https (for example Caddy, or tailscale serve) to use them.",
  };

  return (
    <Card
      title="Notifications"
      right={<Pill tone={state === "on" ? "good" : "neutral"}>{state === "on" ? "on" : "off"}</Pill>}
    >
      <p className="text-xs text-mist-400">{text[state]}</p>
      {(state === "on" || state === "off") && (
        <button className={`${button} mt-3`} onClick={toggle} disabled={busy}>
          {state === "on" ? "Turn off" : "Turn on"}
        </button>
      )}
      <p className="mt-2 text-[11px] text-mist-400">
        Notifications come from this page, so they only arrive while it is open in a tab. On a phone the browser may
        pause a tab in the background.
      </p>
    </Card>
  );
}

function DangerZone() {
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  return (
    <section className="rounded-xl border-2 border-alert-400/60 bg-alert-400/5 p-4">
      <h2 className="text-sm font-semibold text-alert-400">⚠ Danger zone</h2>
      <p className="mt-2 text-xs text-mist-200">
        Wipe everything the vnode has stored and start over. Settings are kept. This cannot be undone.
      </p>
      <button
        onClick={() => {
          setResult(null);
          setOpen(true);
        }}
        className="mt-3 rounded-full border border-alert-400 bg-alert-400/15 px-4 py-1.5 text-xs font-semibold text-alert-400"
      >
        Clear all data from the vnode…
      </button>
      {result && <Feedback msg={result} ok />}
      {open && (
        <ClearDialog
          onClose={() => setOpen(false)}
          onDone={(msg) => {
            setOpen(false);
            setResult(msg);
          }}
        />
      )}
    </section>
  );
}

/** Says exactly what goes, with today's numbers, before anything is deleted. */
function ClearDialog({ onClose, onDone }: { onClose: () => void; onDone: (msg: string) => void }) {
  const preview = useResource<Record<string, number>>(() => api.clearPreview(), []);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const n = (key: string) => (preview.data ? (preview.data[key] ?? 0).toLocaleString() : "…");
  const clear = async () => {
    setBusy(true);
    setError(null);
    try {
      const { removed } = await api.clearData();
      onDone(
        `Cleared ${removed.packets ?? 0} messages and ${removed.nodes ?? 0} nodes, disconnected ` +
          `${removed["connected apps"] ?? 0} apps. Re-reading the node now.`,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  const deleted: [string, string][] = [
    [n("packets"), "stored messages and reactions, in every channel and DM"],
    [n("nodes"), "nodes in the node list, with names, positions and last-heard times"],
    [n("telemetry"), "telemetry samples (battery, noise floor, channel use, GPS) - the Telemetry graphs"],
    [n("positions"), "points of position tracks shown on the map"],
    [n("traffic"), 'hourly traffic counts - the "Mesh traffic heard per hour" chart'],
    [n("delivery_log"), "delivery reports for messages you sent (who acked, who repeated)"],
    [n("exchanges"), "traceroutes and position, telemetry and node info requests"],
    [n("events"), "entries in the event log on the Status page"],
    [n("clients"), "known apps, with their replay positions"],
  ];

  return (
    <Sheet label="Clear all data" onClose={onClose} locked={busy} alert>
      <h2 className="text-base font-bold text-alert-400">⚠ Clear all data from the vnode?</h2>
      <p className="mt-1 text-xs text-mist-200">This cannot be undone. There is no backup.</p>

      <h3 className="mt-4 text-xs font-semibold uppercase tracking-wider text-alert-400">Deleted</h3>
      <ul className="mt-1 space-y-1 text-xs">
        {deleted.map(([count, what]) => (
          <li key={what} className="flex gap-2">
            <span className="w-14 shrink-0 text-right font-mono tabular-nums text-alert-400">{count}</span>
            <span className="text-mist-200">{what}</span>
          </li>
        ))}
      </ul>

      <h3 className="mt-4 text-xs font-semibold uppercase tracking-wider text-mist-400">Kept</h3>
      <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-mist-200">
        <li>
          All settings on this page: node address, app access, what apps get, basemap and its key, muted chats,
          telemetry layout
        </li>
        <li>Messages already in the Meshtastic apps and on the physical node - this only clears the vnode</li>
      </ul>

      <h3 className="mt-4 text-xs font-semibold uppercase tracking-wider text-mist-400">Then</h3>
      <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-mist-200">
        <li>
          {n("connected apps")} connected app(s) are disconnected. They reconnect by themselves and start from the empty
          store, so messages they have not been given yet are lost to them.
        </li>
        <li>The vnode reconnects to the node and fetches its config and node list fresh.</li>
        <li>Message numbering starts again at 1, and new messages fill the store from now on.</li>
      </ul>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (typed === "CLEAR") void clear();
        }}
        className="mt-4 space-y-2 border-t border-ink-700 pt-3"
      >
        <label className="block text-xs text-mist-200" htmlFor="clear-confirm">
          Type <span className="font-mono font-semibold text-alert-400">CLEAR</span> to confirm
        </label>
        <input
          id="clear-confirm"
          className={`${input} border-alert-400/60 font-mono`}
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          autoFocus
          disabled={busy}
        />
        {error && <p className="text-xs text-alert-400">{error}</p>}
        <div className="flex gap-2">
          <button
            type="submit"
            disabled={typed !== "CLEAR" || busy}
            className="rounded-full bg-alert-400 px-4 py-1.5 text-xs font-bold text-ink-950 disabled:opacity-30"
          >
            {busy ? "Clearing…" : "Delete everything"}
          </button>
          <button type="button" className={quiet} onClick={onClose} disabled={busy}>
            Cancel
          </button>
        </div>
      </form>
    </Sheet>
  );
}

function MutedCard({ channels }: { channels: Channel[] }) {
  const { prefs } = usePrefs();
  const convos = useResource<Conversations>(() => api.conversations(), []);
  const saver = useSaver();
  const muted = prefs!.muted;

  const name = (key: string) => {
    const [kind, id] = key.split(":");
    if (kind === "ch") return `#${channels.find((c) => c.index === Number(id))?.name ?? `channel ${id}`}`;
    const d = convos.data?.direct.find((c) => c.node_num === Number(id));
    return `@${d ? d.short_name || d.long_name || d.node_id : nodeIdHex(Number(id))}`;
  };

  return (
    <Card title="Muted conversations">
      {muted.length === 0 ? (
        <p className="text-xs text-mist-400">None. Mute a conversation from its header on the Messages tab.</p>
      ) : (
        <ul className="flex flex-wrap gap-1.5">
          {muted.map((key) => (
            <li key={key}>
              <button
                className="rounded-full border border-warn-400/40 bg-warn-400/10 px-3 py-1.5 text-xs text-warn-400"
                onClick={() => void saver.run({ muted: muted.filter((k) => k !== key) }, "Unmuted")}
                disabled={saver.busy}
                title="Unmute"
              >
                🔕 {name(key)} ✕
              </button>
            </li>
          ))}
        </ul>
      )}
      <Feedback msg={saver.msg} ok={saver.ok} />
    </Card>
  );
}
