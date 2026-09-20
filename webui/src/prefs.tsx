/** Settings made in the web UI, shared by every view.
 *
 * They live on the server (not in this browser) so the phone and the laptop
 * see the same muted chats, map key and telemetry layout. */

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, type Prefs, type PrefsPatch } from "./api";

interface PrefsState {
  prefs: Prefs | null;
  error: string | null;
  /** Save a change. Applied optimistically; rolled back if the server refuses. */
  save: (patch: PrefsPatch) => Promise<void>;
}

const Ctx = createContext<PrefsState>({ prefs: null, error: null, save: async () => {} });

export function PrefsProvider({ children }: { children: ReactNode }) {
  const [prefs, setPrefs] = useState<Prefs | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.prefs().then(setPrefs, (e) => setError(String(e)));
  }, []);

  const save = useCallback(async (patch: PrefsPatch) => {
    let before: Prefs | null = null;
    setPrefs((p) => {
      before = p;
      return p ? ({ ...p, ...patch } as Prefs) : p;
    });
    try {
      setPrefs(await api.savePrefs(patch));
      setError(null);
    } catch (e) {
      setPrefs(before);
      throw e;
    }
  }, []);

  return <Ctx.Provider value={{ prefs, error, save }}>{children}</Ctx.Provider>;
}

export const usePrefs = () => useContext(Ctx);

export const convKey = (t: { kind: "channel"; index: number } | { kind: "dm"; node: number }) =>
  t.kind === "channel" ? `ch:${t.index}` : `dm:${t.node}`;
