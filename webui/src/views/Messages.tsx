import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  useResource,
  type Channel,
  type Conversations,
  type DeliveryEvent,
  type DeliveryStatus,
  type Message,
  type MessageDetails,
  type NodeName,
} from "../api";
import { convKey, usePrefs } from "../prefs";
import { Empty, Pill, clockTime, dayLabel } from "../ui";

// The tapbacks the Meshtastic phone apps offer, in their order. Staying with
// this set means a reaction sent here renders as a tapback there rather than
// as a stray one-character message.
const TAPBACKS = ["\u{1F44D}", "\u{1F44E}", "\u{1F602}", "\u2757", "\u2753", "\u{1F622}", "\u{1F4A9}"];

export type Target = { kind: "channel"; index: number; name: string } | { kind: "dm"; node: number; name: string };

// The last conversation, per browser: reopening the page should not drop you
// back on the primary channel every time.
const LAST_KEY = "vnode.lastConversation";

function loadLast(): Target | null {
  try {
    const m = localStorage.getItem(LAST_KEY)?.match(/^(ch|dm):(\d+)$/);
    if (!m) return null;
    return m[1] === "ch" ? { kind: "channel", index: Number(m[2]), name: "" } : { kind: "dm", node: Number(m[2]), name: "" };
  } catch {
    return null;
  }
}

function saveLast(t: Target) {
  try {
    localStorage.setItem(LAST_KEY, convKey(t));
  } catch {
    /* private window: fine */
  }
}

export default function Messages({
  myNodeNum,
  tick,
  requested,
  onViewing,
}: {
  myNodeNum: number | null;
  tick: number;
  /** A conversation another view asked to open, e.g. "Message" on a node. */
  requested: Target | null;
  /** Tells the app which conversation is on screen, so it does not notify for it. */
  onViewing: (key: string | null) => void;
}) {
  const convos = useResource<Conversations>(() => api.conversations(), [tick]);
  const [target, setTarget] = useState<Target | null>(() => requested ?? loadLast());
  const [details, setDetails] = useState<number | null>(null);

  useEffect(() => {
    if (requested) setTarget(requested);
  }, [requested]);

  // With nothing remembered, land on the primary channel.
  useEffect(() => {
    if (target || !convos.data) return;
    const first = convos.data.channels[0];
    if (first) setTarget({ kind: "channel", index: first.index, name: first.name });
  }, [convos.data, target]);

  useEffect(() => {
    if (target) saveLast(target);
    onViewing(target ? convKey(target) : null);
    return () => onViewing(null);
  }, [target, onViewing]);

  // A target opened from a link or a notification arrives without its name.
  const named = useMemo(() => (target ? withName(target, convos.data) : null), [target, convos.data]);

  const messages = useResource<Message[]>(
    () =>
      target === null
        ? Promise.resolve([])
        : api.messages(target.kind === "channel" ? { channel: target.index } : { node: target.node }),
    [target?.kind, target?.kind === "channel" ? target.index : target?.node, tick],
  );

  // A reaction is a normal send: the emoji is the payload and reply_id names
  // the message. Refresh rather than patch locally - the reaction comes back
  // through the same store as everyone else's.
  const react = useCallback(
    async (message: Message, emoji: string) => {
      if (!named) return;
      await api.send({
        text: emoji,
        emoji: true,
        reply_id: message.packet_id,
        ...(named.kind === "channel" ? { channel: named.index } : { to: named.node }),
      });
      void messages.refresh();
    },
    [named, messages],
  );

  return (
    <div className="flex h-full flex-col gap-3">
      <ConversationBar convos={convos.data} target={named} onSelect={setTarget} />
      {named && <ThreadHeader target={named} channels={convos.data?.channels ?? []} />}
      <Thread
        messages={messages.data ?? []}
        myNodeNum={myNodeNum}
        loading={messages.loading}
        onDetails={setDetails}
        onReact={react}
      />
      {named && <Composer target={named} onSent={() => void messages.refresh()} />}
      {details !== null && <DetailsSheet seq={details} tick={tick} myNodeNum={myNodeNum} onClose={() => setDetails(null)} />}
    </div>
  );
}

function withName(t: Target, convos: Conversations | null): Target {
  if (t.name || !convos) return t;
  if (t.kind === "channel") {
    const ch = convos.channels.find((c) => c.index === t.index);
    return { ...t, name: ch?.name ?? `channel ${t.index}` };
  }
  const d = convos.direct.find((c) => c.node_num === t.node);
  return { ...t, name: d ? d.short_name || d.long_name || d.node_id : `!${t.node.toString(16).padStart(8, "0")}` };
}

function ConversationBar({
  convos,
  target,
  onSelect,
}: {
  convos: Conversations | null;
  target: Target | null;
  onSelect: (t: Target) => void;
}) {
  const { prefs } = usePrefs();
  if (!convos) return <div className="h-9" />;
  const muted = new Set(prefs?.muted ?? []);
  const isActive = (t: Target) => target !== null && convKey(target) === convKey(t);

  // A DM opened from the node list may have no messages yet, so it is not in
  // the list the server returns. Show it anyway, or the composer has no context.
  const openDmMissing =
    target?.kind === "dm" && !convos.direct.some((d) => d.node_num === target.node) ? target : null;

  const chip = (t: Target, meta?: string) => (
    <button
      key={convKey(t)}
      onClick={() => onSelect(t)}
      className={`shrink-0 rounded-full border px-3 py-1.5 text-xs font-medium transition ${
        isActive(t)
          ? "border-accent-400/60 bg-accent-400/15 text-accent-400"
          : "border-ink-700 bg-ink-800/70 text-mist-400 hover:text-mist-200"
      }`}
    >
      {t.kind === "channel" && <span className="mr-1 tabular-nums opacity-60">{t.index}</span>}
      {t.kind === "channel" ? "#" : "@"}
      {t.name}
      {meta && <span className="ml-1.5 opacity-60">{meta}</span>}
      {muted.has(convKey(t)) && (
        <span className="ml-1 opacity-60" aria-label="muted">
          🔕
        </span>
      )}
    </button>
  );

  return (
    <div className="no-scrollbar -mx-1 flex gap-2 overflow-x-auto px-1 pb-1">
      {convos.channels.map((c) => chip({ kind: "channel", index: c.index, name: c.name }))}
      {openDmMissing && chip(openDmMissing, "new")}
      {convos.direct.map((d) =>
        chip({ kind: "dm", node: d.node_num, name: d.short_name || d.long_name || d.node_id }, String(d.count)),
      )}
    </div>
  );
}

function ThreadHeader({ target, channels }: { target: Target; channels: Channel[] }) {
  const { prefs, save } = usePrefs();
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState(false);
  const channel = target.kind === "channel" ? channels.find((c) => c.index === target.index) : undefined;
  const key = convKey(target);
  useEffect(() => setInfo(false), [key]);
  const muted = prefs?.muted.includes(key) ?? false;

  const toggle = async () => {
    if (!prefs) return;
    setError(null);
    try {
      await save({ muted: muted ? prefs.muted.filter((k) => k !== key) : [...prefs.muted, key] });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="-mt-1 px-1">
    <div className="flex items-center justify-between gap-2">
      <span className="truncate text-xs text-mist-400">
        {target.kind === "channel"
          ? `Channel ${target.index}${channel ? ` · ${channel.role === "PRIMARY" ? "primary" : "secondary"}` : ""}`
          : "Direct message"}
        {error && <span className="ml-2 text-alert-400">{error}</span>}
      </span>
      <div className="flex shrink-0 gap-1.5">
      {channel && (
        <button
          onClick={() => setInfo((i) => !i)}
          aria-expanded={info}
          className={`rounded-full border px-2.5 py-1 text-[11px] font-medium ${
            info ? "border-accent-400/60 bg-accent-400/15 text-accent-400" : "border-ink-700 bg-ink-800/70 text-mist-400"
          }`}
        >
          ⓘ Info
        </button>
      )}
      <button
        onClick={toggle}
        disabled={!prefs}
        aria-pressed={muted}
        title={muted ? "Muted: no notifications from this conversation" : "Mute notifications from this conversation"}
        className={`shrink-0 rounded-full border px-2.5 py-1 text-[11px] font-medium ${
          muted ? "border-warn-400/40 bg-warn-400/10 text-warn-400" : "border-ink-700 bg-ink-800/70 text-mist-400"
        }`}
      >
        {muted ? "🔕 Muted" : "🔔 Mute"}
      </button>
      </div>
    </div>
    {info && channel && <ChannelInfo channel={channel} />}
    </div>
  );
}

const KEY_TEXT: Record<string, string> = {
  none: "No encryption: readable by anyone in range",
  default: "The well-known default key: readable by anyone with a Meshtastic device",
  aes128: "Private key (AES-128): only members can read it",
  aes256: "Private key (AES-256): only members can read it",
};

/** What this channel carries and who can read it, from the node's own channel
 *  settings. Changing them is done in the Meshtastic app. */
function ChannelInfo({ channel: c }: { channel: Channel }) {
  const m = c.position_meters;
  const position =
    c.position_precision === 0
      ? "Not shared on this channel"
      : c.position_precision >= 32
        ? "Exact position"
        : `Blurred to ±${m !== null && m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m ?? 0)} m`} (${c.position_precision} bits)`;
  return (
    <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 rounded-lg border border-ink-700 bg-ink-900/80 px-3 py-2 text-xs">
      <dt className="text-mist-400">Encryption</dt>
      <dd className={c.public ? "text-warn-400" : "text-mist-200"}>{KEY_TEXT[c.key] ?? c.key}</dd>
      <dt className="text-mist-400">Your position</dt>
      <dd className="text-mist-200">
        {position}
        {c.carries_position && <span className="block text-[11px] text-mist-400">Your node broadcasts its position on this channel.</span>}
        {c.public && c.position_precision === 15 && (
          <span className="block text-[11px] text-mist-400">The firmware caps position on a public channel at this.</span>
        )}
      </dd>
      <dt className="text-mist-400">MQTT</dt>
      <dd className="text-mist-200">
        {c.uplink || c.downlink
          ? [c.uplink && "sends to MQTT", c.downlink && "receives from MQTT"].filter(Boolean).join(", ")
          : "not bridged"}
      </dd>
      {c.muted_on_node && (
        <>
          <dt className="text-mist-400">Node</dt>
          <dd className="text-mist-200">Muted on the node</dd>
        </>
      )}
      <dd className="col-span-2 mt-1 text-[11px] text-mist-400">
        Device telemetry and node info go out on the primary channel. Change channel settings in the Meshtastic app.
      </dd>
    </dl>
  );
}

function Thread({
  messages,
  myNodeNum,
  loading,
  onDetails,
  onReact,
}: {
  messages: Message[];
  myNodeNum: number | null;
  loading: boolean;
  onDetails: (seq: number) => void;
  onReact: (message: Message, emoji: string) => Promise<void>;
}) {
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  const grouped = useMemo(() => {
    const out: { day: string; items: Message[] }[] = [];
    for (const m of messages) {
      const day = dayLabel(m.rx_time);
      if (!out.length || out[out.length - 1].day !== day) out.push({ day, items: [] });
      out[out.length - 1].items.push(m);
    }
    return out;
  }, [messages]);

  if (loading && !messages.length) return <Empty>Loading…</Empty>;
  if (!messages.length)
    return (
      <div className="flex-1">
        <Empty>No messages stored for this conversation yet.</Empty>
      </div>
    );

  return (
    <div className="flex-1 overflow-y-auto rounded-xl border border-ink-700 bg-ink-900/60 p-3">
      {grouped.map((group) => (
        <div key={group.day}>
          <div className="my-3 text-center text-[11px] uppercase tracking-wider text-mist-400">{group.day}</div>
          {group.items.map((m) => (
            <Bubble
              key={m.seq}
              message={m}
              mine={myNodeNum !== null && m.from_num === myNodeNum}
              onDetails={() => onDetails(m.seq)}
              onReact={(emoji) => onReact(m, emoji)}
            />
          ))}
        </div>
      ))}
      <div ref={bottom} />
    </div>
  );
}

const displayName = (n: NodeName) => n.short || n.long || n.id;
const fullName = (n: NodeName) => (n.long && n.short ? `${n.long} (${n.short})` : n.long || n.short || n.id);

function Bubble({
  message,
  mine,
  onDetails,
  onReact,
}: {
  message: Message;
  mine: boolean;
  onDetails: () => void;
  onReact: (emoji: string) => Promise<void>;
}) {
  const [picking, setPicking] = useState(false);
  const [sending, setSending] = useState(false);
  const react = async (emoji: string) => {
    setPicking(false);
    if (sending) return;
    setSending(true);
    try {
      await onReact(emoji);
    } finally {
      setSending(false);
    }
  };
  const hops = message.hop_start - message.hop_limit;
  const who = `${message.sender.long ?? message.sender.short ?? ""} ${message.sender.id}`.trim();

  if (message.is_reaction) {
    // Its target is older than this page, so it cannot sit under it.
    return (
      <div className="mb-2 text-center text-[11px] text-mist-400" title={who}>
        {displayName(message.sender)} reacted {message.text} to an earlier message · {clockTime(message.rx_time)}
      </div>
    );
  }

  return (
    <div className={`mb-2 flex flex-col ${mine ? "items-end" : "items-start"}`}>
      <div className={`flex max-w-[85%] items-end gap-1 ${mine ? "flex-row-reverse" : ""}`}>
        <div
          className={`min-w-0 rounded-2xl px-3 py-2 text-sm ${
            mine ? "bg-accent-400/15 text-mist-200" : "bg-ink-800 text-mist-200"
          }`}
        >
          {!mine && (
            <div className="mb-0.5 text-[11px] font-semibold text-accent-400" title={who}>
              {displayName(message.sender)}
            </div>
          )}
          <div className="whitespace-pre-wrap break-words">{message.text}</div>
          {/* The whole meta line opens the details: a bare tick is too small a
              target on a phone. */}
          <button
            onClick={onDetails}
            className="mt-1 flex items-center gap-2 text-[10px] text-mist-400 hover:text-mist-200"
            title="Details"
          >
            <span>{clockTime(message.rx_time)}</span>
            {message.rx_snr !== null && <span>{message.rx_snr.toFixed(1)} dB</span>}
            {hops > 0 && <span>{hops} hop{hops > 1 ? "s" : ""}</span>}
            {mine && <DeliveryMark message={message} />}
          </button>
        </div>
        {/* Sits outside the bubble so it never covers the text, and stays a
            full-size touch target on a phone. */}
        <button
          onClick={() => setPicking((v) => !v)}
          disabled={sending}
          aria-label="Add reaction"
          aria-expanded={picking}
          title="Add reaction"
          className={`h-7 w-7 shrink-0 rounded-full border border-ink-700 text-xs leading-none transition ${
            picking ? "border-accent-400/50 bg-accent-400/15 text-accent-400" : "text-mist-400 hover:text-mist-200"
          } ${sending ? "opacity-50" : ""}`}
        >
          ☺
        </button>
      </div>

      {picking && (
        <div className={`mt-1 flex flex-wrap gap-1 ${mine ? "justify-end" : ""}`}>
          {TAPBACKS.map((e) => (
            <button
              key={e}
              onClick={() => void react(e)}
              className="h-8 w-8 rounded-full border border-ink-700 bg-ink-900 text-base leading-none hover:border-accent-400/50 hover:bg-accent-400/10"
            >
              {e}
            </button>
          ))}
        </div>
      )}

      {message.reactions.length > 0 && (
        <div className={`-mt-1 flex flex-wrap gap-1 px-2 ${mine ? "justify-end" : ""}`}>
          {message.reactions.map((r, i) => (
            <button
              key={i}
              onClick={() => void react(r.emoji)}
              disabled={sending}
              title={`${r.sender.long ?? r.sender.short ?? ""} ${r.sender.id}`.trim()}
              className="rounded-full border border-ink-700 bg-ink-900 px-1.5 py-0.5 text-[11px] hover:border-accent-400/50"
            >
              {r.emoji} <span className="text-mist-400">{displayName(r.sender)}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------ delivery marks

/** One distinct shape per state, so none depends on colour alone:
 *  clock = waiting for the node, arrow = in the node's queue, waves = heard
 *  repeated on the mesh, check = the recipient confirmed (direct messages only). */
function DeliveryIcon({ status }: { status: DeliveryStatus }) {
  const common = { width: 14, height: 14, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round" } as const;
  switch (status) {
    case "pending":
      return (
        <svg {...common} aria-hidden>
          <circle cx="8" cy="8" r="6" />
          <path d="M8 4.5V8l2.5 1.5" />
        </svg>
      );
    case "sent":
      return (
        <svg {...common} aria-hidden>
          <path d="M8 13V3M4 7l4-4 4 4" />
        </svg>
      );
    case "relayed":
      return (
        <svg {...common} aria-hidden>
          <circle cx="8" cy="8" r="1.2" fill="currentColor" />
          <path d="M5.2 5.2a4 4 0 0 0 0 5.6M10.8 5.2a4 4 0 0 1 0 5.6M3 3a7 7 0 0 0 0 10M13 3a7 7 0 0 1 0 10" />
        </svg>
      );
    case "delivered":
      return (
        <svg {...common} strokeWidth={2.2} aria-hidden>
          <path d="M3 8.5l3.2 3.2L13 4.5" />
        </svg>
      );
    case "failed":
      return (
        <svg {...common} aria-hidden>
          <path d="M4 4l8 8M12 4l-8 8" />
        </svg>
      );
  }
}

const STATUS_VIEW: Record<DeliveryStatus, { cls: string; dm: string; channel: string }> = {
  pending: { cls: "text-mist-400", dm: "Waiting for the node to take it", channel: "Waiting for the node to take it" },
  sent: { cls: "text-mist-200", dm: "In the node's transmit queue", channel: "In the node's transmit queue" },
  relayed: {
    cls: "text-accent-400",
    dm: "On the mesh: another node repeated it. Not yet confirmed by the recipient",
    channel: "On the mesh: another node repeated it",
  },
  delivered: { cls: "text-signal-400", dm: "Confirmed by the recipient", channel: "Acknowledged" },
  failed: { cls: "text-alert-400", dm: "Not delivered", channel: "Not sent" },
};

function DeliveryMark({ message }: { message: Message }) {
  // No status at all means it was sent before tracking existed: unknown, so
  // say nothing rather than show a clock forever.
  if (message.status === null) return null;
  const v = STATUS_VIEW[message.status];
  const title = message.is_dm ? v.dm : v.channel;
  return (
    <span className={`inline-flex items-center gap-1 font-semibold ${v.cls}`} title={title} aria-label={title}>
      <DeliveryIcon status={message.status} />
      {message.status === "failed" && <span>{message.status_detail ?? "failed"}</span>}
    </span>
  );
}

// ------------------------------------------------------------ details sheet

// What the routing errors mean, in words. Names from mesh.proto Routing.Error.
const ERRORS: Record<string, string> = {
  MAX_RETRANSMIT: "no acknowledgement after all retries",
  NO_RESPONSE: "the recipient did not answer",
  TIMEOUT: "timed out",
  NO_ROUTE: "no route to the recipient",
  GOT_NAK: "a node on the way refused it",
  NO_INTERFACE: "the node has no radio to send it on",
  NO_CHANNEL: "the recipient has no matching channel",
  TOO_LARGE: "too large to send",
  DUTY_CYCLE_LIMIT: "held back by the duty-cycle limit",
  PKI_FAILED: "the recipient could not decrypt it",
  PKI_UNKNOWN_PUBKEY: "the node does not know the recipient's public key yet",
  PKI_SEND_FAIL_PUBLIC_KEY: "the node does not know the recipient's public key yet",
  NOT_AUTHORIZED: "not authorised",
  RATE_LIMIT_EXCEEDED: "rate limited by the node",
};

const fullTime = (unix: number) =>
  new Date(unix * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const hex = (n: number) => `0x${n.toString(16).padStart(8, "0")}`;

function DetailsSheet({
  seq,
  tick,
  myNodeNum,
  onClose,
}: {
  seq: number;
  tick: number;
  myNodeNum: number | null;
  onClose: () => void;
}) {
  const d = useResource<MessageDetails>(() => api.messageDetails(seq), [seq, tick]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const m = d.data;
  const mine = m !== null && myNodeNum !== null && m.from_num === myNodeNum;

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 sm:items-center" onClick={onClose}>
      <div
        role="dialog"
        aria-label="Message details"
        onClick={(e) => e.stopPropagation()}
        className="safe-bottom max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-t-2xl border border-ink-700 bg-ink-900 p-4 sm:rounded-2xl"
      >
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-mist-200">Message details</h2>
          <button onClick={onClose} className="rounded-full px-2 py-1 text-mist-400 hover:text-mist-200" aria-label="Close">
            ✕
          </button>
        </div>
        {!m ? (
          <Empty>{d.error ?? "Loading…"}</Empty>
        ) : (
          <>
            <p className="mb-3 whitespace-pre-wrap break-words rounded-lg bg-ink-800 px-3 py-2 text-sm text-mist-200">{m.text}</p>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
              <Row label="From">{fullName(m.sender)}</Row>
              <Row label="To">{m.recipient ? fullName(m.recipient) : `everyone on channel ${m.channel}`}</Row>
              <Row label={mine ? "Sent" : "Received"}>{fullTime(m.rx_time)}</Row>
              {!mine && m.hop_start > 0 && (
                <Row label="Path">
                  {m.hop_start - m.hop_limit === 0 ? "heard directly" : `${m.hop_start - m.hop_limit} of ${m.hop_start} hops`}
                </Row>
              )}
              {!mine && (m.rx_snr !== null || m.rx_rssi !== null) && (
                <Row label="Signal">
                  {[m.rx_snr !== null && `SNR ${m.rx_snr.toFixed(1)} dB`, m.rx_rssi !== null && `RSSI ${m.rx_rssi} dBm`]
                    .filter(Boolean)
                    .join(" · ")}
                  {m.hop_start - m.hop_limit > 0 && <span className="text-mist-400"> (from the last relay)</span>}
                </Row>
              )}
              {m.is_dm && <Row label="Encryption">{m.pki ? "sealed to the recipient's key" : "channel key"}</Row>}
              <Row label="Packet">
                <span className="font-mono">{hex(m.packet_id)}</span>
                {m.origin && <span className="text-mist-400"> · sent from {m.origin}</span>}
              </Row>
            </dl>

            {mine && <DeliveryTimeline m={m} />}
          </>
        )}
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-mist-400">{label}</dt>
      <dd className="min-w-0 break-words text-mist-200">{children}</dd>
    </>
  );
}

function DeliveryTimeline({ m }: { m: MessageDetails }) {
  const status = m.status;
  return (
    <section className="mt-4">
      <h3 className="mb-2 flex items-center gap-2 text-xs font-semibold text-mist-200">
        Delivery
        {status && (
          <span className={`inline-flex items-center gap-1 font-normal ${STATUS_VIEW[status].cls}`}>
            <DeliveryIcon status={status} />
            {m.is_dm ? STATUS_VIEW[status].dm : STATUS_VIEW[status].channel}
          </span>
        )}
      </h3>
      {m.delivery.length === 0 ? (
        <p className="text-xs text-mist-400">
          {status === null
            ? "Sent before delivery tracking existed."
            : "Nothing reported by the node yet, or sent before the delivery log existed."}
        </p>
      ) : (
        <ol className="space-y-2 border-l border-ink-700 pl-3">
          {m.delivery.map((e) => (
            <li key={e.id} className="text-xs">
              <div className="text-mist-200">{describe(e, m)}</div>
              <div className="text-[11px] text-mist-400">
                {fullTime(e.ts)}
                {e.hops !== null && e.event === "ack" && ` · came back over ${e.hops} hop${e.hops === 1 ? "" : "s"}`}
                {e.rx_snr !== null && ` · SNR ${e.rx_snr.toFixed(1)} dB`}
                {e.rx_rssi !== null && ` · RSSI ${e.rx_rssi} dBm`}
              </div>
            </li>
          ))}
        </ol>
      )}
      <p className="mt-3 text-[11px] text-mist-400">
        {m.is_dm
          ? "A direct message is confirmed when the recipient's node acknowledges it. Before that, the most the node can report is that a neighbour repeated it."
          : "Channel messages are not acknowledged by the nodes that receive them, and nobody knows who is on a channel. So the most the node can report is that a neighbour repeated it onto the mesh. Only the first repeat is reported."}
      </p>
    </section>
  );
}

function describe(e: DeliveryEvent, m: MessageDetails): React.ReactNode {
  switch (e.event) {
    case "queued":
      return "The node took it into its transmit queue";
    case "refused":
      return `The node refused it: ${e.error ?? "queue full"}`;
    case "implicit_ack": {
      const c = e.relay_candidates ?? [];
      if (!e.relay_node) return "Heard another node repeat it";
      const byte = `…${e.relay_node.toString(16).padStart(2, "0")}`;
      if (c.length === 0) return `Heard it repeated by an unknown node (id ending ${byte})`;
      if (c.length === 1) return `Heard ${fullName(c[0])} repeat it`;
      return (
        <>
          Heard it repeated by a node whose id ends in {byte}: probably {fullName(c[0])}
          <span className="text-mist-400"> (also possible: {c.slice(1).map(displayName).join(", ")})</span>
        </>
      );
    }
    case "ack":
      return `Acknowledged by ${e.ack_from_name ? fullName(e.ack_from_name) : "the recipient"}`;
    case "nak": {
      const reason = e.error ? (ERRORS[e.error] ?? e.error) : "unknown reason";
      const from =
        e.ack_from !== null && e.ack_from !== m.from_num && e.ack_from_name ? ` (reported by ${displayName(e.ack_from_name)})` : "";
      return `Failed: ${reason}${from}`;
    }
  }
}

function Composer({ target, onSent }: { target: Target; onSent: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!text.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.send(
        target.kind === "channel"
          ? { text: text.trim(), channel: target.index }
          : { text: text.trim(), to: target.node },
      );
      setText("");
      onSent();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="safe-bottom">
      {error && (
        <div className="mb-2">
          <Pill tone="bad">{error}</Pill>
        </div>
      )}
      <div className="flex gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          maxLength={200}
          placeholder={target.kind === "channel" ? `Message #${target.name}` : `Message @${target.name}`}
          className="flex-1 rounded-full border border-ink-700 bg-ink-800 px-4 py-2.5 text-sm text-mist-200 outline-none placeholder:text-mist-400 focus:border-accent-400/60"
        />
        <button
          type="submit"
          disabled={busy || !text.trim()}
          className="rounded-full bg-accent-400/20 px-5 py-2.5 text-sm font-semibold text-accent-400 disabled:opacity-40"
        >
          {busy ? "…" : "Send"}
        </button>
      </div>
    </form>
  );
}
