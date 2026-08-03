// Live mode (served by FastAPI). The static-site build overwrites this file to
// set window.CMO_STATIC = true, which switches the dashboard to fetch prebuilt
// JSON snapshots (api/<name>.json) instead of the live /api/<name> endpoints.
window.CMO_STATIC = false;
