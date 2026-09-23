/** Which views refetch when. A live event bumps only the topics it touches;
 *  the fallback poll and a change of the node link bump them all. */

import { createContext, useCallback, useContext, useState } from "react";

export type Topic = "messages" | "exchanges" | "nodes" | "status" | "telemetry";

const ALL: Topic[] = ["messages", "exchanges", "nodes", "status", "telemetry"];

const BY_EVENT: Record<string, Topic[]> = {
  packet: ["messages", "status"],
  status: ["messages"],
  exchange: ["exchanges"],
  client: ["status"],
  nodes: ["nodes"],
};

type Ticks = Record<Topic, number>;

const zero = Object.fromEntries(ALL.map((t) => [t, 0])) as Ticks;

export const TicksContext = createContext<Ticks>(zero);

export function useTicks() {
  const [ticks, setTicks] = useState<Ticks>(zero);
  const bump = useCallback((event?: string) => {
    const topics = (event && BY_EVENT[event]) || ALL;
    setTicks((t) => {
      const next = { ...t };
      for (const topic of topics) next[topic] += 1;
      return next;
    });
  }, []);
  return { ticks, bump };
}

/** A number that changes whenever one of `topics` should be refetched. */
export function useTick(...topics: Topic[]): number {
  const ticks = useContext(TicksContext);
  return topics.reduce((sum, t) => sum + ticks[t], 0);
}
