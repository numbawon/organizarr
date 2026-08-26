"""
Organizarr -- a small config hub for the *arr apps. Not a clone of every
app's settings UI: it covers the fields that tend to be genuinely
annoying to keep straight across apps by hand -- auth method,
download-client host/port, Prowlarr's app/indexer sync -- via each
app's own real API, not by touching config files directly.

Every app is optional and configured entirely through environment
variables -- see README for the full list. Only <NAME>_URL is required
to turn an app on; <NAME>_CONFIG_PATH is optional (auto-detects the API
key from that app's own config file if given, mounted read-only) and
API keys are never sent back to the browser once set.

Auto-detection is tried first, lazily and self-healing -- see
_get_api_key(). If an app's key can't be auto-detected (no
CONFIG_PATH given, volume not mounted, unusual setup), a
manually-entered override is the fallback, persisted to /state so it
survives a restart. Auto-detection always wins if it starts working
later -- the override is a safety net, not a permanent pin.

Auth: this container has no login of its own. It expects to sit behind
a reverse-proxy auth gate (see README) -- anyone who reaches this page
can change any configured app's authentication settings and
download-client credentials.
"""
import configparser
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Organizarr")

OVERRIDE_PATH = Path(os.environ.get("STATE_PATH", "/state")) / "api_key_overrides.json"


def _load_overrides() -> dict[str, str]:
    if not OVERRIDE_PATH.exists():
        return {}
    try:
        return json.loads(OVERRIDE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_overrides(overrides: dict[str, str]) -> None:
    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_PATH.write_text(json.dumps(overrides))


# ---------------------------------------------------------------------
# App registry, built from environment variables -- every app is
# opt-in via its own <NAME>_URL; nothing is assumed to exist. "servarr"
# apps share one settings-provider API shape (config/host is a flat
# object; downloadclient/indexer/applications are self-describing
# {fields: [...]} schemas) -- everything below leans on that instead of
# hand-writing a client per app.
# ---------------------------------------------------------------------
_APP_SPECS = [
    # (name, kind, api_version_or_None)
    ("sonarr", "servarr", "v3"),
    ("radarr", "servarr", "v3"),
    ("lidarr", "servarr", "v1"),
    ("prowlarr", "prowlarr", "v1"),
    ("bazarr", "bazarr", None),
    ("lazylibrarian", "lazylibrarian", None),
]


def _build_apps() -> dict[str, dict[str, Any]]:
    apps: dict[str, dict[str, Any]] = {}
    for name, kind, api in _APP_SPECS:
        prefix = name.upper()
        url = os.environ.get(f"{prefix}_URL")
        if not url:
            continue  # not configured -- this app just doesn't exist in this deployment
        entry: dict[str, Any] = {"kind": kind, "base": url.rstrip("/")}
        if api:
            entry["api"] = api
        config_path = os.environ.get(f"{prefix}_CONFIG_PATH")
        if config_path:
            entry["config"] = config_path
        apps[name] = entry
    return apps


APPS: dict[str, dict[str, Any]] = _build_apps()

# Bazarr has no self-describing schema like the servarr/Prowlarr family --
# its settings API is one big fixed-shape object, written back via
# form-encoded settings-<section>-<key> pairs (see save_settings() in
# bazarr's own app/config.py). Curated to the fields that actually tend
# to matter: its own login, and its Sonarr/Radarr connections -- both
# easy to leave unconfigured without noticing.
#
# general.use_sonarr/use_radarr are the master on/off switches -- without
# them the connection details below are stored but completely inert, which
# is an easy way to "configure" Bazarr and have it silently do nothing.
BAZARR_FIELDS = {
    "general": ["use_sonarr", "use_radarr"],
    "auth":    ["type", "username", "password"],
    "sonarr":  ["ip", "port", "apikey", "base_url", "ssl"],
    "radarr":  ["ip", "port", "apikey", "base_url", "ssl"],
}
BAZARR_SECRET_KEYS = {"password", "apikey"}

# LazyLibrarian has no bulk settings endpoint either -- one readCFG/
# writeCFG call per (group, name) pair (see its own api.py). auth_type
# is worth exposing since it's easy to end up with LazyLibrarian's own
# Forms login stacked redundantly on top of a reverse-proxy auth gate.
# "" and "FORM" are the only two values confirmed by direct observation
# of a live instance -- not guessing at a fuller enum.
LAZYLIBRARIAN_FIELDS = {
    "General": ["auth_type"],
    "QBITTORRENT": ["qbittorrent_host", "qbittorrent_port"],
}

# Host-settings fields this UI will show/edit -- deliberately not the
# full config/host object, which also carries the admin password hash.
# Nothing outside this list is ever read or written through /host.
HOST_FIELDS = ["authenticationMethod", "authenticationRequired", "port", "urlBase", "branch"]


def _read_api_key(path: str | None) -> str | None:
    if not path:
        return None  # no CONFIG_PATH given -- manual override is the only path for this app
    p = Path(path)
    if not p.exists():
        return None
    if p.suffix == ".xml":
        m = re.search(r"<ApiKey>([^<]+)</ApiKey>", p.read_text())
        return m.group(1) if m else None
    if p.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(p.read_text()) or {}
        return (data.get("auth") or {}).get("apikey") or None
    if p.suffix == ".ini":
        cp = configparser.ConfigParser()
        cp.read(p)
        for section in cp.sections():
            if cp.has_option(section, "api_key"):
                return cp.get(section, "api_key")
        # LazyLibrarian's config.ini has no [section] header on some keys
        text = p.read_text()
        m = re.search(r"^api_key\s*=\s*(\S+)", text, re.MULTILINE)
        return m.group(1) if m else None
    return None


def _get_api_key(name: str) -> str | None:
    # Lazy + self-healing, not a one-shot read at container startup: on a
    # genuinely fresh deploy (no prior human/AI to notice and restart
    # this container), each *arr app only writes its own config.xml --
    # and the ApiKey inside it -- on ITS OWN first boot, which can lag
    # this container's startup by anywhere from seconds to however long
    # a slow pull takes. Caching a startup-time None forever would mean
    # that app's editor silently 503s until someone happens to restart
    # Organizarr. Re-reading on every miss means it just starts working
    # on its own, whenever the app in question actually finishes booting
    # -- no restart, no one watching required.
    cfg = APPS[name]
    if not cfg.get("api_key"):
        cfg["api_key"] = _read_api_key(cfg.get("config"))
    if cfg.get("api_key"):
        return cfg["api_key"]
    # Auto-detection has nothing (yet). Fall back to a manually-entered
    # override rather than staying stuck -- re-checked every call, same
    # as the auto path, so if the volume gets fixed later this still
    # switches back to it automatically (see set_api_key_override()).
    return _load_overrides().get(name)


def _client(name: str) -> httpx.AsyncClient:
    key = _get_api_key(name)
    if not key:
        raise HTTPException(503, f"{name}: no API key found yet (has it finished its own first boot?)")
    headers = {"X-Api-Key": key}
    # follow_redirects handles the urlBase-prefix 307 these apps issue
    # (see docker-stack.yml history for why some carry a urlBase) --
    # httpx preserves method+body across 307/308, so PUT/POST land
    # correctly without hardcoding each app's prefix.
    return httpx.AsyncClient(base_url=APPS[name]["base"], headers=headers, follow_redirects=True, timeout=15)


def _require_kind(app_name: str, kind: str, what: str) -> None:
    # Every app is opt-in via env vars now (see _build_apps) -- routes
    # can no longer assume e.g. "sonarr" exists just because the name is
    # familiar. A clean 404 here beats a raw KeyError from deeper in.
    if APPS.get(app_name, {}).get("kind") != kind:
        raise HTTPException(404, f"{app_name}: not configured, or not a {what}")


def _require_app(app_name: str) -> None:
    if app_name not in APPS:
        raise HTTPException(404, f"{app_name}: not configured (set {app_name.upper()}_URL)")


async def _servarr_get(name: str, path: str) -> Any:
    api = APPS[name]["api"]
    async with _client(name) as c:
        r = await c.get(f"/api/{api}{path}")
        r.raise_for_status()
        return r.json()


async def _servarr_put(name: str, path: str, body: Any) -> Any:
    api = APPS[name]["api"]
    async with _client(name) as c:
        r = await c.put(f"/api/{api}{path}", json=body)
        r.raise_for_status()
        return r.json()


async def _servarr_post(name: str, path: str, body: Any) -> Any:
    api = APPS[name]["api"]
    async with _client(name) as c:
        r = await c.post(f"/api/{api}{path}", json=body)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------
# API key management -- auto-detect is always tried first (see
# _get_api_key); these exist for the apps/setups where it comes up
# empty. Never echoes a stored key back, auto-detected or manual --
# only which source is currently in effect.
# ---------------------------------------------------------------------
@app.get("/api/{app_name}/apikey")
async def get_apikey_status(app_name: str):
    if app_name not in APPS:
        raise HTTPException(404, "unknown app")
    cfg = APPS[app_name]
    if not cfg.get("api_key"):
        cfg["api_key"] = _read_api_key(cfg.get("config"))
    if cfg.get("api_key"):
        return {"source": "auto"}
    if app_name in _load_overrides():
        return {"source": "manual"}
    return {"source": "none"}


@app.post("/api/{app_name}/apikey")
async def set_apikey_override(app_name: str, body: dict[str, Any]):
    if app_name not in APPS:
        raise HTTPException(404, "unknown app")
    value = (body.get("value") or "").strip()
    if not value:
        raise HTTPException(400, "value required")
    overrides = _load_overrides()
    overrides[app_name] = value
    _save_overrides(overrides)
    return {"ok": True}


@app.delete("/api/{app_name}/apikey")
async def clear_apikey_override(app_name: str):
    if app_name not in APPS:
        raise HTTPException(404, "unknown app")
    overrides = _load_overrides()
    if overrides.pop(app_name, None) is not None:
        _save_overrides(overrides)
    return {"ok": True}


# ---------------------------------------------------------------------
# Status -- every app, reachable or not, shown on one page.
# ---------------------------------------------------------------------
@app.get("/api/status")
async def status():
    out = []
    for name, cfg in APPS.items():
        entry = {"name": name, "kind": cfg["kind"], "reachable": False, "version": None, "error": None}
        key = _get_api_key(name)
        if not key:
            entry["error"] = "no API key found yet (has it finished its own first boot?)"
            out.append(entry)
            continue
        try:
            if cfg["kind"] in ("servarr", "prowlarr"):
                data = await _servarr_get(name, "/system/status")
                entry["reachable"] = True
                entry["version"] = data.get("version")
            elif cfg["kind"] == "bazarr":
                async with _client(name) as c:
                    r = await c.get("/api/system/status")
                    r.raise_for_status()
                    entry["reachable"] = True
                    entry["version"] = r.json().get("data", {}).get("bazarr_version")
            elif cfg["kind"] == "lazylibrarian":
                async with httpx.AsyncClient(base_url=cfg["base"], timeout=15) as c:
                    r = await c.get("/api", params={"apikey": key, "cmd": "getVersion"})
                    r.raise_for_status()
                    data = r.json()
                    entry["reachable"] = bool(data.get("Success"))
                    entry["version"] = data.get("current_version")
            else:
                async with _client(name) as c:
                    r = await c.get("/")
                    # A 4xx here (not just 5xx) means something's actually
                    # wrong -- a real prior case was an app whose own
                    # misconfigured URL-prefix setting 404'd on every
                    # route, and a status_code<500 check called that
                    # "reachable" when it plainly wasn't.
                    r.raise_for_status()
                    entry["reachable"] = True
        except Exception as e:  # noqa: BLE001 -- surfacing to the UI is the point
            entry["error"] = str(e)
        out.append(entry)
    return out


# ---------------------------------------------------------------------
# Host/security settings -- Sonarr/Radarr/Lidarr only (Prowlarr has no
# forms auth to toggle).
# ---------------------------------------------------------------------
@app.get("/api/{app_name}/host")
async def get_host(app_name: str):
    _require_kind(app_name, "servarr", "servarr app with host settings")
    data = await _servarr_get(app_name, "/config/host")
    return {k: data.get(k) for k in HOST_FIELDS}


@app.post("/api/{app_name}/host")
async def set_host(app_name: str, changes: dict[str, Any]):
    _require_kind(app_name, "servarr", "servarr app with host settings")
    bad = set(changes) - set(HOST_FIELDS)
    if bad:
        raise HTTPException(400, f"not editable here: {sorted(bad)}")
    current = await _servarr_get(app_name, "/config/host")
    current.update(changes)
    return await _servarr_put(app_name, "/config/host/1", current)


# ---------------------------------------------------------------------
# Download clients -- Sonarr/Radarr/Lidarr. Passthrough of the schema
# shape (each provider type self-describes its own fields), but a
# blank password/apiKey field on save means "leave unchanged", never
# "clear it" -- these apps store real credentials here.
# ---------------------------------------------------------------------
@app.get("/api/{app_name}/downloadclients")
async def list_download_clients(app_name: str):
    _require_kind(app_name, "servarr", "servarr app")
    return await _servarr_get(app_name, "/downloadclient")


@app.put("/api/{app_name}/downloadclients/{client_id}")
async def update_download_client(app_name: str, client_id: int, body: dict[str, Any]):
    _require_kind(app_name, "servarr", "servarr app")
    current = await _servarr_get(app_name, f"/downloadclient/{client_id}")
    incoming_by_name = {f["name"]: f for f in body.get("fields", [])}
    for f in current["fields"]:
        new = incoming_by_name.get(f["name"])
        if new is None:
            continue
        if f.get("privacy") in ("password", "apiKey") and new.get("value") in (None, ""):
            continue  # blank privacy field on the wire == "unchanged"
        f["value"] = new.get("value")
    return await _servarr_put(app_name, f"/downloadclient/{client_id}", current)


# ---------------------------------------------------------------------
# Prowlarr -- applications (sync targets) and indexers.
# ---------------------------------------------------------------------
@app.get("/api/prowlarr/applications")
async def list_applications():
    _require_app("prowlarr")
    return await _servarr_get("prowlarr", "/applications")


@app.get("/api/prowlarr/applications/schema")
async def application_schema():
    _require_app("prowlarr")
    return await _servarr_get("prowlarr", "/applications/schema")


@app.post("/api/prowlarr/applications")
async def create_application(body: dict[str, Any]):
    _require_app("prowlarr")
    return await _servarr_post("prowlarr", "/applications", body)


@app.put("/api/prowlarr/applications/{app_id}")
async def update_application(app_id: int, body: dict[str, Any]):
    _require_app("prowlarr")
    return await _servarr_put("prowlarr", f"/applications/{app_id}", body)


@app.get("/api/prowlarr/indexers")
async def list_indexers():
    _require_app("prowlarr")
    return await _servarr_get("prowlarr", "/indexer")


@app.get("/api/prowlarr/indexers/schema")
async def indexer_schema():
    _require_app("prowlarr")
    return await _servarr_get("prowlarr", "/indexer/schema")


@app.post("/api/prowlarr/indexers")
async def create_indexer(body: dict[str, Any]):
    _require_app("prowlarr")
    return await _servarr_post("prowlarr", "/indexer", body)


@app.put("/api/prowlarr/indexers/{indexer_id}")
async def update_indexer(indexer_id: int, body: dict[str, Any]):
    _require_app("prowlarr")
    return await _servarr_put("prowlarr", f"/indexer/{indexer_id}", body)


# ---------------------------------------------------------------------
# Bazarr -- its own login, plus its Sonarr/Radarr connections.
# ---------------------------------------------------------------------
@app.get("/api/bazarr/settings")
async def get_bazarr_settings():
    _require_app("bazarr")
    async with _client("bazarr") as c:
        r = await c.get("/api/system/settings")
        r.raise_for_status()
        full = r.json()
    out: dict[str, dict[str, Any]] = {}
    for section, keys in BAZARR_FIELDS.items():
        section_data = full.get(section) or {}
        out[section] = {
            k: ("" if k in BAZARR_SECRET_KEYS else section_data.get(k))
            for k in keys
        }
    return out


@app.post("/api/bazarr/settings")
async def set_bazarr_settings(changes: dict[str, dict[str, Any]]):
    _require_app("bazarr")
    bad_sections = set(changes) - set(BAZARR_FIELDS)
    if bad_sections:
        raise HTTPException(400, f"not editable here: {sorted(bad_sections)}")
    form: dict[str, str] = {}
    for section, fields in changes.items():
        bad_keys = set(fields) - set(BAZARR_FIELDS[section])
        if bad_keys:
            raise HTTPException(400, f"not editable here: {section}.{sorted(bad_keys)}")
        for key, value in fields.items():
            if key in BAZARR_SECRET_KEYS and value in (None, ""):
                continue  # blank secret field on the wire == "unchanged"
            if isinstance(value, bool):
                # Bazarr's save_settings only converts the lowercase
                # strings "true"/"false" back into booleans; Python's
                # str(False) is "False", which fails its type validator.
                form[f"settings-{section}-{key}"] = "true" if value else "false"
            else:
                form[f"settings-{section}-{key}"] = str(value)
    async with _client("bazarr") as c:
        r = await c.post("/api/system/settings", data=form)
        if r.status_code not in (200, 204):
            raise HTTPException(r.status_code, r.text)
    return {"ok": True}


# ---------------------------------------------------------------------
# LazyLibrarian -- one readCFG/writeCFG call per field, no bulk endpoint.
# ---------------------------------------------------------------------
def _ll_unwrap(text: str) -> str:
    # readCFG's response is the literal bytes "[value]", not JSON.
    return text[1:-1] if text.startswith("[") and text.endswith("]") else text


@app.get("/api/lazylibrarian/settings")
async def get_lazylibrarian_settings():
    _require_app("lazylibrarian")
    key = _get_api_key("lazylibrarian")
    if not key:
        raise HTTPException(503, "lazylibrarian: no API key found yet (has it finished its own first boot?)")
    out: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(base_url=APPS["lazylibrarian"]["base"], timeout=15) as c:
        for group, names in LAZYLIBRARIAN_FIELDS.items():
            out[group] = {}
            for name in names:
                r = await c.get("/api", params={"apikey": key, "cmd": "readCFG", "name": name, "group": group})
                r.raise_for_status()
                out[group][name] = _ll_unwrap(r.text)
    return out


@app.post("/api/lazylibrarian/settings")
async def set_lazylibrarian_settings(changes: dict[str, dict[str, Any]]):
    _require_app("lazylibrarian")
    bad_groups = set(changes) - set(LAZYLIBRARIAN_FIELDS)
    if bad_groups:
        raise HTTPException(400, f"not editable here: {sorted(bad_groups)}")
    key = _get_api_key("lazylibrarian")
    if not key:
        raise HTTPException(503, "lazylibrarian: no API key found yet (has it finished its own first boot?)")
    async with httpx.AsyncClient(base_url=APPS["lazylibrarian"]["base"], timeout=15) as c:
        for group, fields in changes.items():
            bad_names = set(fields) - set(LAZYLIBRARIAN_FIELDS[group])
            if bad_names:
                raise HTTPException(400, f"not editable here: {group}.{sorted(bad_names)}")
            for name, value in fields.items():
                r = await c.get("/api", params={"apikey": key, "cmd": "writeCFG", "name": name, "group": group, "value": value})
                r.raise_for_status()
                if r.text.strip() != "OK":
                    raise HTTPException(502, f"{group}.{name}: {r.text}")
    return {"ok": True}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
