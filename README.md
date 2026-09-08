# Organizarr

A small config hub for the *arr apps. Not a clone of every app's settings
UI: it covers the fields that are genuinely annoying to keep straight by
hand, and the connections between apps that break quietly.

Covers Sonarr, Radarr, Lidarr, Prowlarr, Bazarr, LazyLibrarian, Cleanuparr
and Seerr. Every app is optional and enabled purely by environment
variable, so it fits a stack that runs some of them rather than all.

Two things it does that the apps cannot do for themselves:

- **Reads each app's API key from its own config**, in whatever format that
  app happens to use: XML, YAML, INI, JSON or SQLite. Keys never reach the
  browser.
- **Checks the links between apps.** Cleanuparr, Seerr, Prowlarr and Bazarr
  all need another app's URL and API key, and a wrong one fails silently:
  nothing announces the break, results just stop appearing. Organizarr is
  the only component holding all of them, so it can both report and repair
  those links.

## Why

If you run more than one or two of these, you already know the
annoyance: the same handful of settings -- is Forms auth still on after
a redeploy, does this app's download client actually point at the
right host, does Prowlarr actually know about every app -- live in five
different UIs with five different login screens (well, ideally one:
see [Security](#security) below). Organizarr puts the fields that
matter in one page, and gets out of the way for everything else.

## Quick start

The image is published to two registries from the same build, so the tags
are identical and you can use whichever you prefer:

- `ghcr.io/numbawon/organizarr:latest`
- `numbawon/organizarr:latest` (Docker Hub)

Tags available on both: `latest`, a semver tag per release, and the short
commit SHA of every build on `main`.


```yaml
# docker-compose.yml
services:
  organizarr:
    image: ghcr.io/numbawon/organizarr:latest
    ports:
      - "8000:8000"
    environment:
      SONARR_URL: http://sonarr:8989
      SONARR_CONFIG_PATH: /data/sonarr/config.xml
      RADARR_URL: http://radarr:7878
      RADARR_CONFIG_PATH: /data/radarr/config.xml
      PROWLARR_URL: http://prowlarr:9696
      PROWLARR_CONFIG_PATH: /data/prowlarr/config.xml
    volumes:
      - sonarr_config:/data/sonarr:ro
      - radarr_config:/data/radarr:ro
      - prowlarr_config:/data/prowlarr:ro
      - organizarr_data:/state
    networks:
      - your_stack_network

volumes:
  organizarr_data:

networks:
  your_stack_network:
    external: true
```

Any app you don't set a `_URL` for just doesn't show up -- no error, no
placeholder, it's as if it doesn't exist in this deployment.

## Environment variables

Every app follows the same two-variable pattern:

| Variable | Required | What it does |
|---|---|---|
| `<NAME>_URL` | Yes, to enable that app | Base URL Organizarr uses to reach the app's own API |
| `<NAME>_CONFIG_PATH` | No | Path (inside this container) to that app's config file, for automatic API key detection. Mount the app's config volume read-only at this path. |

`<NAME>` is one of `SONARR`, `RADARR`, `LIDARR`, `PROWLARR`, `BAZARR`,
`LAZYLIBRARIAN`.

If `<NAME>_CONFIG_PATH` isn't set (or the file isn't readable, or the
app hasn't finished its own first boot yet), that app's page shows a
manual API key field instead -- paste the key there and it works the
same way. Auto-detection is retried on every request, not just once at
startup, and always takes back over automatically the moment it
succeeds -- the manual entry is a fallback, not a permanent override.

`STATE_PATH` (default `/state`) -- where manually-entered API key
overrides are persisted. Mount a volume here if you want overrides to
survive a restart (recommended).

## Auto-detected key file formats

| App | Format | Where the key lives |
|---|---|---|
| Sonarr, Radarr, Lidarr, Prowlarr | `config.xml` | `<ApiKey>` element |
| Bazarr | `config.yaml` | `auth.apikey` |
| LazyLibrarian | `config.ini` | `api_key` |
| Cleanuparr | `users.db` (SQLite) | `users.api_key`, once `setup_completed` |

Cleanuparr's database is opened read-only through a URI, and deliberately
not with `immutable=1`: the app holds it open in WAL mode, so an immutable
open can hand back a snapshot from before the key was written.

## Connections

Several apps here consume the *arr apps rather than being configured like
them: Cleanuparr acts on their queues, Seerr requests into them, Prowlarr
pushes indexers to them, Bazarr pulls their libraries. Each needs the same
two facts, a reachable URL and a valid API key, and Organizarr is the only
component already holding all of them.

Getting it wrong fails quietly. Nothing announces that a link broke, only
that results stop appearing, so the read-only view is worth having even if
you never press Sync.

| Endpoint | Does |
|---|---|
| `GET /api/connections` | Read-only. What each consumer currently believes. Writes nothing. |
| `POST /api/connections/{consumer}/sync` | Corrects that consumer's links. |
| `POST /api/connections/{consumer}/sync?rewrite_keys=true` | Also pushes the current key where it cannot be verified. |

### Which keys can be checked, and which cannot

| Consumer | Key on read | Checkable |
|---|---|---|
| Cleanuparr | `••••••••` | no |
| Prowlarr | `********` (any field with `privacy: apiKey`) | no |
| Seerr | returned as stored | yes |
| Bazarr | returned as stored | yes |

Where the key is masked, a stale one is indistinguishable from a correct
one from outside, so those links are left alone unless `rewrite_keys=true`
is passed. Comparing the mask against the real key would instead mark every
link permanently wrong, which is exactly the bug this table exists to
prevent — it was written twice during development, once for Cleanuparr and
once for Prowlarr.

Where the key is readable, a wrong one shows up as an ordinary mismatch and
is corrected by a plain sync.

### What is checked besides the key

URL, and the *arr's own `urlBase`, read from its `config.xml` rather than
assumed. Seerr stores that separately from the hostname, and on the stack
this was built against it held a DOMAIN fragment (`/sonarr.example.com`)
where the app expects a path (`/sonarr`) — unreachable, and it presents as
a hostname problem.

Seerr's `PUT` also rejects the read-only extras it hands back, `id`
included, so only the accepted fields are sent. A full round-trip is a 400
with no useful message.

An entry pointing at some other URL is reported as left alone rather than
overwritten.

### Limits

`create` is not supported for Seerr: a new instance needs a quality profile
and root folder chosen inside Seerr first, so an existing instance is
corrected but a missing one is reported for you to add.

Recyclarr is deliberately absent. It has no API, only `settings.yml` and a
`configs/` directory, and every config here is mounted read-only by design.

## Security

Organizarr has **no login of its own**. It expects to sit behind a
reverse-proxy auth gate (Authentik forward-auth, Authelia, an
`nginx`/Traefik basic-auth middleware, whatever you already use) --
anyone who can reach it can view and change every configured app's
authentication settings and download-client credentials. Don't expose
it directly to the internet, and if you're running it alongside other
apps behind SSO, consider gating it more tightly than the rest (an
admins-only group, a separate provider) since its blast radius is
larger than any single app it manages.

API keys are read server-side only and never sent back to the browser,
auto-detected or manually entered.

## Development

```bash
docker build -t organizarr:local .
docker run --rm -p 8000:8000 \
  -e SONARR_URL=http://sonarr:8989 \
  --network your_stack_network \
  organizarr:local
```

No build step beyond the Dockerfile -- the frontend is a single static
HTML/JS file, no bundler.

## License

MIT -- see [LICENSE](LICENSE).
