import { api, useResource, type EventRow, type KnownClient, type Status as StatusT } from "../api";
import { Card, Empty, Pill, Stat, relTime } from "../ui";

export default function Status({ status, tick }: { status: StatusT | null; tick: number }) {
  const clients = useResource(() => api.clients(), [tick]);
  const events = useResource<EventRow[]>(() => api.events(60), [tick]);

  if (!status) return <Empty>Loading…</Empty>;
  const up = status.upstream;

  return (
    <div className="space-y-3">
      <Card
        title="Upstream node"
        right={up.connected ? <Pill tone="good">connected</Pill> : <Pill tone="bad">disconnected</Pill>}
      >
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat
            label="Node"
            value={
              <span title={[up.my_long_name, up.my_node_id].filter(Boolean).join(" · ")}>
                {up.my_short_name ?? up.my_node_id ?? "unknown"}
                {up.my_short_name && up.my_node_id && (
                  <span className="block truncate font-mono text-[11px] font-normal text-mist-400">{up.my_node_id}</span>
                )}
              </span>
            }
          />
          <Stat label="Address" value={`${up.host}:${up.port}`} />
          <Stat label="Firmware" value={up.firmware ?? "—"} />
          <Stat label="Up since" value={relTime(up.connected_since)} />
        </div>
        {up.last_error && (
          <p className="mt-3 text-xs text-alert-400">last error: {up.last_error}</p>
        )}
        <p className="mt-3 text-xs text-mist-400">
          {up.config_frames} config frames captured {up.config_captured_at ? relTime(up.config_captured_at) : "in an earlier run"}.
          These are replayed verbatim to every app that connects.
        </p>
      </Card>

      <Card title="Store">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="Text messages" value={status.counts.texts} tone="good" />
          <Stat label="Packets" value={status.counts.packets} />
          <Stat label="Nodes" value={status.counts.nodes} />
          <Stat label="Telemetry" value={status.counts.telemetry} />
        </div>
      </Card>

      <Card title="Virtual node" right={<Pill tone="accent">{status.vnode.listen}</Pill>}>
        <div className="mb-3 flex flex-wrap gap-1.5">
          <Pill>replay: {status.vnode.replay_mode}</Pill>
          <Pill>client key: {status.vnode.client_key_mode}</Pill>
        </div>
        {status.vnode.clients.length === 0 ? (
          <p className="text-sm text-mist-400">No app connected right now.</p>
        ) : (
          <ul className="space-y-2">
            {status.vnode.clients.map((c) => (
              <li key={c.id} className="rounded-lg border border-ink-700 bg-ink-800/60 px-3 py-2 text-xs">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-mist-200">{c.peer}</span>
                  {c.live ? <Pill tone="good">live</Pill> : <Pill tone="warn">handshake</Pill>}
                </div>
                <div className="mt-1 text-mist-400">
                  cursor {c.cursor} · replayed {c.replayed} · sent {c.frames_sent} · received {c.frames_received}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Known clients" right={<Pill>max seq {clients.data?.max_seq ?? 0}</Pill>}>
        {!clients.data?.known.length ? (
          <p className="text-sm text-mist-400">Nothing has connected yet.</p>
        ) : (
          <ul className="space-y-2">
            {clients.data.known.map((c: KnownClient) => (
              <li key={c.client_key} className="flex items-center justify-between gap-3 rounded-lg border border-ink-700 bg-ink-800/60 px-3 py-2 text-xs">
                <div className="min-w-0">
                  <div className="truncate font-mono text-mist-200">{c.client_key}</div>
                  <div className="mt-0.5 text-mist-400">
                    cursor {c.last_seq} · {c.connects} connects · {c.replayed} replayed · {relTime(c.last_seen)}
                  </div>
                </div>
                <button
                  onClick={async () => {
                    await api.resetCursor(c.client_key);
                    void clients.refresh();
                  }}
                  className="shrink-0 rounded-full border border-warn-400/40 bg-warn-400/10 px-3 py-1 text-[11px] font-medium text-warn-400"
                  title="Rewind this client's cursor so the next connect replays everything"
                >
                  Reset cursor
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Recent events">
        {!events.data?.length ? (
          <p className="text-sm text-mist-400">Nothing logged yet.</p>
        ) : (
          <ul className="space-y-1 font-mono text-[11px]">
            {events.data.map((e) => (
              <li key={e.id} className="flex gap-2">
                <span className="shrink-0 text-mist-400">{new Date(e.ts * 1000).toLocaleTimeString()}</span>
                <span className={`shrink-0 ${e.level === "warn" ? "text-warn-400" : "text-mist-400"}`}>{e.source}</span>
                <span className="text-mist-200">{e.message}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
