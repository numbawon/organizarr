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
    # Seerr holds Sonarr/Radarr connections of its own, so it is a
    # connection consumer like Cleanuparr rather than a settings provider.
    ("seerr", "seerr", None),
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
    if p.suffix == ".json":
        # Seerr keeps its key in its own settings file rather than a
        # dedicated config format. Read-only, and tolerant of the file
        # being mid-write: Seerr rewrites settings.json wholesale, so a
        # torn read is a normal transient rather than a fault.
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        return ((data.get("main") or {}).get("apiKey")) or None
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
    # Cache, but invalidate on the config file's mtime rather than only on
    # a miss. Caching until the key is falsy is self-healing for an app
    # that has not booted yet, and WRONG for a rotated key: the stale copy
    # gets compared against a consumer's equally stale copy, both agree,
    # and the connections view reports a broken link as fine. Observed
    # exactly that after rotating Sonarr's key. A stat per call is cheap
    # next to the HTTP calls these routes already make.
    path = cfg.get("config")
    stamp = None
    if path:
        try:
            stamp = Path(path).stat().st_mtime_ns
        except OSError:
            stamp = None
    if not cfg.get("api_key") or cfg.get("api_key_stamp") != stamp:
        fresh = _read_api_key(path)
        if fresh:
            cfg["api_key"] = fresh
            cfg["api_key_stamp"] = stamp
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
# Connections: which app holds which other app's URL and API key.
#
# Several apps here consume the *arr apps rather than being configured
# like them. Cleanuparr acts on their queues, Seerr requests into them,
# Prowlarr pushes indexers to them, Bazarr pulls their libraries. Every
# one needs the same two facts -- a reachable URL and a valid API key --
# and Organizarr is the only component already holding all of them.
#
# Getting that wrong fails silently. Seerr spent this stack's *arr move
# pointed at a hostname that no longer resolved, with nothing surfacing
# it, which is the entire argument for a read-only view that just says
# what each app currently believes.
#
# Keys never reach the browser. A link reports whether it matches, not
# what it is set to, and where the consumer masks its stored key (see
# Cleanuparr below) it reports that the answer is unknowable rather than
# guessing.
# ---------------------------------------------------------------------

# consumer -> producers it needs. Only apps actually configured here are
# ever contacted; anything absent is reported as skipped.
CONNECTIONS = {
    "cleanuparr": ["sonarr", "radarr", "lidarr"],
    "seerr": ["sonarr", "radarr"],
    "prowlarr": ["sonarr", "radarr", "lidarr"],
    "bazarr": ["sonarr", "radarr"],
}


def _producer_url(name: str) -> str:
    return APPS[name]["base"].rstrip("/")


async def _producer_version(name: str) -> float:
    try:
        info = await _servarr_get(name, "/system/status")
        return float(str(info.get("version", "0")).split(".")[0] or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _producer_url_base(name: str) -> str:
    """The *arr's own urlBase, which consumers must append.

    Seerr stores this separately from the hostname and had it set to a
    DOMAIN fragment rather than a path, which is unreachable and looks
    like a hostname problem. Read it from the app itself instead of
    assuming.
    """
    cfg = APPS.get(name, {})
    path = cfg.get("config")
    if path and Path(path).suffix == ".xml" and Path(path).exists():
        m = re.search(r"<UrlBase>([^<]*)</UrlBase>", Path(path).read_text())
        if m:
            return ("/" + m.group(1).strip("/")) if m.group(1).strip("/") else ""
    return ""


def _skeleton(consumer: str, producer: str) -> dict[str, Any] | None:
    """Shared preflight. Returns a row to short-circuit on, or None."""
    if producer not in APPS:
        return {"producer": producer, "action": "skip",
                "reason": f"{producer} not configured here"}
    if not _get_api_key(producer):
        return {"producer": producer, "action": "skip",
                "reason": f"no API key for {producer} yet"}
    return None


async def _links_cleanuparr() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    async with _client("cleanuparr") as cc:
        for producer in CONNECTIONS["cleanuparr"]:
            pre = _skeleton("cleanuparr", producer)
            if pre:
                rows.append(pre)
                continue
            row: dict[str, Any] = {"producer": producer, "action": "none",
                                   "url": _producer_url(producer)}
            try:
                r = await cc.get(f"/api/configuration/{producer}")
                if "application/json" not in r.headers.get("content-type", ""):
                    row.update(action="unsupported",
                               reason="endpoint returned HTML (not supported by this version)")
                    rows.append(row)
                    continue
                r.raise_for_status()
                cfg = r.json()
            except Exception as e:  # noqa: BLE001
                row.update(action="error", reason=str(e)[:160])
                rows.append(row)
                continue
            match = next((i for i in (cfg.get("instances") or [])
                          if (i.get("url") or "").rstrip("/") == row["url"]), None)
            if match is None:
                row["action"] = "create"
                row["other"] = [i.get("url") for i in (cfg.get("instances") or [])]
            else:
                row["instance_id"] = match.get("id")
                row["enabled"] = bool(match.get("enabled"))
                # Cleanuparr returns bullets, never the stored key, so
                # "does it still match" cannot be answered from out here.
                row["key_verifiable"] = False
                row["action"] = "enable" if not match.get("enabled") else "none"
            rows.append(row)
    return rows


async def _links_seerr() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    async with _client("seerr") as sc:
        for producer in CONNECTIONS["seerr"]:
            pre = _skeleton("seerr", producer)
            if pre:
                rows.append(pre)
                continue
            want_url = _producer_url(producer)
            host = want_url.split("//", 1)[-1].split(":")[0]
            port = int(want_url.rsplit(":", 1)[-1]) if ":" in want_url.split("//", 1)[-1] else 80
            base = _producer_url_base(producer)
            row: dict[str, Any] = {"producer": producer, "action": "none",
                                   "url": f"{host}:{port}{base}"}
            try:
                r = await sc.get(f"/api/v1/settings/{producer}")
                r.raise_for_status()
                instances = r.json()
            except Exception as e:  # noqa: BLE001
                row.update(action="error", reason=str(e)[:160])
                rows.append(row)
                continue
            if not instances:
                row["action"] = "create"
                rows.append(row)
                continue
            inst = instances[0]
            row["instance_id"] = inst.get("id")
            # Unlike Cleanuparr, Seerr hands the key back, so this one IS
            # checkable -- report it rather than pretending otherwise.
            row["key_verifiable"] = True
            wrong = []
            if inst.get("hostname") != host:
                wrong.append(f"hostname {inst.get('hostname')!r}")
            if int(inst.get("port") or 0) != port:
                wrong.append(f"port {inst.get('port')}")
            if (inst.get("baseUrl") or "") != base:
                wrong.append(f"baseUrl {inst.get('baseUrl')!r}")
            if inst.get("apiKey") != _get_api_key(producer):
                wrong.append("apiKey")
            if wrong:
                row["action"] = "update"
                row["wrong"] = wrong
            rows.append(row)
    return rows


async def _links_prowlarr() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        apps_cfg = await _servarr_get("prowlarr", "/applications")
    except Exception as e:  # noqa: BLE001
        return [{"producer": p, "action": "error", "reason": str(e)[:160]}
                for p in CONNECTIONS["prowlarr"]]
    for producer in CONNECTIONS["prowlarr"]:
        pre = _skeleton("prowlarr", producer)
        if pre:
            rows.append(pre)
            continue
        row: dict[str, Any] = {"producer": producer, "action": "none",
                               "url": _producer_url(producer)}
        match = next((a for a in apps_cfg
                      if (a.get("implementation") or "").lower() == producer), None)
        if match is None:
            row["action"] = "create"
        else:
            row["instance_id"] = match.get("id")
            fields = {f.get("name"): f.get("value") for f in (match.get("fields") or [])}
            # Prowlarr masks any field marked privacy=apiKey, handing back
            # eight asterisks rather than the stored value -- the same
            # behaviour as Cleanuparr's bullets. Comparing that to the real
            # key marks every application permanently wrong, so the key is
            # not checkable here either. Only baseUrl can be verified.
            row["key_verifiable"] = False
            wrong = []
            if (fields.get("baseUrl") or "").rstrip("/") != row["url"]:
                wrong.append(f"baseUrl {fields.get('baseUrl')!r}")
            if wrong:
                row["action"] = "update"
                row["wrong"] = wrong
        rows.append(row)
    return rows


async def _links_bazarr() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    key = _get_api_key("bazarr")
    if not key:
        return [{"producer": p, "action": "error", "reason": "no bazarr API key"}
                for p in CONNECTIONS["bazarr"]]
    async with httpx.AsyncClient(base_url=APPS["bazarr"]["base"],
                                 headers={"X-API-KEY": key}, timeout=15,
                                 follow_redirects=True) as bc:
        try:
            r = await bc.get("/api/system/settings")
            r.raise_for_status()
            settings = r.json()
        except Exception as e:  # noqa: BLE001
            return [{"producer": p, "action": "error", "reason": str(e)[:160]}
                    for p in CONNECTIONS["bazarr"]]
    for producer in CONNECTIONS["bazarr"]:
        pre = _skeleton("bazarr", producer)
        if pre:
            rows.append(pre)
            continue
        want_url = _producer_url(producer)
        host = want_url.split("//", 1)[-1].split(":")[0]
        port = int(want_url.rsplit(":", 1)[-1]) if ":" in want_url.split("//", 1)[-1] else 80
        base = _producer_url_base(producer)
        row: dict[str, Any] = {"producer": producer, "action": "none",
                               "url": f"{host}:{port}{base}"}
        sec = settings.get(producer) or {}
        general = settings.get("general") or {}
        row["key_verifiable"] = True
        wrong = []
        if not general.get(f"use_{producer}"):
            wrong.append(f"use_{producer} is off")
        if sec.get("ip") != host:
            wrong.append(f"ip {sec.get('ip')!r}")
        if int(sec.get("port") or 0) != port:
            wrong.append(f"port {sec.get('port')}")
        if (sec.get("base_url") or "") != base:
            wrong.append(f"base_url {sec.get('base_url')!r}")
        if sec.get("apikey") != _get_api_key(producer):
            wrong.append("apikey")
        if wrong:
            row["action"] = "update"
            row["wrong"] = wrong
        rows.append(row)
    return rows


_LINK_READERS = {
    "cleanuparr": _links_cleanuparr,
    "seerr": _links_seerr,
    "prowlarr": _links_prowlarr,
    "bazarr": _links_bazarr,
}


@app.get("/api/connections")
async def list_connections():
    """Read-only. What each consumer currently believes about the *arrs."""
    out = []
    for consumer, reader in _LINK_READERS.items():
        if consumer not in APPS:
            continue
        try:
            links = await reader()
        except HTTPException as e:
            links = [{"producer": p, "action": "error", "reason": e.detail}
                     for p in CONNECTIONS[consumer]]
        except Exception as e:  # noqa: BLE001
            links = [{"producer": p, "action": "error", "reason": str(e)[:160]}
                     for p in CONNECTIONS[consumer]]
        out.append({"consumer": consumer, "links": links})
    return {"connections": out}


async def _sync_cleanuparr(rows: list[dict[str, Any]], rewrite: bool) -> list[dict[str, Any]]:
    results = []
    async with _client("cleanuparr") as cc:
        for row in rows:
            producer, action = row["producer"], row["action"]
            if rewrite and action == "none" and row.get("instance_id"):
                action = "update"
            if action in ("none", "skip", "unsupported", "error"):
                results.append({"producer": producer, "action": action,
                                "ok": action == "none", "reason": row.get("reason")})
                continue
            body = {"enabled": True, "name": producer.capitalize(), "url": row["url"],
                    "apiKey": _get_api_key(producer),
                    "version": await _producer_version(producer)}
            try:
                if action == "create":
                    r = await cc.post(f"/api/configuration/{producer}/instances", json=body)
                else:
                    r = await cc.put(
                        f"/api/configuration/{producer}/instances/{row['instance_id']}", json=body)
                r.raise_for_status()
                results.append({"producer": producer, "action": action, "ok": True})
            except Exception as e:  # noqa: BLE001
                results.append({"producer": producer, "action": action, "ok": False,
                                "reason": str(e)[:200]})
    return results


# Seerr rejects a PUT that carries the read-only extras it hands back --
# notably `id`, which is in the path already. Send only the fields it
# actually accepts, or every save is a 400 that says nothing useful.
_SEERR_PUT_FIELDS = (
    "name", "hostname", "port", "apiKey", "useSsl", "baseUrl", "activeProfileId",
    "activeProfileName", "activeDirectory", "is4k", "isDefault", "syncEnabled",
    "preventSearch", "tagRequests", "tags",
)
_SEERR_PUT_EXTRA = {"sonarr": ("enableSeasonFolders", "animeTags", "monitorNewItems"),
                    "radarr": ("minimumAvailability",)}


async def _sync_seerr(rows: list[dict[str, Any]], rewrite: bool) -> list[dict[str, Any]]:
    results = []
    async with _client("seerr") as sc:
        for row in rows:
            producer, action = row["producer"], row["action"]
            if action in ("none", "skip", "unsupported", "error"):
                results.append({"producer": producer, "action": action,
                                "ok": action == "none", "reason": row.get("reason")})
                continue
            if action == "create":
                results.append({"producer": producer, "action": action, "ok": False,
                                "reason": "no instance to correct; add it in Seerr first "
                                          "(it needs a quality profile and root folder "
                                          "chosen there)"})
                continue
            want_url = _producer_url(producer)
            host = want_url.split("//", 1)[-1].split(":")[0]
            port = int(want_url.rsplit(":", 1)[-1]) if ":" in want_url.split("//", 1)[-1] else 80
            try:
                r = await sc.get(f"/api/v1/settings/{producer}")
                r.raise_for_status()
                inst = next(i for i in r.json() if i.get("id") == row["instance_id"])
                allowed = _SEERR_PUT_FIELDS + _SEERR_PUT_EXTRA.get(producer, ())
                body = {k: inst[k] for k in allowed if k in inst}
                body.update(hostname=host, port=port,
                            baseUrl=_producer_url_base(producer),
                            apiKey=_get_api_key(producer))
                r = await sc.put(f"/api/v1/settings/{producer}/{row['instance_id']}", json=body)
                r.raise_for_status()
                results.append({"producer": producer, "action": action, "ok": True})
            except Exception as e:  # noqa: BLE001
                results.append({"producer": producer, "action": action, "ok": False,
                                "reason": str(e)[:200]})
    return results


async def _sync_prowlarr(rows: list[dict[str, Any]], rewrite: bool) -> list[dict[str, Any]]:
    results = []
    for row in rows:
        producer, action = row["producer"], row["action"]
        if rewrite and action == "none" and row.get("instance_id"):
            action = "update"
        if action in ("none", "skip", "unsupported", "error"):
            results.append({"producer": producer, "action": action,
                            "ok": action == "none", "reason": row.get("reason")})
            continue
        try:
            if action == "create":
                schema = await _servarr_get("prowlarr", "/applications/schema")
                tmpl = next(x for x in schema
                            if (x.get("implementation") or "").lower() == producer)
                body = dict(tmpl)
                body["name"] = producer.capitalize()
                for f in body.get("fields", []):
                    if f.get("name") == "baseUrl":
                        f["value"] = _producer_url(producer)
                    elif f.get("name") == "apiKey":
                        f["value"] = _get_api_key(producer)
                    elif f.get("name") == "prowlarrUrl":
                        f["value"] = APPS["prowlarr"]["base"].rstrip("/")
                await _servarr_post("prowlarr", "/applications", body)
            else:
                apps_cfg = await _servarr_get("prowlarr", "/applications")
                body = next(a for a in apps_cfg if a.get("id") == row["instance_id"])
                for f in body.get("fields", []):
                    if f.get("name") == "baseUrl":
                        f["value"] = _producer_url(producer)
                    elif f.get("name") == "apiKey":
                        f["value"] = _get_api_key(producer)
                await _servarr_put("prowlarr", f"/applications/{row['instance_id']}", body)
            results.append({"producer": producer, "action": action, "ok": True})
        except Exception as e:  # noqa: BLE001
            results.append({"producer": producer, "action": action, "ok": False,
                            "reason": str(e)[:200]})
    return results


async def _sync_bazarr(rows: list[dict[str, Any]], rewrite: bool) -> list[dict[str, Any]]:
    results = []
    key = _get_api_key("bazarr")
    async with httpx.AsyncClient(base_url=APPS["bazarr"]["base"],
                                 headers={"X-API-KEY": key}, timeout=15,
                                 follow_redirects=True) as bc:
        for row in rows:
            producer, action = row["producer"], row["action"]
            if action in ("none", "skip", "unsupported", "error"):
                results.append({"producer": producer, "action": action,
                                "ok": action == "none", "reason": row.get("reason")})
                continue
            want_url = _producer_url(producer)
            host = want_url.split("//", 1)[-1].split(":")[0]
            port = int(want_url.rsplit(":", 1)[-1]) if ":" in want_url.split("//", 1)[-1] else 80
            # Bazarr saves via form-encoded settings-<section>-<key> pairs,
            # the same shape set_bazarr_settings already uses.
            form = {
                f"settings-general-use_{producer}": "true",
                f"settings-{producer}-ip": host,
                f"settings-{producer}-port": str(port),
                f"settings-{producer}-base_url": _producer_url_base(producer),
                f"settings-{producer}-apikey": _get_api_key(producer),
                f"settings-{producer}-ssl": "false",
            }
            try:
                r = await bc.post("/api/system/settings", data=form)
                r.raise_for_status()
                results.append({"producer": producer, "action": action, "ok": True})
            except Exception as e:  # noqa: BLE001
                results.append({"producer": producer, "action": action, "ok": False,
                                "reason": str(e)[:200]})
    return results


_LINK_WRITERS = {
    "cleanuparr": _sync_cleanuparr,
    "seerr": _sync_seerr,
    "prowlarr": _sync_prowlarr,
    "bazarr": _sync_bazarr,
}


@app.post("/api/connections/{consumer}/sync")
async def sync_connections(consumer: str, rewrite_keys: bool = False):
    """Correct one consumer's links to the *arr apps.

    rewrite_keys matters for consumers that mask their stored key on read:
    Cleanuparr returns bullets, Prowlarr returns asterisks for any field
    marked privacy=apiKey. For those, a stale key is indistinguishable from
    a correct one from out here, so an otherwise-fine link is left alone
    unless this is set. Seerr and Bazarr hand the key back, so a wrong one
    shows up as an ordinary mismatch and is corrected without it.
    """
    if consumer not in _LINK_WRITERS:
        raise HTTPException(404, f"{consumer}: not a connection consumer")
    _require_app(consumer)
    rows = await _LINK_READERS[consumer]()
    return {"results": await _LINK_WRITERS[consumer](rows, rewrite_keys)}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
