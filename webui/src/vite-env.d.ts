// Ambient types for vite's own additions, notably `import.meta.env` (src/api.ts
// uses DEV to tell the dev server from a build). A reference file rather than
// `types` in tsconfig: `types` replaces the ambient set instead of adding to
// it, which would drop @types/geojson that maplibre-gl relies on.
/// <reference types="vite/client" />
