/** Known node positions on a vector basemap.
 *
 * A node that shares its position blurred (precision_bits < 32) sends the
 * centre of a grid cell; the circle around it is that cell, so the dot is not
 * read as more exact than it is. */

import maplibregl, { type GeoJSONSource, type MapLayerMouseEvent } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, useResource, type Node, type TrackPoint } from "../api";
import { mapProvider } from "../maps";
import { useTick } from "../live";
import { usePrefs } from "../prefs";
import {
  Chip,
  Empty,
  distanceLabel,
  hopsLabel,
  nodeLabel,
  oneOf,
  precisionMeters,
  relTime,
  useStoredState,
} from "../ui";
import NodeSheet from "./NodeSheet";

const HEARD = ["24h", "7d", "any"] as const;
type Heard = (typeof HEARD)[number];
const HEARD_SECONDS: Record<Heard, number | null> = { "24h": 86400, "7d": 604800, any: null };

/** How far back of a selected node's track to draw. The track itself is
 *  fetched once for 7 days, so switching span is a client-side filter. */
const SPANS = ["1h", "3h", "6h", "24h", "all"] as const;
type Span = (typeof SPANS)[number];
const SPAN_SECONDS: Record<Span, number | null> = { "1h": 3600, "3h": 10800, "6h": 21600, "24h": 86400, all: null };

/** Track age ramp: newest bright amber, oldest a dim slate that sinks into
 *  the basemap. Positions are 0 (newest) to 1 (the far end of the span). */
const AGE_RAMP: [number, string][] = [
  [0, "#fbbf24"],
  [0.5, "#a16207"],
  [1, "#475569"],
];

const rgb = (hex: string): number[] => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));

function mixHex(a: string, b: string, t: number): string {
  const [x, y] = [rgb(a), rgb(b)];
  return `#${x
    .map((v, i) =>
      Math.round(v + (y[i] - v) * t)
        .toString(16)
        .padStart(2, "0"),
    )
    .join("")}`;
}

/** The ramp evaluated in JS, for the per-vertex stops of a line gradient. */
function ageColor(t: number): string {
  const x = Math.min(1, Math.max(0, t));
  for (let i = 1; i < AGE_RAMP.length; i++) {
    const [p0, c0] = AGE_RAMP[i - 1];
    const [p1, c1] = AGE_RAMP[i];
    if (x <= p1) return mixHex(c0, c1, (x - p0) / (p1 - p0));
  }
  return AGE_RAMP[AGE_RAMP.length - 1][1];
}

/** The same ramp as a style expression, driven by each point's `age`. */
const AGE_COLORS = [
  "interpolate",
  ["linear"],
  ["get", "age"],
  ...AGE_RAMP.flat(),
] as maplibregl.ExpressionSpecification;

/** CARTO's style, tiles, sprites and glyphs all come from *.basemaps.cartocdn.com
 *  and all take the key as `?key=`. Any other host is left alone, so the key is
 *  never sent to another provider. */
function withKey(url: string, key: string): string {
  if (!key) return url;
  try {
    const u = new URL(url);
    if (!(u.hostname === "basemaps.cartocdn.com" || u.hostname.endsWith(".basemaps.cartocdn.com"))) return url;
    if (!u.searchParams.has("key")) u.searchParams.set("key", key);
    return u.toString();
  } catch {
    return url;
  }
}

function circle(lon: number, lat: number, meters: number, steps = 48): [number, number][] {
  const out: [number, number][] = [];
  const dLat = meters / 111_320;
  const dLon = meters / (111_320 * Math.cos((lat * Math.PI) / 180));
  for (let i = 0; i <= steps; i++) {
    const a = (i / steps) * 2 * Math.PI;
    out.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]);
  }
  return out;
}

/** Heard within this long counts as online, as ONLINE_WINDOW_S in the backend. */
const ONLINE_S = 2 * 3600;

const LOCAL_COLOR = "#60a5fa";
const FAVORITE_COLOR = "#fbbf24";
const UNKNOWN_HOPS_COLOR = "#94a3b8";
const NAMELESS_COLOR = "#475569";
/** By hops away: direct, 1, 2, 3, 4, and 5 or more. */
const HOP_COLORS = ["#4ade80", "#2dd4bf", "#a78bfa", "#e879f9", "#fb7185", "#f4511e"];

const nameless = (n: Node) => !n.short_name && !n.long_name;
const online = (n: Node, now: number) => n.last_heard !== null && now - n.last_heard <= ONLINE_S;

/** A node heard only by its position, never its node info, has no name to
 *  show: it stays small, muted and unlabelled rather than showing its id. */
function nodeProps(n: Node, now: number) {
  return {
    num: n.node_num,
    label: n.short_name || n.long_name || "",
    color: n.is_local
      ? LOCAL_COLOR
      : n.is_favorite
        ? FAVORITE_COLOR
        : nameless(n)
          ? NAMELESS_COLOR
          : n.hops_away === null
            ? UNKNOWN_HOPS_COLOR
            : HOP_COLORS[Math.min(n.hops_away, HOP_COLORS.length - 1)],
    radius: n.is_local ? 7 : n.is_favorite ? 6 : nameless(n) ? 4 : 5,
    opacity: n.is_local || online(n, now) ? 1 : 0.45,
  };
}

/** Which node of a stack speaks for it: ours, favourites, then online and
 *  near before stale and far, nameless last. */
function rank(n: Node, now: number): number {
  if (n.is_local) return 0;
  if (n.is_favorite) return 1;
  return 2 + (nameless(n) ? 20 : 0) + (online(n, now) ? 0 : 10) + (n.hops_away ?? 8) / 10;
}

/** Nodes at the very same coordinates. Blurred positions are snapped to the
 *  centre of their grid cell, so on a busy mesh many nodes share one. */
type Stack = { key: string; lon: number; lat: number; nodes: Node[] };

const stackKey = (n: Node) => `${n.longitude},${n.latitude}`;

function stackNodes(nodes: Node[]): Stack[] {
  const now = Date.now() / 1000;
  const stacks = new Map<string, Stack>();
  for (const n of nodes) {
    const key = stackKey(n);
    const s = stacks.get(key);
    if (s) s.nodes.push(n);
    else stacks.set(key, { key, lon: n.longitude as number, lat: n.latitude as number, nodes: [n] });
  }
  for (const s of stacks.values()) s.nodes.sort((a, b) => rank(a, now) - rank(b, now));
  return [...stacks.values()];
}

const feature = (geometry: GeoJSON.Geometry, properties: GeoJSON.GeoJsonProperties): GeoJSON.Feature => ({
  type: "Feature",
  geometry,
  properties,
});
const collection = (features: GeoJSON.Feature[]): GeoJSON.FeatureCollection => ({
  type: "FeatureCollection",
  features,
});

/** Single nodes, and a counted dot for each stack except the fanned-out one.
 *  Precision circles are drawn once per stack, not once per node in it. */
function toGeoJSON(stacks: Stack[], fanned: string | null) {
  const now = Date.now() / 1000;
  const points: GeoJSON.Feature[] = [];
  const areas: GeoJSON.Feature[] = [];
  for (const s of stacks) {
    const top = nodeProps(s.nodes[0], now);
    const at: GeoJSON.Point = { type: "Point", coordinates: [s.lon, s.lat] };
    if (s.nodes.length === 1) points.push(feature(at, top));
    else if (s.key !== fanned)
      points.push(feature(at, { key: s.key, count: s.nodes.length, color: top.color, opacity: top.opacity }));
    for (const r of new Set(s.nodes.map((n) => precisionMeters(n.precision_bits))))
      if (r > 0) areas.push(feature({ type: "Polygon", coordinates: [circle(s.lon, s.lat, r)] }, { color: top.color }));
  }
  return { points: collection(points), areas: collection(areas) };
}

/** Spacing of a fanned-out stack, in screen pixels. */
const FAN_MIN_RADIUS_PX = 30;
const FAN_GAP_PX = 26;

/** A label on the far side of its dot from the stack centre, so it does not
 *  cover the neighbouring dots on the ring. */
function outward(a: number) {
  const [x, y] = [Math.cos(a), Math.sin(a)];
  const v = y > 0.4 ? "top" : y < -0.4 ? "bottom" : "";
  const h = x > 0.4 ? "left" : x < -0.4 ? "right" : "";
  return { anchor: [v, h].filter(Boolean).join("-") || "center", offset: [x * 0.7, y * 0.7] };
}

/** A stack's nodes on a ring around the position they share, each with a leg
 *  back to it. Laid out in pixels, so it is redrawn as the map moves. */
function fanGeoJSON(m: maplibregl.Map, s: Stack): GeoJSON.FeatureCollection {
  const now = Date.now() / 1000;
  const c = m.project([s.lon, s.lat]);
  const r = Math.max(FAN_MIN_RADIUS_PX, (s.nodes.length * FAN_GAP_PX) / (2 * Math.PI));
  return collection(
    s.nodes.flatMap((n, i) => {
      const a = -Math.PI / 2 + (i / s.nodes.length) * 2 * Math.PI;
      const at = m.unproject([c.x + r * Math.cos(a), c.y + r * Math.sin(a)]).toArray();
      const props = nodeProps(n, now);
      return [
        feature({ type: "LineString", coordinates: [[s.lon, s.lat], at] }, { color: props.color }),
        feature({ type: "Point", coordinates: at }, { ...props, ...outward(a) }),
      ];
    }),
  );
}

/** Bounds of a circle around a point: the opening view is this node and
 *  what is within reach of it, not every node ever heard (one with a bogus
 *  position on another continent would zoom the map out to the world). */
function aroundBounds(lon: number, lat: number, meters: number): maplibregl.LngLatBounds {
  const dLat = meters / 111_320;
  const dLon = meters / (111_320 * Math.cos((lat * Math.PI) / 180));
  return new maplibregl.LngLatBounds([lon - dLon, lat - dLat], [lon + dLon, lat + dLat]);
}

const OPENING_RADIUS_M = 10_000;
const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

/** How old each point is, as 0 (newest) to 1 (the far end of the span).
 *  With a span selected the scale is that span, so the colours mean the same
 *  thing as you pan around; on "All" it stretches over the track's own age. */
function ageScale(points: TrackPoint[], windowSecs: number | null): (t: number) => number {
  const newest = points[points.length - 1].time;
  const span = windowSecs ?? Math.max(1, newest - points[0].time);
  return (t) => Math.min(1, Math.max(0, (newest - t) / span));
}

function trackGeoJSON(points: TrackPoint[], windowSecs: number | null): GeoJSON.FeatureCollection {
  if (points.length === 0) return EMPTY;
  const age = ageScale(points, windowSecs);
  const coords = points.map((p) => [p.longitude, p.latitude]);
  return {
    type: "FeatureCollection",
    features: [
      ...(coords.length > 1
        ? [
            {
              type: "Feature",
              geometry: { type: "LineString", coordinates: coords },
              properties: {},
            } as GeoJSON.Feature,
          ]
        : []),
      ...points.map(
        (p) =>
          ({
            type: "Feature",
            geometry: { type: "Point", coordinates: [p.longitude, p.latitude] },
            properties: { age: age(p.time) },
          }) as GeoJSON.Feature,
      ),
    ],
  };
}

/** The track line coloured by age.
 *
 * `line-gradient` interpolates over `line-progress`, which is distance along
 * the line, not time - so rather than a fixed ramp we place one stop per
 * vertex at that vertex's own distance, carrying its age colour. The result is
 * smooth and still true to the timestamps. Stops must strictly ascend, so
 * repeated positions (zero-length steps) are dropped. */
function trackGradient(points: TrackPoint[], windowSecs: number | null): maplibregl.ExpressionSpecification | null {
  if (points.length < 2) return null;
  const at: number[] = [0];
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1];
    const b = points[i];
    const dx = (b.longitude - a.longitude) * Math.cos((a.latitude * Math.PI) / 180);
    at.push(at[i - 1] + Math.hypot(dx, b.latitude - a.latitude));
  }
  const total = at[at.length - 1];
  if (!(total > 0)) return null;
  const age = ageScale(points, windowSecs);
  const stops: (number | string)[] = [];
  let prev = -1;
  for (let i = 0; i < points.length; i++) {
    const p = at[i] / total;
    if (p <= prev) continue;
    prev = p;
    stops.push(p, ageColor(age(points[i].time)));
  }
  if (stops.length < 4) return null;
  return ["interpolate", ["linear"], ["line-progress"], ...stops] as maplibregl.ExpressionSpecification;
}

/** Shared by single nodes and the nodes of a fanned-out stack. */
const DOT_PAINT = {
  "circle-radius": ["get", "radius"],
  "circle-color": ["get", "color"],
  "circle-opacity": ["get", "opacity"],
  "circle-stroke-color": "#070b14",
  "circle-stroke-width": 1.5,
  "circle-stroke-opacity": ["get", "opacity"],
} as maplibregl.CircleLayerSpecification["paint"];

const LABEL = {
  layout: {
    "text-field": ["get", "label"],
    "text-size": 11,
    "text-offset": [0, 1.1],
    "text-anchor": "top",
    "text-optional": true,
  },
  paint: { "text-color": "#c6d0e0", "text-halo-color": "#070b14", "text-halo-width": 1.5 },
} as Pick<maplibregl.SymbolLayerSpecification, "layout" | "paint">;

const isStack = ["has", "count"] as maplibregl.ExpressionSpecification;
const isPoint = ["==", ["geometry-type"], "Point"] as maplibregl.ExpressionSpecification;

const LEGEND: [string, string][] = [
  [LOCAL_COLOR, "this node"],
  [FAVORITE_COLOR, "favourite"],
  [HOP_COLORS[0], "direct"],
  ...HOP_COLORS.slice(1).map((c, i): [string, string] => [
    c,
    i + 2 === HOP_COLORS.length ? `${i + 1}+ hops` : `${i + 1}`,
  ]),
  [UNKNOWN_HOPS_COLOR, "hops unknown"],
  [NAMELESS_COLOR, "name not heard"],
];

export default function MapView({
  onMessage,
  focus,
}: {
  onMessage: (n: Node) => void;
  /** A node to centre on, e.g. from "Map" in the node list. */
  focus: number | null;
}) {
  const { prefs } = usePrefs();
  const tick = useTick("nodes");
  const nodes = useResource<Node[]>(() => api.nodes(), [tick]);
  const [heard, setHeard] = useStoredState<Heard>("vnode.mapHeard", "7d", oneOf(HEARD));
  const [span, setSpan] = useStoredState<Span>("vnode.mapTrackSpan", "all", oneOf(SPANS));
  const [selected, setSelected] = useState<number | null>(focus);
  // The stack clicked open. Without one, the selected node's stack is fanned
  // out, so the selected node is always visible on its own.
  const [expanded, setExpanded] = useState<string | null>(null);
  // The full card over the map, where the node can also be asked for a
  // traceroute or a fresh position.
  const [details, setDetails] = useState<Node | null>(null);
  const [track, setTrack] = useState<TrackPoint[] | null>(null);
  const [mapError, setMapError] = useState<string | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const fitted = useRef(false);
  // Bumped when a (new) map finished loading, so the data effects re-run.
  const [loaded, setLoaded] = useState(0);

  const placed = useMemo(() => {
    const now = Date.now() / 1000;
    const window = HEARD_SECONDS[heard];
    return (nodes.data ?? []).filter(
      (n) =>
        n.latitude !== null &&
        n.longitude !== null &&
        !(n.latitude === 0 && n.longitude === 0) &&
        (n.is_local ||
          n.is_favorite ||
          n.node_num === focus ||
          window === null ||
          (n.last_heard !== null && now - n.last_heard <= window)),
    );
  }, [nodes.data, heard, focus]);

  const stacks = useMemo(() => stackNodes(placed), [placed]);
  const selectedNode = placed.find((n) => n.node_num === selected) ?? null;
  const fanKey = expanded ?? (selectedNode ? stackKey(selectedNode) : null);
  const fan = useMemo(() => stacks.find((s) => s.key === fanKey && s.nodes.length > 1) ?? null, [stacks, fanKey]);

  const provider = prefs?.map_provider ?? "openfreemap";
  const style = prefs?.map_style ?? "dark";
  // Only CARTO takes a key; with any other provider it stays in the database.
  const key = provider === "carto" ? (prefs?.carto_api_key ?? "") : "";

  // One map per provider, style and key: each changes the requests the map makes.
  useEffect(() => {
    if (!container.current || !prefs) return;
    setMapError(null);
    const { styleUrl, attribution } = mapProvider(provider);
    const m = new maplibregl.Map({
      container: container.current,
      style: withKey(styleUrl(style), key),
      transformRequest: (url) => ({ url: withKey(url, key) }),
      center: [0, 30],
      zoom: 1.5,
      attributionControl: { compact: true, customAttribution: attribution },
    });
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    m.on("error", (e) => {
      const status = (e.error as { status?: number } | undefined)?.status;
      if (status === 401 || status === 403)
        setMapError(
          provider === "carto"
            ? "CARTO refused the map key for this address. Check it under Settings."
            : "The basemap provider refused the request. Try another provider under Settings.",
        );
    });
    m.on("load", () => {
      m.addSource("areas", { type: "geojson", data: EMPTY });
      m.addSource("nodes", { type: "geojson", data: EMPTY });
      m.addSource("fan", { type: "geojson", data: EMPTY });
      // lineMetrics: line-progress (and so line-gradient) is only defined with it.
      m.addSource("track", { type: "geojson", data: EMPTY, lineMetrics: true });
      m.addLayer({
        id: "areas-fill",
        type: "fill",
        source: "areas",
        paint: { "fill-color": ["get", "color"], "fill-opacity": 0.08 },
      });
      m.addLayer({
        id: "areas-line",
        type: "line",
        source: "areas",
        paint: { "line-color": ["get", "color"], "line-opacity": 0.5, "line-width": 1 },
      });
      // Below the nodes, so the dots stay clickable.
      m.addLayer({
        id: "track-line",
        type: "line",
        source: "track",
        filter: ["==", ["geometry-type"], "LineString"],
        paint: { "line-color": AGE_RAMP[0][1], "line-width": 2, "line-opacity": 0.85 },
        layout: { "line-join": "round", "line-cap": "round" },
      });
      m.addLayer({
        id: "track-points",
        type: "circle",
        source: "track",
        filter: ["==", ["geometry-type"], "Point"],
        paint: { "circle-radius": 2.5, "circle-color": AGE_COLORS, "circle-opacity": 0.9 },
      });
      m.addLayer({ id: "nodes-dot", type: "circle", source: "nodes", filter: ["!", isStack], paint: DOT_PAINT });
      m.addLayer({
        id: "stacks-dot",
        type: "circle",
        source: "nodes",
        filter: isStack,
        paint: { ...DOT_PAINT, "circle-radius": 9 },
      });
      m.addLayer({
        id: "stacks-count",
        type: "symbol",
        source: "nodes",
        filter: isStack,
        layout: { "text-field": ["to-string", ["get", "count"]], "text-size": 10, "text-allow-overlap": true },
        paint: { "text-color": "#070b14", "text-opacity": ["get", "opacity"] },
      });
      m.addLayer({ id: "nodes-label", type: "symbol", source: "nodes", filter: ["!", isStack], ...LABEL });
      m.addLayer({
        id: "fan-legs",
        type: "line",
        source: "fan",
        filter: ["==", ["geometry-type"], "LineString"],
        paint: { "line-color": ["get", "color"], "line-opacity": 0.8, "line-width": 1.5 },
      });
      m.addLayer({ id: "fan-dot", type: "circle", source: "fan", filter: isPoint, paint: DOT_PAINT });
      m.addLayer({
        id: "fan-label",
        type: "symbol",
        source: "fan",
        filter: isPoint,
        layout: { ...LABEL.layout, "text-anchor": ["get", "anchor"], "text-offset": ["get", "offset"] },
        paint: LABEL.paint,
      });
      const clickable = ["nodes-dot", "fan-dot", "stacks-dot"];
      for (const layer of clickable) {
        m.on("click", layer, (e: MapLayerMouseEvent) => {
          const props = e.features?.[0]?.properties;
          if (typeof props?.key === "string") setExpanded(props.key);
          else if (typeof props?.num === "number") {
            setSelected(props.num);
            if (layer === "nodes-dot") setExpanded(null);
          }
        });
        m.on("mouseenter", layer, () => (m.getCanvas().style.cursor = "pointer"));
        m.on("mouseleave", layer, () => (m.getCanvas().style.cursor = ""));
      }
      // A click on open map folds an opened stack back up.
      m.on("click", (e) => {
        if (!m.queryRenderedFeatures(e.point, { layers: clickable }).length) setExpanded(null);
      });
      map.current = m;
      fitted.current = false;
      setLoaded((n) => n + 1);
    });
    return () => {
      map.current = null;
      m.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider, style, key, prefs === null]);

  useEffect(() => {
    const m = map.current;
    if (!m) return;
    const { points, areas } = toGeoJSON(stacks, fan?.key ?? null);
    (m.getSource("nodes") as GeoJSONSource | undefined)?.setData(points);
    (m.getSource("areas") as GeoJSONSource | undefined)?.setData(areas);
  }, [stacks, fan, loaded]);

  useEffect(() => {
    const m = map.current;
    const src = m?.getSource("fan") as GeoJSONSource | undefined;
    if (!m || !src) return;
    if (!fan) {
      src.setData(EMPTY);
      return;
    }
    const draw = () => src.setData(fanGeoJSON(m, fan));
    draw();
    m.on("move", draw);
    return () => {
      m.off("move", draw);
    };
  }, [fan, loaded]);

  useEffect(() => {
    const m = map.current;
    if (!m) return;
    if (!fitted.current && placed.length) {
      // Opening view: the focused node, else this node and 10 km around it,
      // else (no own position) everything placed.
      const target = placed.find((n) => n.node_num === focus) ?? placed.find((n) => n.is_local);
      if (target) {
        m.fitBounds(
          aroundBounds(target.longitude as number, target.latitude as number, focus ? 2_000 : OPENING_RADIUS_M),
          {
            padding: 20,
            duration: 0,
          },
        );
      } else {
        const bounds = new maplibregl.LngLatBounds();
        for (const n of placed) bounds.extend([n.longitude as number, n.latitude as number]);
        m.fitBounds(bounds, { padding: 40, maxZoom: 13, duration: 0 });
      }
      fitted.current = true;
    }
  }, [placed, loaded, focus]);

  // "Map" pressed in the node list while the map is already open.
  useEffect(() => {
    if (focus === null) return;
    setSelected(focus);
    const n = placed.find((p) => p.node_num === focus);
    const m = map.current;
    if (n && m && fitted.current)
      m.flyTo({ center: [n.longitude as number, n.latitude as number], zoom: Math.max(m.getZoom(), 13) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus]);

  // The selected node's track, for this node and favourites (the only ones
  // with a position history).
  const tracked = selectedNode?.tracked ?? false;
  useEffect(() => {
    setTrack(null);
    if (selected === null || !tracked) return;
    let stale = false;
    api.track(selected).then(
      (points) => !stale && setTrack(points),
      () => !stale && setTrack([]),
    );
    return () => {
      stale = true;
    };
  }, [selected, tracked, tick]);

  const windowSecs = SPAN_SECONDS[span];
  const shownTrack = useMemo(() => {
    if (track === null) return null;
    if (windowSecs === null) return track;
    const cutoff = Date.now() / 1000 - windowSecs;
    return track.filter((p) => p.time >= cutoff);
  }, [track, windowSecs]);

  useEffect(() => {
    const m = map.current;
    const src = m?.getSource("track") as GeoJSONSource | undefined;
    if (!m || !src) return;
    const points = shownTrack ?? [];
    src.setData(trackGeoJSON(points, windowSecs));
    // Undefined falls the layer back to its flat line-color, for a track with
    // too few distinct positions to place gradient stops along.
    m.setPaintProperty("track-line", "line-gradient", trackGradient(points, windowSecs) ?? undefined);
  }, [shownTrack, windowSecs, loaded]);

  const withoutPosition = (nodes.data?.length ?? 0) - (nodes.data ?? []).filter((n) => n.latitude !== null).length;
  const sel = selectedNode;

  if (!prefs) return <Empty>Loading…</Empty>;

  return (
    <div className="flex h-full flex-col gap-2">
      <div className="no-scrollbar -mx-1 flex items-center gap-1.5 overflow-x-auto px-1">
        {HEARD.map((h) => (
          <Chip key={h} on={heard === h} onClick={() => setHeard(h)}>
            {h === "any" ? "Any time" : `Heard ${h}`}
          </Chip>
        ))}
        <span className="ml-auto shrink-0 pl-2 text-[11px] text-mist-400">
          {placed.length} on the map · {withoutPosition} without a position
        </span>
      </div>
      {mapError && <p className="text-xs text-alert-400">{mapError}</p>}
      {provider === "carto" && !key && (
        <p className="text-[11px] text-mist-400">
          No CARTO key set (Settings). The map still loads, but CARTO may mark the tiles or throttle them.
        </p>
      )}

      <div className="relative min-h-[65vh] flex-1 overflow-hidden rounded-xl border border-ink-700 sm:min-h-[320px]">
        {/* Inline, not `absolute inset-0`: maplibre-gl.css sets
            `.maplibregl-map { position: relative }`, and as unlayered CSS it
            beats Tailwind's layered utilities, which left the map 0px tall. */}
        <div ref={container} style={{ position: "absolute", inset: 0 }} />
        {track !== null && track.length > 0 && (
          <div className="absolute left-2 top-2 z-10 rounded-xl border border-ink-700 bg-ink-900/90 px-2 py-1.5 shadow-lg backdrop-blur">
            <div className="mb-1 text-[10px] uppercase tracking-wide text-mist-400">Track</div>
            <div className="flex items-center gap-1">
              {SPANS.map((sp) => (
                <Chip
                  key={sp}
                  small
                  on={span === sp}
                  onClick={() => setSpan(sp)}
                  title={sp === "all" ? "All stored positions (up to 7 days)" : `Positions from the past ${sp}`}
                >
                  {sp === "all" ? "All" : sp}
                </Chip>
              ))}
            </div>
            <div className="mt-1.5 flex items-center gap-1.5">
              <span className="text-[10px] text-mist-400">new</span>
              <span
                className="h-1.5 w-20 rounded-full"
                style={{
                  background: `linear-gradient(to right, ${AGE_RAMP.map(([at, c]) => `${c} ${at * 100}%`).join(", ")})`,
                }}
              />
              <span className="text-[10px] text-mist-400">old</span>
            </div>
          </div>
        )}
        {/* Bottom left: the bottom right is where the basemap attribution (i) sits. */}
        {sel && (
          <NodeCard
            node={sel}
            track={shownTrack}
            span={span}
            onClose={() => setSelected(null)}
            onMessage={onMessage}
            onDetails={setDetails}
          />
        )}
        {details && (
          <NodeSheet
            nodeNum={details.node_num}
            fallback={{ short: details.short_name, long: details.long_name, id: details.node_id }}
            onClose={() => setDetails(null)}
          />
        )}
      </div>
      <p className="hidden px-1 text-[11px] text-mist-400 sm:block">
        {LEGEND.map(([color, label]) => (
          <span key={label} className="mr-2 whitespace-nowrap">
            <span style={{ color }}>●</span> {label}
          </span>
        ))}
        <br />
        Faded: not heard in the past 2 h. A numbered dot is several nodes at one position; click it to fan them out.
        Circles show how far a blurred position can be off. Favourites are shown however long ago they were heard. A
        selected node's track fades from amber (newest) to slate (oldest).
      </p>
    </div>
  );
}

function NodeCard({
  node: n,
  track,
  span,
  onClose,
  onMessage,
  onDetails,
}: {
  node: Node;
  /** Already cut down to `span`. */
  track: TrackPoint[] | null;
  span: Span;
  onClose: () => void;
  onMessage: (n: Node) => void;
  onDetails: (n: Node) => void;
}) {
  const r = precisionMeters(n.precision_bits);
  const precision =
    n.precision_bits === null ? "precision unknown" : r === 0 ? "exact position" : `within ±${distanceLabel(r)}`;
  return (
    <div className="absolute inset-x-2 bottom-2 rounded-xl border border-ink-700 bg-ink-900/95 p-3 text-xs shadow-xl sm:right-auto sm:w-72">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-mist-200">
            {n.short_name && <span className="mr-1.5 text-accent-400">{n.short_name}</span>}
            {n.long_name ?? nodeLabel(n)}
          </div>
          <div className="font-mono text-[11px] text-mist-400">{n.node_id}</div>
        </div>
        <button onClick={onClose} className="px-1 text-mist-400 hover:text-mist-200" aria-label="Close">
          ✕
        </button>
      </div>
      <div className="mt-2 space-y-0.5 text-mist-400">
        <div>
          Heard {relTime(n.last_heard)}
          {n.hops_away !== null && ` · ${hopsLabel(n.hops_away)}`}
          {n.snr !== null && ` · ${n.snr.toFixed(1)} dB`}
        </div>
        <div>
          {n.latitude?.toFixed(5)}, {n.longitude?.toFixed(5)}
          {n.altitude !== null && ` · ${n.altitude} m`} · {precision}
        </div>
        {n.position_time && <div>Position from {relTime(n.position_time)}</div>}
        {n.tracked && track !== null && (
          <div>
            {track.length > 1
              ? `Track: ${track.length} positions since ${relTime(track[0].time)}${span === "all" ? " (last 7 days)" : ` (past ${span})`}`
              : track.length === 1
                ? `Track: one position, ${relTime(track[0].time)}`
                : span === "all"
                  ? "No movement recorded yet - the track starts with the next position that differs."
                  : `No positions in the past ${span}.`}
          </div>
        )}
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        {!n.is_local && (
          <button
            onClick={() => onMessage(n)}
            className="rounded-full border border-accent-400/40 bg-accent-400/10 px-3 py-1 text-[11px] font-semibold text-accent-400"
          >
            Message
          </button>
        )}
        <button
          onClick={() => onDetails(n)}
          className="rounded-full border border-ink-600 bg-ink-800 px-3 py-1 text-[11px] font-semibold text-mist-200"
        >
          Details
        </button>
      </div>
    </div>
  );
}
