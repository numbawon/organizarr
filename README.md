# Organizarr

One place for the `*arr` settings that are actually annoying to keep
consistent by hand: authentication method, download-client host/port,
and Prowlarr's application/indexer sync. Not a clone of every app's
full settings UI -- just the fields worth centralizing, edited through
each app's own real API, never by touching a config file directly.

Supports Sonarr, Radarr, Lidarr, Prowlarr, Bazarr, and LazyLibrarian.
Every app is optional -- turn one on by setting its `_URL` env var, skip
the rest.

![Organizarr's status grid and Sonarr's settings expanded, live data from a real deployment](docs/screenshot.png)

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
