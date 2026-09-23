import { useMemo, useState, type ReactNode } from "react";
import { api, useResource, type DisplayMode, type TelemetryNode, type TelemetrySample, type TrafficBucket } from "../api";
import { usePrefs } from "../prefs";
import { Card, Empty, Pill, Stat, nodeLabel, relTime } from "../ui";
import { ChartNote, SERIES, StackedHours, TimeSeries } from "../viz";

const RANGES = [
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
  { label: "7d", hours: 24 * 7 },
  { label: "30d", hours: 24 * 30 },
];

type Row = Record<string, number | null>;

/** Rows of the given kinds, mapped to chart records with explicit nulls.
 *
 *  Where samples stop for much longer than usual (the service was down, the
 *  node rebooted) a row of nulls is inserted, so the line breaks instead of
 *  drawing a straight stretch that looks like measurements. */
function seriesOf(samples: TelemetrySample[], kinds: string[], pick: (s: TelemetrySample) => Row): Row[] {
  const rows = samples.filter((s) => kinds.includes(s.kind)).map((s) => ({ t: s.rx_time, ...pick(s) }) as Row);
  if (rows.length < 3) return rows;
  const gaps = rows.slice(1).map((r, i) => (r.t as number) - (rows[i].t as number)).sort((a, b) => a - b);
  const typical = gaps[Math.floor(gaps.length / 2)];
  const limit = Math.max(typical * 5, 600);
  const out: Row[] = [rows[0]];
  for (let i = 1; i < rows.length; i++) {
    const prev = rows[i - 1].t as number;
    const cur = rows[i].t as number;
    if (cur - prev > limit) {
      const blank: Row = { t: Math.round((prev + cur) / 2) };
      for (const key of Object.keys(rows[i])) if (key !== "t") blank[key] = null;
      out.push(blank);
    }
    out.push(rows[i]);
  }
  return out;
}

const hasAny = (rows: Row[], key: string) => rows.some((r) => r[key] !== null && r[key] !== undefined);
const latest = (rows: Row[], key: string): number | null => {
  for (let i = rows.length - 1; i >= 0; i--) if (rows[i][key] != null) return rows[i][key];
  return null;
};

export default function Telemetry({ tick }: { tick: number }) {
  const [hours, setHours] = useState(24);
  const nodes = useResource<TelemetryNode[]>(() => api.telemetryNodes(), [tick]);
  const [selected, setSelected] = useState<number | null>(null);
  const [customising, setCustomising] = useState(false);
  const active = nodes.data?.find((n) => n.node_num === selected) ?? nodes.data?.[0] ?? null;

  const samples = useResource<TelemetrySample[]>(
    () => (active === null ? Promise.resolve([]) : api.telemetry(active.node_num, hours)),
    [active?.node_num, hours, tick],
  );

  return (
    <div className="space-y-3">
      {/* One filter row above the charts: range, then which node. */}
      <div className="no-scrollbar -mx-1 flex gap-2 overflow-x-auto px-1">
        {RANGES.map((r) => (
          <button
            key={r.hours}
            onClick={() => setHours(r.hours)}
            className={`shrink-0 rounded-full border px-3 py-1.5 text-xs font-medium ${
              hours === r.hours
                ? "border-accent-400/60 bg-accent-400/15 text-accent-400"
                : "border-ink-700 bg-ink-800/70 text-mist-400"
            }`}
          >
            {r.label}
          </button>
        ))}
        <button
          onClick={() => setCustomising((c) => !c)}
          aria-pressed={customising}
          className={`ml-auto shrink-0 rounded-full border px-3 py-1.5 text-xs font-medium ${
            customising
              ? "border-accent-400/60 bg-accent-400/15 text-accent-400"
              : "border-ink-700 bg-ink-800/70 text-mist-400"
          }`}
        >
          {customising ? "Done" : "Customise"}
        </button>
      </div>

      {!nodes.data?.length ? (
        <Empty>
          No telemetry yet. It is kept for the connected node and your favourites only, and arrives every few minutes.
        </Empty>
      ) : (
        <div className="no-scrollbar -mx-1 flex gap-2 overflow-x-auto px-1">
          {nodes.data.map((n) => (
            <button
              key={n.node_num}
              onClick={() => setSelected(n.node_num)}
              className={`shrink-0 rounded-full border px-3 py-1.5 text-xs ${
                active?.node_num === n.node_num
                  ? "border-accent-400/60 bg-accent-400/15 text-accent-400"
                  : "border-ink-700 bg-ink-800/70 text-mist-400"
              }`}
            >
              {n.is_local ? "This node" : `★ ${nodeLabel(n)}`}
            </button>
          ))}
        </div>
      )}

      {active && (
        <NodeTelemetry
          samples={samples.data ?? []}
          hours={hours}
          tick={tick}
          local={active.is_local}
          customising={customising}
        />
      )}
    </div>
  );
}

// ------------------------------------------------------------------- widgets

interface Data {
  device: Row[];
  local: Row[];
  env: Row[];
  gps: Row[];
  /** Channel use: device metrics when present, else the local stats. */
  radio: Row[];
}

interface Widget {
  id: string;
  title: string;
  rows: (d: Data) => Row[];
  series: { key: string; name: string; color: string }[];
  unit?: string;
  domain?: [number | string, number | string];
  digits?: number;
  format?: (v: number) => string;
  note?: (d: Data, local: boolean) => ReactNode;
  right?: (d: Data) => ReactNode;
}

const powered = (v: number) => (v > 100 ? "powered" : `${Math.round(v)}%`);

/** The node database is a fixed-size array compiled into the firmware: 10 on an
 *  STM32WL, 80 on an nRF52, 200 or 250 on an ESP32-S3 depending on its flash,
 *  100 on everything else (firmware, src/mesh/mesh-pb-constants.h). Nothing in
 *  the protocol says the database is full, so sitting exactly on one of those
 *  numbers is the only sign of it from out here. */
const NODEDB_CAPS = [10, 80, 100, 200, 250];
const nodedbFull = (total: number) => NODEDB_CAPS.includes(total);

/** Every value the page can show. Each is a graph, a number or hidden, as
 *  chosen under "Customise"; the default is a graph. */
const WIDGETS: Widget[] = [
  {
    id: "noise",
    title: "Noise floor",
    rows: (d) => d.local,
    series: [{ key: "noise", name: "noise floor", color: SERIES[0] }],
    unit: "dBm",
    digits: 0,
    note: () => "Background RF level the radio hears when nobody transmits. Lower (more negative) is quieter.",
  },
  {
    id: "radio",
    title: "Radio utilisation",
    rows: (d) => d.radio,
    series: [
      { key: "chUtil", name: "channel busy", color: SERIES[0] },
      { key: "airTx", name: "own transmissions", color: SERIES[1] },
    ],
    unit: "%",
    domain: [0, "auto"],
    digits: 1,
    note: (_, local) =>
      local ? "Above roughly 25% channel use the mesh starts dropping packets." : "As that node sees it.",
  },
  {
    id: "packets",
    title: "Packets per hour",
    rows: (d) => d.local,
    series: [
      { key: "rx", name: "received", color: SERIES[0] },
      { key: "tx", name: "sent", color: SERIES[1] },
      { key: "relay", name: "relayed", color: SERIES[2] },
    ],
    domain: [0, "auto"],
    digits: 0,
  },
  {
    id: "online",
    title: "Nodes online",
    rows: (d) => d.local,
    // Two counts of the same two hours: what the radio still had room to
    // remember, and what actually reached this add-on. They track each other
    // until the radio's database fills up, and separate afterwards - that gap
    // is how much the radio is forgetting. Its *total* is left off the chart on
    // purpose: it is a fixed maximum, so once full it draws a flat ceiling that
    // squashes everything else. It is in the pill instead.
    series: [
      { key: "online", name: "online (radio)", color: SERIES[0] },
      { key: "heard", name: "heard (here)", color: SERIES[1] },
    ],
    domain: [0, "auto"],
    digits: 0,
    right: (d) => {
      const known = latest(d.local, "total");
      const here = latest(d.local, "here");
      if (known === null && here === null) return null;
      return (
        <span className="flex shrink-0 gap-1.5">
          {known !== null && (
            <Pill tone={nodedbFull(known) ? "warn" : "neutral"}>
              {known} known{nodedbFull(known) ? " (full)" : ""}
            </Pill>
          )}
          {here !== null && <Pill>{here} here</Pill>}
        </span>
      );
    },
    note: (d) => {
      const known = latest(d.local, "total");
      const base =
        "Online is the radio's own count of nodes heard in the last two hours; heard is this " +
        "add-on's count over the same two hours.";
      if (known === null || !nodedbFull(known)) return base;
      return (
        base +
        ` The radio's database tops out at ${known} on this board and is full, so it drops the node` +
        " heard longest ago whenever it meets a new one. Anything it forgets is still kept here, which" +
        " is why the two counts drift apart and why the Nodes tab lists more."
      );
    },
  },
  {
    id: "sats",
    title: "GPS satellites in view",
    rows: (d) => d.gps,
    series: [{ key: "sats", name: "satellites", color: SERIES[0] }],
    domain: [0, "auto"],
    digits: 0,
    right: (d) => {
      const bits = latest(d.gps, "bits");
      return bits !== null ? <Pill>{precisionLabel(bits)}</Pill> : null;
    },
  },
  {
    id: "dop",
    title: "GPS precision (dilution of precision)",
    rows: (d) => d.gps,
    series: [
      { key: "pdop", name: "PDOP", color: SERIES[0] },
      { key: "hdop", name: "HDOP", color: SERIES[1] },
      { key: "vdop", name: "VDOP", color: SERIES[2] },
    ],
    domain: [0, "auto"],
    digits: 2,
    note: () => "Lower is better: under 2 is a good fix, above 5 a poor one.",
  },
  {
    id: "battery",
    title: "Battery",
    rows: (d) => d.device,
    series: [{ key: "battery", name: "charge", color: SERIES[0] }],
    unit: "%",
    domain: [0, 101],
    format: powered,
    right: (d) => <Pill>{relTime(latestTime(d.device))}</Pill>,
    note: (d) => ((latest(d.device, "battery") ?? 0) > 100 ? "101% is how the firmware reports external power." : null),
  },
  {
    id: "voltage",
    title: "Battery voltage",
    rows: (d) => d.device,
    series: [{ key: "voltage", name: "voltage", color: SERIES[0] }],
    unit: "V",
    digits: 2,
  },
  {
    id: "temperature",
    title: "Temperature",
    rows: (d) => d.env,
    series: [{ key: "temperature", name: "temperature", color: SERIES[1] }],
    unit: "°C",
    digits: 1,
  },
  {
    id: "humidity",
    title: "Relative humidity",
    rows: (d) => d.env,
    series: [{ key: "humidity", name: "humidity", color: SERIES[0] }],
    unit: "%",
    domain: [0, 100],
    digits: 0,
  },
  {
    id: "pressure",
    title: "Barometric pressure",
    rows: (d) => d.env,
    series: [{ key: "pressure", name: "pressure", color: SERIES[2] }],
    unit: "hPa",
    digits: 1,
  },
];

function fmt(w: Widget, v: number): string {
  if (w.format) return w.format(v);
  const n = v.toFixed(w.digits ?? 1);
  return w.unit ? `${n} ${w.unit}` : n;
}

function NodeTelemetry({
  samples,
  hours,
  tick,
  local,
  customising,
}: {
  samples: TelemetrySample[];
  hours: number;
  tick: number;
  local: boolean;
  customising: boolean;
}) {
  const { prefs, save } = usePrefs();
  const display = prefs?.telemetry_display ?? {};
  const modeOf = (id: string): DisplayMode => display[id] ?? "graph";

  const data: Data = useMemo(() => {
    const device = seriesOf(samples, ["device"], (s) => ({
      battery: s.battery_level ?? null,
      voltage: s.voltage ?? null,
      chUtil: s.channel_utilization ?? null,
      airTx: s.air_util_tx ?? null,
    }));
    const localRows = seriesOf(samples, ["local"], (s) => ({
      noise: s.noise_floor ?? null,
      rx: s.rx_per_hour ?? null,
      tx: s.tx_per_hour ?? null,
      relay: s.relay_per_hour ?? null,
      online: s.num_online_nodes ?? null,
      total: s.num_total_nodes ?? null,
      heard: s.num_heard_here ?? null,
      here: s.num_nodes_here ?? null,
      chUtil: s.channel_utilization ?? null,
      airTx: s.air_util_tx ?? null,
    }));
    const env = seriesOf(samples, ["environment"], (s) => ({
      temperature: s.temperature ?? null,
      humidity: s.relative_humidity ?? null,
      pressure: s.barometric_pressure ?? null,
    }));
    const gps = seriesOf(samples, ["gps"], (s) => ({
      sats: s.sats_in_view ?? null,
      pdop: s.pdop ?? null,
      hdop: s.hdop ?? null,
      vdop: s.vdop ?? null,
      bits: s.precision_bits ?? null,
    }));
    // Channel use arrives in both device metrics and local stats; prefer the
    // device series, which every firmware sends.
    return { device, local: localRows, env, gps, radio: hasAny(device, "chUtil") ? device : localRows };
  }, [samples]);

  // Only widgets with something to show, and only the series that have data.
  const present = WIDGETS.map((w) => {
    const rows = w.rows(data);
    return { w, rows, series: w.series.filter((s) => hasAny(rows, s.key)) };
  }).filter((x) => x.series.length > 0);

  const setMode = (id: string, mode: DisplayMode) =>
    void save({ telemetry_display: { ...display, [id]: mode } }).catch(() => {});

  const missing = local
    ? [
        !hasAny(data.local, "noise") && "noise floor",
        !hasAny(data.local, "rx") && "packet counters",
        !hasAny(data.gps, "sats") && "GPS",
      ].filter(Boolean)
    : [];

  if (!samples.length) return <Empty>No samples from this node in the last {hours} hours.</Empty>;

  const numbers = present.filter((x) => modeOf(x.w.id) === "number");
  const graphs = present.filter((x) => modeOf(x.w.id) === "graph");

  return (
    <>
      {customising && (
        <Card title="Customise">
          <p className="mb-2 text-[11px] text-mist-400">
            How each value is shown, for every node. Values this node has not sent are not listed.
          </p>
          <ul className="divide-y divide-ink-700">
            {present.map(({ w }) => (
              <li key={w.id} className="flex items-center justify-between gap-2 py-1.5">
                <span className="min-w-0 truncate text-xs text-mist-200">{w.title}</span>
                <div className="flex shrink-0 overflow-hidden rounded-full border border-ink-700">
                  {(["graph", "number", "hidden"] as DisplayMode[]).map((m) => (
                    <button
                      key={m}
                      onClick={() => setMode(w.id, m)}
                      aria-pressed={modeOf(w.id) === m}
                      className={`px-2.5 py-1 text-[11px] font-medium ${
                        modeOf(w.id) === m ? "bg-accent-400/15 text-accent-400" : "text-mist-400"
                      }`}
                    >
                      {m === "graph" ? "Graph" : m === "number" ? "Number" : "Hide"}
                    </button>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {numbers.length > 0 && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {numbers.flatMap(({ w, rows, series }) =>
            series.map((s) => {
              const v = latest(rows, s.key);
              return (
                <Stat
                  key={`${w.id}-${s.key}`}
                  label={series.length > 1 || w.series.length > 1 ? `${w.title.split(" (")[0]}: ${s.name}` : w.title}
                  value={v === null ? "—" : fmt(w, v)}
                />
              );
            }),
          )}
        </div>
      )}

      {missing.length > 0 && (
        <p className="px-1 text-[11px] text-mist-400">
          Nothing received yet in this range for: {missing.join(", ")}.
          {(missing.includes("noise floor") || missing.includes("packet counters")) &&
            " Noise floor and packet counters come in the node's local stats, which arrive far less often than" +
              " the rest - the first one can take 10-15 minutes after connecting."}
        </p>
      )}

      {graphs.map(({ w, rows, series }) => {
        const note = w.note?.(data, local);
        return (
          <Card key={w.id} title={w.title} right={w.right?.(data)}>
            <TimeSeries data={rows} series={series} domain={w.domain} unit={w.unit} />
            {note && <ChartNote>{note}</ChartNote>}
          </Card>
        );
      })}

      {local && <LocalTraffic hours={hours} tick={tick} />}
    </>
  );
}

function LocalTraffic({ hours, tick }: { hours: number; tick: number }) {
  const traffic = useResource<TrafficBucket[]>(() => api.traffic(Math.min(hours, 24 * 30)), [hours, tick]);
  return <TrafficCard buckets={traffic.data ?? []} />;
}

function precisionLabel(bits: number): string {
  if (bits >= 32) return "shares exact position";
  const meters = (2 ** (32 - bits) * 1e-7 * 111_320) / 2;
  const text = meters >= 1000 ? `${(meters / 1000).toFixed(1)} km` : `${Math.round(meters)} m`;
  return `shares position to ±${text}`;
}

function latestTime(rows: Row[]): number | null {
  return rows.length ? (rows[rows.length - 1].t as number) : null;
}

function TrafficCard({ buckets }: { buckets: TrafficBucket[] }) {
  const { data, totals } = useMemo(() => {
    const byHour = new Map<number, { hour: number; text: number; position: number; telemetry: number; other: number }>();
    const sums = { text: 0, position: 0, telemetry: 0, other: 0 };
    for (const b of buckets) {
      const row = byHour.get(b.hour) ?? { hour: b.hour, text: 0, position: 0, telemetry: 0, other: 0 };
      const slot = b.portnum === 1 ? "text" : b.portnum === 3 ? "position" : b.portnum === 67 ? "telemetry" : "other";
      row[slot] += b.count;
      sums[slot] += b.count;
      byHour.set(b.hour, row);
    }
    const hours = [...byHour.keys()];
    if (hours.length) {
      // Fill hours with nothing heard: bars sit on a category axis, and a
      // missing hour would otherwise vanish instead of reading as zero.
      for (let h = Math.min(...hours); h <= Math.max(...hours); h += 3600) {
        if (!byHour.has(h)) byHour.set(h, { hour: h, text: 0, position: 0, telemetry: 0, other: 0 });
      }
    }
    return { data: [...byHour.values()].sort((a, b) => a.hour - b.hour), totals: sums };
  }, [buckets]);

  // Three categorical slots validate on this surface; "other" folds node
  // info, routing and the rest in with position rather than adding a fourth.
  const merged = data.map((d) => ({ hour: d.hour, text: d.text, telemetry: d.telemetry, other: d.position + d.other }));
  const series = [
    { key: "text", name: "text", color: SERIES[0] },
    { key: "telemetry", name: "telemetry", color: SERIES[1] },
    { key: "other", name: "position & other", color: SERIES[2] },
  ];

  return (
    <Card title="Mesh traffic heard per hour">
      <div className="mb-3 grid grid-cols-3 gap-2">
        <Stat label="Text" value={totals.text} />
        <Stat label="Telemetry" value={totals.telemetry} />
        <Stat label="Position & other" value={totals.position + totals.other} />
      </div>
      {merged.length === 0 ? (
        <Empty>Nothing heard in this range.</Empty>
      ) : (
        <>
          <StackedHours data={merged} series={series} />
          <ChartNote>Every packet the node passed on, from all nodes. Only text is stored.</ChartNote>
        </>
      )}
    </Card>
  );
}

