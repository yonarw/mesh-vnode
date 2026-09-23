/** Chart primitives.
 *
 * The palette is the validated dark-mode categorical order (blue, orange,
 * aqua), assigned by series identity and never by rank, so filtering the node
 * list never repaints a surviving series. Checked against this app's chart
 * surface (#131c2f): worst adjacent CVD dE 9.4, normal-vision dE 26.5, all
 * three above 3:1 contrast.
 */

import type { ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { clockTime } from "./ui";

export const SERIES = ["#3987e5", "#d95926", "#199e70"] as const;

const SURFACE = "#131c2f";
const GRID = "#1e2a42";
const AXIS = { stroke: "#8595b0", fontSize: 11 };
const AXIS_PROPS = { ...AXIS, tickLine: false, axisLine: false } as const;
// Animation is off everywhere: these re-render on every live event, and a
// chart that restarts its entrance animation each time is unreadable.
const STATIC = { isAnimationActive: false } as const;

export interface Series {
  key: string;
  name: string;
  color: string;
}

const secondsTime = (unix: number) =>
  new Date(unix * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dayTime = (unix: number) => new Date(unix * 1000).toLocaleDateString([], { day: "numeric", month: "short" });

/** Pick an x-axis label granularity from the span actually on screen, so a
 *  short window does not print the same minute five times and a month does not
 *  print only clock times. */
function timeFormatter(data: Record<string, number | null>[], key: string) {
  if (data.length < 2) return clockTime;
  const span = (data[data.length - 1][key] ?? 0) - (data[0][key] ?? 0);
  if (span < 10 * 60) return secondsTime;
  if (span > 3 * 86400) return dayTime;
  return clockTime;
}

// Compact y ticks: 200000 does not fit a 38px axis, 200k does.
const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
// Three decimals, trimmed: a 4.350-4.360 V range needs them, 12 does not.
const yTick = (v: number) => (Math.abs(v) >= 10000 ? compact.format(v) : String(Number(v.toFixed(3))));
const hourRange = (unix: number) => {
  const d = new Date(unix * 1000);
  return `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${d.getHours()}:00`;
};
const fullTime = (unix: number) =>
  new Date(unix * 1000).toLocaleString([], {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });

interface TooltipEntry {
  name?: string;
  value?: number | string;
  color?: string;
}

function VizTooltip({
  active,
  payload,
  label,
  labelFormatter,
  unit,
}: {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: number;
  labelFormatter: (v: number) => string;
  unit?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-ink-600 bg-ink-900 px-3 py-2 text-xs shadow-xl">
      <div className="mb-1 text-mist-400">{labelFormatter(label ?? 0)}</div>
      {payload
        .filter((p) => p.value !== null && p.value !== undefined)
        .map((p, i) => (
          <div key={i} className="flex items-center gap-2">
            <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: p.color }} />
            <span className="text-mist-400">{p.name}</span>
            <span className="ml-auto tabular-nums text-mist-200">
              {p.value}
              {unit ? ` ${unit}` : ""}
            </span>
          </div>
        ))}
    </div>
  );
}

function legendProps(series: Series[]) {
  // A legend is always present for two or more series, so identity is never
  // carried by color alone.
  if (series.length < 2) return null;
  return (
    <Legend
      verticalAlign="top"
      align="left"
      height={24}
      iconType="circle"
      iconSize={8}
      formatter={(value: string) => <span className="text-[11px] text-mist-400">{value}</span>}
    />
  );
}

/** Indices of points with no neighbour on either side. A line needs two
 *  points, so without a dot these would not be drawn at all - which is what
 *  happens to a sparse series (the node's local stats, every 15 minutes or
 *  so) once gaps are broken out. */
function isolatedPoints(data: Record<string, number | null>[], key: string): Set<number> {
  const has = (i: number) => i >= 0 && i < data.length && data[i][key] !== null && data[i][key] !== undefined;
  const out = new Set<number>();
  for (let i = 0; i < data.length; i++) if (has(i) && !has(i - 1) && !has(i + 1)) out.add(i);
  return out;
}

export function TimeSeries({
  data,
  series,
  domain = ["auto", "auto"],
  unit,
  height = 168,
}: {
  data: Record<string, number | null>[];
  series: Series[];
  domain?: [number | string, number | string];
  unit?: string;
  height?: number;
}) {
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          {legendProps(series)}
          {/* A numeric time axis, not a category one: samples are placed by
              when they happened, so an outage shows as a gap instead of being
              squeezed out. */}
          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={["dataMin", "dataMax"]}
            tickFormatter={timeFormatter(data, "t")}
            minTickGap={36}
            {...AXIS_PROPS}
          />
          <YAxis domain={domain} width={40} tickFormatter={yTick} {...AXIS_PROPS} />
          <Tooltip
            cursor={{ stroke: GRID, strokeWidth: 1 }}
            content={<VizTooltip labelFormatter={fullTime} unit={unit} />}
          />
          {series.map((s) => {
            const lonely = isolatedPoints(data, s.key);
            return (
              <Line
                key={s.key}
                type="monotone"
                dataKey={s.key}
                name={s.name}
                stroke={s.color}
                strokeWidth={2}
                dot={
                  lonely.size
                    ? (p: { index: number; cx: number; cy: number }) =>
                        lonely.has(p.index) ? (
                          <circle key={p.index} cx={p.cx} cy={p.cy} r={3} fill={s.color} />
                        ) : (
                          <g key={p.index} />
                        )
                    : false
                }
                activeDot={{ r: 4, strokeWidth: 2, stroke: SURFACE }}
                // Each chart gets rows of one kind, so a null is a real gap
                // (inserted where samples stopped) and must break the line.
                connectNulls={false}
                {...STATIC}
              />
            );
          })}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function StackedHours({
  data,
  series,
  height = 176,
}: {
  data: Record<string, number>[];
  series: Series[];
  height?: number;
}) {
  const top = series[series.length - 1]?.key;
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }} barCategoryGap="22%">
          <CartesianGrid stroke={GRID} vertical={false} />
          {legendProps(series)}
          <XAxis dataKey="hour" tickFormatter={timeFormatter(data, "hour")} minTickGap={36} {...AXIS_PROPS} />
          <YAxis allowDecimals={false} width={40} tickFormatter={yTick} {...AXIS_PROPS} />
          <Tooltip cursor={{ fill: "#ffffff10" }} content={<VizTooltip labelFormatter={hourRange} />} />
          {series.map((s) => (
            <Bar
              key={s.key}
              dataKey={s.key}
              name={s.name}
              stackId="packets"
              fill={s.color}
              // A 2px surface-coloured seam separates the stacked segments;
              // only the topmost one gets the rounded data-end.
              stroke={SURFACE}
              strokeWidth={2}
              radius={s.key === top ? [4, 4, 0, 0] : 0}
              // Keeps a range with one or two buckets from becoming a slab.
              maxBarSize={56}
              {...STATIC}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function ChartNote({ children }: { children: ReactNode }) {
  return <p className="mt-2 text-[11px] text-mist-400">{children}</p>;
}
