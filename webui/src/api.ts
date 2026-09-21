/** Typed wrapper over the vnode HTTP API, plus the live-event socket. */

import { useCallback, useEffect, useRef, useState } from "react";

export const BROADCAST = 4294967295;

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

export type MapProvider = "openfreemap" | "carto";
export type MapStyle = "dark" | "liberty" | "bright" | "positron" | "fiord" | "dark-matter" | "voyager";
export type DisplayMode = "graph" | "number" | "hidden";

export interface Prefs {
  upstream_host: string;
  upstream_port: number;
  upstream_source: "command line" | "web ui" | "environment";
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

export type PrefsPatch = Partial<Omit<Prefs, "upstream_source" | "upstream_host" | "upstream_port">> & {
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

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`${path}: ${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
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
  send: async (body: { text: string; channel?: number; to?: number; reply_id?: number; emoji?: boolean }) => {
    const res = await fetch(`${API}/send`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error((await res.text()) || res.statusText);
    return res.json();
  },
  messageDetails: (seq: number) => get<MessageDetails>(`/messages/${seq}`),
  prefs: () => get<Prefs>("/prefs"),
  track: (nodeNum: number, hours = 24 * 7) => get<TrackPoint[]>(`/nodes/${nodeNum}/track?hours=${hours}`),
  savePrefs: async (patch: PrefsPatch) => {
    const res = await fetch(`${API}/prefs`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (!res.ok) throw new Error(await errorText(res));
    return (await res.json()) as Prefs;
  },
  setFavorite: async (nodeNum: number, favorite: boolean) => {
    const res = await fetch(`${API}/nodes/${nodeNum}/favorite`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ favorite }),
    });
    if (!res.ok) throw new Error((await res.text()) || res.statusText);
    return res.json();
  },
  clearPreview: () => get<Record<string, number>>("/clear"),
  clearData: async () => {
    const res = await fetch(`${API}/clear`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ confirm: "CLEAR" }),
    });
    if (!res.ok) throw new Error(await errorText(res));
    return (await res.json()) as { removed: Record<string, number> };
  },
  resetCursor: async (key: string) => {
    const res = await fetch(`${API}/clients/${encodeURIComponent(key)}/reset`, { method: "POST" });
    if (!res.ok) throw new Error(res.statusText);
    return res.json();
  },
};

/** Re-run a fetch on mount, on demand, and whenever `deps` change. */
export function useResource<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const loadRef = useRef(load);
  loadRef.current = load;

  const refresh = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
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
