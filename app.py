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
import sqlite3
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
    # Cleanuparr does not share the *arr settings shape, so it keeps its
    # own kind rather than joining the servarr branch. It is no longer
    # reachability-only though: it CONSUMES the other apps' URLs and API
    # keys, and this container is the only thing holding all of them, so
    # it also gets the arr-wiring routes at the bottom of this file.
    ("cleanuparr", "cleanuparr", None),
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
    if p.suffix == ".db":
        # Cleanuparr keeps its API key in SQLite rather than a text config,
        # on the user row, and only once the first-run wizard is done.
        #
        # Opened read-only through a URI, and NOT with immutable=1: the app
        # holds this database open in WAL mode, so an immutable open can
        # return a stale snapshot that predates the key being written.
        # mode=ro can itself fail on a WAL database when SQLite cannot
        # create the -shm sidecar next to a read-only mount, hence the
        # fallback rather than a single attempt.
        for uri in (f"file:{p}?mode=ro", f"file:{p}?immutable=1"):
            try:
                con = sqlite3.connect(uri, uri=True, timeout=5)
                try:
                    row = con.execute(
                        "select api_key from users "
                        "where api_key is not null and api_key != '' "
                        "and setup_completed = 1 limit 1"
                    ).fetchone()
                finally:
                    con.close()
                if row and row[0]:
                    return row[0]
            except sqlite3.Error:
                continue
        return None
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
    """Per-app health, with enough detail for the UI to hide what is not
    actually usable.

    `detected` is the field the UI filters on. An app is detected when it
    answered AND (for the kinds that need one) a working API key was
    found. Anything else is configured-but-not-working: the URL env var
    is set, so someone intended it to exist, but the config volume is not
    mounted, the app has not finished its first boot, or it is down.

    Showing those by default made the page misleading -- a section full
    of controls that 503 on every click looks broken rather than absent.
    They are hidden instead, behind a toggle, with the reason attached.
    """
    out = []
    for name, cfg in APPS.items():
        config_path = cfg.get("config")
        entry = {
            "name": name,
            "kind": cfg["kind"],
            "reachable": False,
            "version": None,
            "error": None,
            "detected": False,
            # Why an app might not be usable, so the UI can say something
            # more useful than "unreachable".
            "config_path": config_path,
            "config_mounted": bool(config_path and Path(config_path).exists()),
            "api_key": False,
            "api_key_source": None,
        }

        # Cleanuparr is reachability-only here: its settings API is real
        # and writable, but every route on it is JWT-gated and we hold no
        # token, so there is nothing to render as editable.
        #
        # The probe is /api/auth/status, NOT /health. Cleanuparr serves an
        # Angular SPA and its static handler answers 200 with index.html
        # for any unrecognised path, so /health "succeeded" whether or not
        # the backend worked -- a locked database or a failed migration
        # still returned 200 and a green tick. /api/auth/status is a real
        # endpoint, is deliberately unauthenticated so the login page can
        # decide what to render, and returns JSON. Requiring valid JSON
        # back is what makes this prove the backend rather than the file
        # server.
        if cfg["kind"] == "cleanuparr":
            try:
                async with httpx.AsyncClient(base_url=cfg["base"], timeout=10) as c:
                    r = await c.get("/api/auth/status", follow_redirects=True)
                    r.raise_for_status()
                    status = r.json()  # SPA fallback is HTML and raises here
                    if not isinstance(status, dict) or "setupCompleted" not in status:
                        raise ValueError("unexpected payload from /api/auth/status")
                    entry["reachable"] = True
                    entry["detected"] = True
                    # Surface the bits worth seeing at a glance. setup_completed
                    # false means it is sitting on its first-run wizard, which
                    # looks identical to "working" from the outside otherwise.
                    entry["cleanuparr"] = {
                        "setup_completed": bool(status.get("setupCompleted")),
                        "oidc_enabled": bool(status.get("oidcEnabled")),
                        "oidc_provider": status.get("oidcProviderName") or None,
                        "oidc_exclusive": bool(status.get("oidcExclusiveMode")),
                        "auth_bypass": bool(status.get("authBypassActive")),
                    }
                    if not status.get("setupCompleted"):
                        entry["error"] = "reachable, but still on first-run setup"
            except Exception as e:  # noqa: BLE001
                entry["error"] = str(e)
            # Cleanuparr DOES have an API key, unlike when this block only
            # probed reachability: it lives in users.db and gates
            # /api/configuration/*, which is what the arr-wiring routes
            # below use. Report it the same way every other app does so a
            # missing key is visible here rather than as a 503 later.
            if _get_api_key(name):
                entry["api_key"] = True
                entry["api_key_source"] = (
                    "config" if _read_api_key(config_path) else "override"
                )
            elif config_path and not entry["config_mounted"]:
                entry.setdefault("error", f"config file not found at {config_path} (volume not mounted?)")
            out.append(entry)
            continue

        key = _get_api_key(name)
        if key:
            entry["api_key"] = True
            # Auto-detection wins over an override (see _get_api_key), so
            # if the config file yields a key that is the live source.
            entry["api_key_source"] = (
                "config" if _read_api_key(config_path) else "override"
            )
        if not key:
            if config_path and not entry["config_mounted"]:
                entry["error"] = f"config file not found at {config_path} (volume not mounted?)"
            elif config_path:
                entry["error"] = "config file mounted but has no API key yet (still on first boot?)"
            else:
                entry["error"] = "no CONFIG_PATH set and no manual API key override"
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
        # Usable means it answered AND we hold a key we can act with.
        entry["detected"] = bool(entry["reachable"] and entry["api_key"])
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



# ---------------------------------------------------------------------
# Cleanuparr arr wiring.
#
# Cleanuparr needs every *arr's URL and API key to act on their queues,
# and on a fresh deploy that is a pile of copy-paste out of config files
# that this container has already read. It is the one component holding
# all of them, so wiring them together belongs here.
#
# Its API is the same shape as the servarr family -- X-Api-Key, JSON --
# so _client() works unchanged. Its own key lives in users.db, which is
# why _read_api_key grew a SQLite branch.
#
# The keys never reach the browser. The diff below reports whether each
# instance matches, not what it is set to, matching how /api/status
# reports api_key as a bool.
# ---------------------------------------------------------------------

# organizarr app name -> Cleanuparr's endpoint segment. Deliberately not
# every app Cleanuparr supports upstream: readarr/whisparr/sportarr are
# in its controller but not in this stack, and lazylibrarian is in its
# controller yet answered with the SPA fallback on the deployed version,
# so support is probed per app rather than assumed from the source.
CLEANUPARR_ARR_MAP = {"sonarr": "sonarr", "radarr": "radarr", "lidarr": "lidarr"}


async def _arr_major_version(name: str) -> float:
    """Cleanuparr wants a numeric major version on each instance."""
    try:
        info = await _servarr_get(name, "/system/status")
        return float(str(info.get("version", "0")).split(".")[0] or 0)
    except Exception:  # noqa: BLE001
        return 0.0


async def _cleanuparr_arr_state() -> list[dict[str, Any]]:
    """What Cleanuparr holds vs what this container knows, per app."""
    _require_kind("cleanuparr", "cleanuparr", "cleanuparr instance")
    rows: list[dict[str, Any]] = []
    async with _client("cleanuparr") as cc:
        for name, segment in CLEANUPARR_ARR_MAP.items():
            row: dict[str, Any] = {"app": name, "action": "none"}
            if name not in APPS:
                row["action"] = "skip"
                row["reason"] = f"{name} not configured here"
                rows.append(row)
                continue
            want_key = _get_api_key(name)
            if not want_key:
                row["action"] = "skip"
                row["reason"] = f"no API key for {name} yet"
                rows.append(row)
                continue
            want_url = APPS[name]["base"]
            row["url"] = want_url
            try:
                r = await cc.get(f"/api/configuration/{segment}")
                if "application/json" not in r.headers.get("content-type", ""):
                    # Cleanuparr serves its SPA for unknown paths, so a 200
                    # of HTML means this version has no such endpoint.
                    row["action"] = "unsupported"
                    row["reason"] = "endpoint returned HTML (not supported by this version)"
                    rows.append(row)
                    continue
                r.raise_for_status()
                cfg = r.json()
            except Exception as e:  # noqa: BLE001
                row["action"] = "error"
                row["reason"] = str(e)[:160]
                rows.append(row)
                continue

            row["config_id"] = cfg.get("id")
            existing = [i for i in (cfg.get("instances") or [])
                        if (i.get("url") or "").rstrip("/") == want_url.rstrip("/")]
            match = existing[0] if existing else None
            if match is None:
                row["action"] = "create"
                # An instance on a DIFFERENT url is not ours to touch; report
                # it so a stale entry is visible rather than silently ignored.
                row["other_instances"] = [i.get("url") for i in (cfg.get("instances") or [])]
            else:
                row["instance_id"] = match.get("id")
                row["enabled"] = bool(match.get("enabled"))
                # Cleanuparr masks the key on read -- GET returns bullets,
                # never the stored value -- so whether it still matches the
                # *arr's real key CANNOT be determined from here. Saying
                # "matches" either way would be a guess, and comparing the
                # mask to the real key marks every instance stale forever.
                # Report it as unverifiable and let the caller decide.
                row["key_verifiable"] = False
                row["action"] = "enable" if not match.get("enabled") else "none"
            rows.append(row)
    return rows


@app.get("/api/cleanuparr/arr")
async def cleanuparr_arr_status():
    """Read-only: what would change if you synced. Writes nothing."""
    return {"apps": await _cleanuparr_arr_state()}


@app.post("/api/cleanuparr/arr/sync")
async def cleanuparr_arr_sync(rewrite_keys: bool = False):
    """Create or correct Cleanuparr's *arr instances from what we know.

    Creates anything missing and re-enables anything disabled. Because the
    stored API key cannot be read back (see _cleanuparr_arr_state), an
    existing, enabled instance is left alone by default. Pass
    rewrite_keys=true to push the current key over it anyway, which is the
    fix after rotating an *arr's key -- the only case where the stored value
    is known to be wrong.
    """
    state = await _cleanuparr_arr_state()
    if rewrite_keys:
        for row in state:
            if row["action"] == "none" and row.get("instance_id"):
                row["action"] = "update"
    results: list[dict[str, Any]] = []
    async with _client("cleanuparr") as cc:
        for row in state:
            action, name = row["action"], row["app"]
            if action in ("none", "skip", "unsupported", "error"):
                results.append({"app": name, "action": action,
                                "reason": row.get("reason"), "ok": action == "none"})
                continue
            segment = CLEANUPARR_ARR_MAP[name]
            body = {
                "enabled": True,
                "name": name.capitalize(),
                "url": row["url"],
                "apiKey": _get_api_key(name),
                "version": await _arr_major_version(name),
            }
            try:
                if action == "create":
                    r = await cc.post(f"/api/configuration/{segment}/instances", json=body)
                else:  # "enable" or "update" -- both are a full PUT of the instance
                    r = await cc.put(
                        f"/api/configuration/{segment}/instances/{row['instance_id']}", json=body
                    )
                r.raise_for_status()
                results.append({"app": name, "action": action, "ok": True})
            except Exception as e:  # noqa: BLE001
                results.append({"app": name, "action": action, "ok": False,
                                "reason": str(e)[:200]})
    return {"results": results}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
