/** Typed wrapper over the vnode HTTP API, plus the live-event socket. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { MapProvider, MapStyle } from "./maps";

export const BROADCAST = 4294967295;
/** What one text packet carries; the backend checks the same limit. */
export const MAX_TEXT_BYTES = 228;

/** Where this app is served from: "/" standalone, "/api/hassio_ingress/<token>/"
 *  as a Home Assistant add-on, any prefix behind a reverse proxy.
 *
 *  Derived from the bundle's own URL, not from `location`. The tab lives in the
 *  hash (see App.tsx), so the path never moves under us, and one level up from
 *  /assets/<chunk>.js is the root. Vite's `base: "./"` keeps index.html's own
 *  asset links relative.
 *
 *  Not so in dev: vite rewrites `import.meta.url` in a source module to
 *  /@fs/<absolute path on disk>, which is not a web root at all - deriving from
 *  it sends every request into the void and the UI reports the HTML it gets
 *  back as "API unreachable". The dev server always serves the app from "/",
 *  and DEV is replaced by `false` at build time, so this costs the bundle
 *  nothing. */
const BASE = import.meta.env.DEV ? "/" : new URL("../", import.meta.url).pathname;
const API = `${BASE}api`;

export interface Status {
  version: string;
  upstream: {
    connected: boolean;
    host: string;
    port: number;
    connected_since: number | null;
    config_captured_at: number | null;
    config_frames: number;
    my_node_num: number | null;
    my_node_id: string | null;
    my_short_name: string | null;
    my_long_name: string | null;
    firmware: string | null;
    last_error: string | null;
  };
  vnode: {
    listen: string;
    clients: ClientInfo[];
    replay_mode: string;
    client_key_mode: string;
  };
  counts: { packets: number; texts: number; nodes: number; telemetry: number };
  settings: Record<string, string | number | boolean>;
}

export interface ClientInfo {
  id: number;
  key: string;
  peer: string;
  connected_at: number;
  last_activity: number;
  live: boolean;
  cursor: number;
  replayed: number;
  frames_sent: number;
  frames_received: number;
}

export interface KnownClient {
  client_key: string;
  label: string | null;
  last_seq: number;
  first_seen: number;
  last_seen: number;
  connects: number;
  replayed: number;
}

export interface Channel {
  index: number;
  role: "PRIMARY" | "SECONDARY";
  name: string;
  /** none | default (the publicly known key) | aes128 | aes256 */
  key: string;
  /** Anyone can read it: no key, or a well-known one. */
  public: boolean;
  /** Bits of position shared on this channel: 0 none, 32 exact. */
  position_precision: number;
  position_meters: number | null;
  /** The node broadcasts its position on this channel. */
  carries_position: boolean;
  muted_on_node: boolean;
  uplink: boolean;
  downlink: boolean;
}

export interface Node {
  node_num: number;
  node_id: string;
  long_name: string | null;
  short_name: string | null;
  hw_model: string | null;
  role: string | null;
  last_heard: number | null;
  snr: number | null;
  hops_away: number | null;
  battery_level: number | null;
  voltage: number | null;
  latitude: number | null;
  longitude: number | null;
  altitude: number | null;
  /** How exactly the node shares its position: 32 exact, fewer = blurred. */
  precision_bits: number | null;
  position_time: number | null;
  is_local: boolean;
  is_favorite: number;
  is_ignored: number;
  via_mqtt: number;
  has_public_key: boolean;
  /** Keeps a position history: this node and favourites. */
  tracked: boolean;
}

/** A traceroute or a position/telemetry/node info request: what was asked of
 *  one node, and what came back. `direction` is "in" for a question another
 *  node asked of ours - the node answers those itself, so only the question is
 *  ever seen. */
export interface Exchange {
  id: number;
  ts: number;
  kind: "traceroute" | "position" | "telemetry" | "nodeinfo";
  node_num: number;
  node: NodeName;
  direction: "out" | "in";
  channel: number;
  packet_id: number;
  origin: string | null;
  status: "sent" | "answered" | "failed" | "expired" | "heard";
  response_ts: number | null;
  result: ExchangeResult | null;
  error: string | null;
}

export interface ExchangeResult {
  /** Traceroute: the nodes the request passed through, and the SNR of each
   *  hop in quarter-dB steps already converted; null where unknown. */
  route?: number[];
  route_names?: NodeName[];
  snr_towards?: (number | null)[];
  route_back?: number[];
  route_back_names?: NodeName[];
  snr_back?: (number | null)[];
  hops?: number | null;
  /** Position: as stored on the node. */
  latitude?: number;
  longitude?: number;
  altitude?: number | null;
  /** Telemetry: whichever metrics the node sent. */
  metric?: string;
  battery_level?: number;
  voltage?: number;
  temperature?: number;
  relative_humidity?: number;
  /** Node info. */
  long_name?: string;
  short_name?: string;
  hw_model?: string;
  role?: string;
}

export interface TrackPoint {
  time: number;
  latitude: number;
  longitude: number;
  altitude: number | null;
  precision_bits: number | null;
}

export interface NodeName {
  short: string | null;
  long: string | null;
  id: string;
}

/** Delivery state of a message we sent. `relayed` is the node's own
 *  (implicit) ack - someone repeated it; `delivered` is the recipient's ack. */
export type DeliveryStatus = "pending" | "sent" | "relayed" | "delivered" | "failed";

export interface Reaction {
  emoji: string;
  from_num: number;
  sender: NodeName;
}

export interface Message {
  seq: number;
  sender: NodeName;
  reactions: Reaction[];
  /** A reaction whose target message is not on this page. */
  is_reaction?: boolean;
  status: DeliveryStatus | null;
  status_detail: string | null;
  packet_id: number;
  from_num: number;
  to_num: number;
  from_id: string;
  to_id: string;
  channel: number;
  rx_time: number;
  text: string | null;
  is_dm: boolean;
  /** Set on a reaction; the emoji is the text. */
  emoji: number;
  reply_id: number | null;
  origin: string | null;
  rx_snr: number | null;
  rx_rssi: number | null;
  hop_start: number;
  hop_limit: number;
}

/** One thing the node reported about a message we sent. */
export interface DeliveryEvent {
  id: number;
  ts: number;
  event: "queued" | "refused" | "implicit_ack" | "ack" | "nak";
  ack_from: number | null;
  ack_from_name?: NodeName;
  error: string | null;
  /** Last byte of the node that repeated the message (implicit ack only). */
  relay_node: number | null;
  relay_candidates?: NodeName[];
  rx_snr: number | null;
  rx_rssi: number | null;
  hops: number | null;
}

export interface MessageDetails extends Omit<Message, "reactions"> {
  recipient: NodeName | null;
  want_ack: number;
  pki: number;
  stored_at: number;
  delivery: DeliveryEvent[];
}

export type DisplayMode = "graph" | "number" | "hidden";

export interface Prefs {
  upstream_host: string;
  upstream_port: number;
  upstream_source: "command line" | "web ui" | "environment";
  /** "host:port" that dropping the web-UI setting would fall back to. */
  upstream_fallback: string;
  /** Whether anything actually set that, or it is only the built-in default. */
  upstream_fallback_set: boolean;
  carto_api_key: string;
  /** Where the basemap comes from. OpenFreeMap needs no key. */
  map_provider: MapProvider;
  map_style: MapStyle;
  /** Conversation keys: "ch:<index>" or "dm:<node_num>". */
  muted: string[];
  telemetry_display: Record<string, DisplayMode>;
  /** Apps connected to the virtual node may change the node's settings. */
  allow_admin: boolean;
  /** Live traffic sent to connected apps, per category. */
  app_forward: AppForward;
}

export interface AppForward {
  position: "all" | "favorites" | "none";
  telemetry: "all" | "favorites" | "none";
  nodeinfo: "all" | "none";
  other: "all" | "none";
}

export type PrefsPatch = Partial<
  Omit<Prefs, "upstream_source" | "upstream_host" | "upstream_port" | "upstream_fallback" | "upstream_fallback_set">
> & {
  upstream_host?: string | null;
  upstream_port?: number | null;
};

export interface Conversations {
  channels: Channel[];
  direct: {
    node_num: number;
    node_id: string;
    long_name: string | null;
    short_name: string | null;
    count: number;
    last_time: number;
    last_text: string | null;
  }[];
}

/** One telemetry sample. Which fields are present depends on `kind`:
 *  device / environment / power come from telemetry packets, `local` is the
 *  connected node's own stats (noise floor, packet counters), `gps` is fix
 *  quality taken from its position reports. Server-side the series is averaged
 *  into time buckets, so values may be fractional. */
export interface TelemetrySample {
  node_num: number;
  rx_time: number;
  kind: "device" | "environment" | "power" | "local" | "gps" | string;
  samples?: number;
  battery_level?: number | null;
  voltage?: number | null;
  channel_utilization?: number | null;
  air_util_tx?: number | null;
  uptime_seconds?: number | null;
  temperature?: number | null;
  relative_humidity?: number | null;
  barometric_pressure?: number | null;
  noise_floor?: number | null;
  num_online_nodes?: number | null;
  num_total_nodes?: number | null;
  /** Counted here rather than by the radio, over the same two-hour window. */
  num_heard_here?: number | null;
  num_nodes_here?: number | null;
  rx_per_hour?: number | null;
  tx_per_hour?: number | null;
  relay_per_hour?: number | null;
  bad_per_hour?: number | null;
  dupe_per_hour?: number | null;
  sats_in_view?: number | null;
  pdop?: number | null;
  hdop?: number | null;
  vdop?: number | null;
  precision_bits?: number | null;
  gps_accuracy_mm?: number | null;
  fix_type?: number | null;
}

export interface TelemetryNode {
  node_num: number;
  samples: number;
  last_time: number;
  long_name: string | null;
  short_name: string | null;
  is_local: boolean;
  is_favorite: number;
}

export interface TrafficBucket {
  hour: number;
  portnum: number;
  count: number;
}

export interface EventRow {
  id: number;
  ts: number;
  level: string;
  source: string;
  message: string;
}

/** FastAPI puts the reason in `detail`, a string or a list of field errors. */
async function errorText(res: Response): Promise<string> {
  const body = await res.text();
  try {
    const detail = JSON.parse(body).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  } catch {
    /* not JSON */
  }
  return body || res.statusText;
}

async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    method,
    ...(body === undefined ? {} : { headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!res.ok) throw new Error(await errorText(res));
  return res.json() as Promise<T>;
}

const get = <T>(path: string) => request<T>(path);

export const api = {
  status: () => get<Status>("/status"),
  channels: () => get<Channel[]>("/channels"),
  nodes: () => get<Node[]>("/nodes"),
  conversations: () => get<Conversations>("/conversations"),
  events: (limit = 100) => get<EventRow[]>(`/events?limit=${limit}`),
  clients: () => get<{ connected: ClientInfo[]; known: KnownClient[]; max_seq: number }>("/clients"),
  traffic: (hours: number) => get<TrafficBucket[]>(`/traffic?hours=${hours}`),
  telemetryNodes: () => get<TelemetryNode[]>("/telemetry/nodes"),
  telemetry: (node: number | null, hours: number) =>
    get<TelemetrySample[]>(`/telemetry?hours=${hours}${node === null ? "" : `&node=${node}`}`),
  messages: (opts: { channel?: number; node?: number; limit?: number }) => {
    const p = new URLSearchParams();
    if (opts.channel !== undefined) p.set("channel", String(opts.channel));
    if (opts.node !== undefined) p.set("node", String(opts.node));
    p.set("limit", String(opts.limit ?? 300));
    return get<Message[]>(`/messages?${p}`);
  },
  send: (body: { text: string; channel?: number; to?: number; reply_id?: number; emoji?: boolean }) =>
    request<{ packet_id: number; seq: number | null }>("/send", "POST", body),
  messageDetails: (seq: number) => get<MessageDetails>(`/messages/${seq}`),
  exchanges: (node?: number, limit = 50) =>
    get<Exchange[]>(`/exchanges?limit=${limit}${node === undefined ? "" : `&node=${node}`}`),
  /** Ask one node for something. The answer arrives later, through the socket. */
  exchange: (body: { kind: Exchange["kind"]; node: number; channel: number }) =>
    request<Exchange & { cooldown_s: number }>("/exchange", "POST", body),
  prefs: () => get<Prefs>("/prefs"),
  track: (nodeNum: number, hours = 24 * 7) => get<TrackPoint[]>(`/nodes/${nodeNum}/track?hours=${hours}`),
  savePrefs: (patch: PrefsPatch) => request<Prefs>("/prefs", "PUT", patch),
  setFavorite: (nodeNum: number, favorite: boolean) =>
    request<unknown>(`/nodes/${nodeNum}/favorite`, "POST", { favorite }),
  clearPreview: () => get<Record<string, number>>("/clear"),
  clearData: () => request<{ removed: Record<string, number> }>("/clear", "POST", { confirm: "CLEAR" }),
  resetCursor: (key: string) => request<unknown>(`/clients/${encodeURIComponent(key)}/reset`, "POST"),
};

/** Re-run a fetch on mount, on demand, and whenever `deps` change. */
export function useResource<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const loadRef = useRef(load);
  loadRef.current = load;
  // Only the latest call may set state: an older, slower response would
  // otherwise overwrite a newer one (e.g. after switching conversations).
  const latest = useRef(0);

  const refresh = useCallback(async () => {
    const call = ++latest.current;
    try {
      const value = await loadRef.current();
      if (call !== latest.current) return;
      setData(value);
      setError(null);
    } catch (e) {
      if (call === latest.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (call === latest.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading, refresh };
}

export interface LiveEvent {
  event: string;
  payload: Record<string, unknown>;
  ts?: number;
}

/** Live events from the service. Reconnects on its own, so a restarted
 *  backend does not leave the page stale. */
export function useLiveEvents(onEvent: (ev: LiveEvent) => void) {
  const [online, setOnline] = useState(false);
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    let socket: WebSocket | null = null;
    let timer: number | undefined;
    let closed = false;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${proto}://${location.host}${API}/ws`);
      socket.onopen = () => setOnline(true);
      socket.onmessage = (m) => {
        try {
          handler.current(JSON.parse(m.data) as LiveEvent);
        } catch {
          /* ignore malformed frames */
        }
      };
      socket.onclose = () => {
        setOnline(false);
        if (!closed) timer = window.setTimeout(connect, 3000);
      };
      socket.onerror = () => socket?.close();
    };

    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, []);

  return online;
}
