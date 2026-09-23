/** The basemap providers. OpenFreeMap asks for nothing, so it is the default;
 *  CARTO has nicer styles and takes an optional key. `attribution` is only set
 *  where the style carries none of its own. The style ids must match
 *  MAP_STYLES in the backend. */
export const MAP_PROVIDERS = [
  {
    id: "openfreemap",
    label: "OpenFreeMap",
    note: "OpenStreetMap data, free, no key and no account.",
    styleUrl: (style: string) => `https://tiles.openfreemap.org/styles/${style}`,
    attribution:
      '<a href="https://openfreemap.org" target="_blank" rel="noreferrer">OpenFreeMap</a> · ' +
      '<a href="https://www.openmaptiles.org/" target="_blank" rel="noreferrer">OpenMapTiles</a> · ' +
      '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> contributors',
    styles: [
      { id: "dark", label: "Dark" },
      { id: "liberty", label: "Liberty" },
      { id: "bright", label: "Bright" },
      { id: "positron", label: "Positron (light)" },
      { id: "fiord", label: "Fiord" },
    ],
  },
  {
    id: "carto",
    label: "CARTO",
    note: "Loads without a key, but CARTO may mark or throttle the tiles.",
    styleUrl: (style: string) => `https://basemaps.cartocdn.com/gl/${style}-gl-style/style.json`,
    attribution: undefined,
    styles: [
      { id: "dark-matter", label: "Dark Matter" },
      { id: "positron", label: "Positron (light)" },
      { id: "voyager", label: "Voyager" },
    ],
  },
] as const;

export type MapProvider = (typeof MAP_PROVIDERS)[number]["id"];
export type MapStyle = (typeof MAP_PROVIDERS)[number]["styles"][number]["id"];

export const mapProvider = (id: MapProvider | undefined) => MAP_PROVIDERS.find((p) => p.id === id) ?? MAP_PROVIDERS[0];
