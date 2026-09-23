/** Small shared pieces: formatting, cards, pills, chips and sheets. */

import { useEffect, useState, type ReactNode } from "react";
import type { Node, NodeName } from "./api";

export function relTime(unix: number | null | undefined): string {
  if (!unix) return "never";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - unix));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function clockTime(unix: number): string {
  return new Date(unix * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function dayLabel(unix: number): string {
  const d = new Date(unix * 1000);
  const today = new Date();
  const same = d.toDateString() === today.toDateString();
  return same ? "Today" : d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
}

export const nodeIdHex = (num: number) => `!${(num >>> 0).toString(16).padStart(8, "0")}`;

export function nodeLabel(n: {
  long_name: string | null;
  short_name: string | null;
  node_id?: string;
  node_num?: number;
}): string {
  return n.long_name || n.short_name || n.node_id || nodeIdHex(n.node_num ?? 0);
}

export const displayName = (n: NodeName) => n.short || n.long || n.id;
export const fullName = (n: NodeName) => (n.long && n.short ? `${n.long} (${n.short})` : n.long || n.short || n.id);

/** 101 is how the firmware reports external power. */
export const batteryLabel = (level: number) => (level > 100 ? "powered" : `${Math.round(level)}%`);

export function batteryTone(level: number | null): "good" | "warn" | "bad" | "neutral" {
  if (level === null) return "neutral";
  if (level >= 50) return "good";
  if (level >= 20) return "warn";
  return "bad";
}

export const hopsLabel = (hops: number) => (hops === 0 ? "direct" : `${hops} hop${hops > 1 ? "s" : ""}`);

/** Half a grid cell in metres: how far a position shared with this many
 *  precision bits can be from the truth. 0 for exact or unknown. */
export function precisionMeters(bits: number | null): number {
  if (!bits || bits >= 32) return 0;
  return (2 ** (32 - bits) * 1e-7 * 111_320) / 2;
}

export const distanceLabel = (m: number) => (m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`);

export function Card({ title, right, children }: { title?: string; right?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-ink-700 bg-ink-900/80 p-4">
      {(title || right) && (
        <header className="mb-3 flex items-center justify-between gap-3">
          {title && <h2 className="text-sm font-semibold tracking-wide text-mist-200">{title}</h2>}
          {right}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  tone?: "neutral" | "good" | "warn" | "bad";
}) {
  const toneClass = {
    neutral: "text-mist-200",
    good: "text-signal-400",
    warn: "text-warn-400",
    bad: "text-alert-400",
  }[tone];
  return (
    <div className="rounded-lg border border-ink-700 bg-ink-800/60 px-3 py-2">
      <div className="text-[11px] uppercase tracking-wider text-mist-400">{label}</div>
      <div className={`mt-0.5 text-lg font-semibold tabular-nums ${toneClass}`}>{value}</div>
    </div>
  );
}

export function Pill({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "good" | "warn" | "bad" | "accent";
  children: ReactNode;
}) {
  const cls = {
    neutral: "border-ink-600 bg-ink-800 text-mist-400",
    good: "border-signal-600/50 bg-signal-600/15 text-signal-400",
    warn: "border-warn-400/40 bg-warn-400/10 text-warn-400",
    bad: "border-alert-400/40 bg-alert-400/10 text-alert-400",
    accent: "border-accent-400/40 bg-accent-400/10 text-accent-400",
  }[tone];
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${cls}`}>
      {children}
    </span>
  );
}

/** What a node reports about itself: battery, signal, distance, flags. */
export function NodePills({ node: n }: { node: Node }) {
  return (
    <>
      {n.battery_level !== null && (
        <Pill tone={batteryTone(n.battery_level)}>
          {batteryLabel(n.battery_level)}
          {n.voltage !== null && ` · ${n.voltage.toFixed(2)} V`}
        </Pill>
      )}
      {n.snr !== null && <Pill>{n.snr.toFixed(1)} dB</Pill>}
      {n.hops_away !== null && <Pill>{hopsLabel(n.hops_away)}</Pill>}
      {n.via_mqtt ? <Pill tone="warn">MQTT</Pill> : null}
      {n.is_ignored ? <Pill>ignored</Pill> : null}
    </>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-8 text-center text-sm text-mist-400">{children}</p>;
}

/** A toggle button in a row of filters or choices. */
export function Chip({
  on,
  onClick,
  small,
  title,
  children,
}: {
  on: boolean;
  onClick: () => void;
  small?: boolean;
  title?: string;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={on}
      title={title}
      className={`shrink-0 rounded-full border font-medium ${small ? "px-2 py-0.5 text-[11px]" : "px-3 py-1.5 text-xs"} ${
        on ? "border-accent-400/60 bg-accent-400/15 text-accent-400" : "border-ink-700 bg-ink-800/70 text-mist-400"
      }`}
    >
      {children}
    </button>
  );
}

/** One choice out of a few, as a joined row of buttons. */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  disabled,
}: {
  options: readonly { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex shrink-0 overflow-hidden rounded-full border border-ink-700">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          disabled={disabled}
          aria-pressed={value === o.value}
          className={`px-2.5 py-1 text-[11px] font-medium ${
            value === o.value ? "bg-accent-400/15 text-accent-400" : "text-mist-400"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** A modal panel: a bottom sheet on a phone, a centred dialog elsewhere.
 *  Escape and a tap outside close it, unless `locked`. */
export function Sheet({
  label,
  onClose,
  locked,
  alert,
  className = "max-w-lg",
  children,
}: {
  label: string;
  onClose: () => void;
  locked?: boolean;
  alert?: boolean;
  className?: string;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !locked && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [locked, onClose]);

  return (
    <div
      className={`fixed inset-0 z-50 flex items-end justify-center sm:items-center ${alert ? "bg-black/60" : "bg-black/50"}`}
      onClick={() => !locked && onClose()}
    >
      <div
        role={alert ? "alertdialog" : "dialog"}
        aria-label={label}
        onClick={(e) => e.stopPropagation()}
        className={`safe-bottom max-h-[85vh] w-full overflow-y-auto rounded-t-2xl bg-ink-900 p-4 sm:rounded-2xl ${
          alert ? "border-2 border-alert-400/70" : "border border-ink-700"
        } ${className}`}
      >
        {children}
      </div>
    </div>
  );
}

export function CloseButton({ onClick }: { onClick: () => void }) {
  return (
    <button onClick={onClick} className="rounded-full px-2 py-1 text-mist-400 hover:text-mist-200" aria-label="Close">
      ✕
    </button>
  );
}

/** State remembered in this browser. Falls back to `initial` when storage is
 *  blocked or holds something `valid` rejects. */
export function useStoredState<T>(key: string, initial: T, valid: (v: unknown) => v is T) {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      const parsed: unknown = raw === null ? null : JSON.parse(raw);
      return valid(parsed) ? parsed : initial;
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* private window or blocked storage */
    }
  }, [key, value]);
  return [value, setValue] as const;
}

export const oneOf =
  <T extends string>(options: readonly T[]) =>
  (v: unknown): v is T =>
    options.includes(v as T);
