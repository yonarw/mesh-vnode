/** Browser notifications for incoming messages.
 *
 * Whether to notify is a per-browser choice (the permission is per browser
 * anyway), remembered in localStorage. Which conversations stay quiet is the
 * shared "muted" preference. Browsers only offer notifications on a secure
 * origin: https, or http://localhost. A plain http://<pi-address> page cannot
 * ask at all. */

import { api, type MessageDetails } from "./api";

const KEY = "vnode.notify";

export type NotifyState = "unsupported" | "insecure" | "denied" | "off" | "on";

export function notifyState(): NotifyState {
  if (!window.isSecureContext) return "insecure";
  if (!("Notification" in window)) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  let wanted = false;
  try {
    wanted = localStorage.getItem(KEY) === "on";
  } catch {
    /* storage blocked: treat as off */
  }
  return wanted && Notification.permission === "granted" ? "on" : "off";
}

export async function setNotify(on: boolean): Promise<NotifyState> {
  if (on && Notification.permission !== "granted") {
    await Notification.requestPermission();
  }
  try {
    localStorage.setItem(KEY, on ? "on" : "off");
  } catch {
    /* fine */
  }
  if (on) await registration();
  return notifyState();
}

let swReg: Promise<ServiceWorkerRegistration | null> | null = null;

function registration(): Promise<ServiceWorkerRegistration | null> {
  if (!swReg) {
    swReg =
      "serviceWorker" in navigator
        ? // Relative, like the icon links in index.html: an absolute "/sw.js"
          // is outside the ingress path and fails to register there.
          navigator.serviceWorker.register("./sw.js").then(
            () => navigator.serviceWorker.ready,
            () => null,
          )
        : Promise.resolve(null);
  }
  return swReg;
}

/** A tapped notification asks the page to open that conversation. */
export function onNotificationOpen(open: (hash: string) => void): () => void {
  if (!("serviceWorker" in navigator)) return () => {};
  const handler = (e: MessageEvent) => {
    if (e.data?.type === "open") open(e.data.hash);
  };
  navigator.serviceWorker.addEventListener("message", handler);
  return () => navigator.serviceWorker.removeEventListener("message", handler);
}

export function convKeyOf(m: Pick<MessageDetails, "is_dm" | "channel" | "from_num" | "to_num">, myNum: number | null) {
  return m.is_dm ? `dm:${m.from_num === myNum ? m.to_num : m.from_num}` : `ch:${m.channel}`;
}

/** Called for every newly stored packet. Looks it up and, unless it is ours,
 *  muted, or already on screen, shows a notification. */
export async function maybeNotify(
  seq: number,
  ctx: { myNum: number | null; muted: string[]; channelName: (index: number) => string; viewing: string | null },
) {
  if (notifyState() !== "on") return;
  let m: MessageDetails;
  try {
    m = await api.messageDetails(seq);
  } catch {
    return;
  }
  if (m.origin || m.from_num === ctx.myNum) return;
  const key = convKeyOf(m, ctx.myNum);
  if (ctx.muted.includes(key)) return;
  if (document.visibilityState === "visible" && document.hasFocus() && ctx.viewing === key) return;

  const who = m.sender.short || m.sender.long || m.sender.id;
  const title = m.is_dm ? who : `${who} in #${ctx.channelName(m.channel)}`;
  const body = m.emoji ? `reacted ${m.text ?? ""}` : (m.text ?? "");
  // One notification per conversation: a burst replaces itself instead of
  // stacking twenty banners.
  const options: NotificationOptions = { body, tag: key, data: { hash: "#messages", conv: key } };
  const reg = await registration();
  if (reg) await reg.showNotification(title, options);
  else new Notification(title, options);
}
