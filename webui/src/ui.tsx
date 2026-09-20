/** Small shared pieces: cards, pills, relative times. */

import type { ReactNode } from "react";

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

export function Stat({ label, value, tone = "neutral" }: { label: string; value: ReactNode; tone?: "neutral" | "good" | "warn" | "bad" }) {
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

export function Pill({ tone = "neutral", children }: { tone?: "neutral" | "good" | "warn" | "bad" | "accent"; children: ReactNode }) {
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

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-8 text-center text-sm text-mist-400">{children}</p>;
}

export function batteryTone(level: number | null): "good" | "warn" | "bad" | "neutral" {
  if (level === null) return "neutral";
  if (level > 100) return "good"; // 101 means "plugged in" in the firmware
  if (level >= 50) return "good";
  if (level >= 20) return "warn";
  return "bad";
}

export function nodeLabel(n: { long_name: string | null; short_name: string | null; node_id?: string; node_num?: number }): string {
  return n.long_name || n.short_name || n.node_id || `!${(n.node_num ?? 0).toString(16).padStart(8, "0")}`;
}
