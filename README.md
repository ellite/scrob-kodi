# scrob-kodi

Kodi repository and add-on for [Scrob](https://github.com/ellite/scrob) — the self-hosted scrobbling service for movies and TV shows.

## What's included

| Directory | ID | Purpose |
|---|---|---|
| `service.scrob` | `service.scrob` | Kodi service add-on that scrobbles playback events to your Scrob instance |
| `repository.scrob` | `repository.scrob` | Kodi repository add-on that lets Kodi install and update `service.scrob` automatically |

## Installation

### Step 1 — Install the Scrob Repository

1. In Kodi, go to **Settings → System → Add-ons** and enable **Unknown sources**.
2. Download the latest `repository.scrob-*.zip` from the [Releases](https://github.com/ellite/scrob-kodi/releases) page.
3. Go to **Add-ons → Install from zip file** and select the downloaded zip.
4. Kodi will confirm that the Scrob Repository has been installed.

### Step 2 — Install the Scrob add-on

1. Go to **Add-ons → Install from repository → Scrob Repository → Services → Scrob**.
2. Click **Install**.

### Step 3 — Configure the add-on

1. Open the add-on settings (**Add-ons → My add-ons → Services → Scrob → Configure**).
2. Set the following:

### Connection

| Setting | Description | Default |
|---|---|---|
| **Scrob URL** | Base URL of your Scrob instance | `http://localhost:7330` |
| **API Key** | API key from your Scrob instance | _(empty)_ |
| **Progress interval (seconds)** | How often playback progress is sent | `60` |

### Sync

| Setting | Description | Default |
|---|---|---|
| **Send events to Scrob** | Scrobble playback events (play/pause/resume/stop) to Scrob | on |
| **Mark watched at (% of runtime)** | Playback past this percentage counts as a completed watch, even if you stop before the very end | `90` |
| **Keep Kodi library in sync from Scrob** | On startup, raise local play counts to match Scrob | on |
| **Also sync ratings from Scrob** | Pull Scrob ratings into the library as `userrating` (only fills titles that have no local rating) | on |
| **Re-sync from Scrob every (minutes)** | Repeat the pull on this interval so several Kodi boxes converge; `0` = startup only | `0` |
| **Rate movies / episodes after watching** | Show a 1–10 star popup when a title finishes | off |

3. Restart Kodi (or the add-on) for the settings to take effect.

## How it works

`service.scrob` runs as a background service in Kodi and listens for player events (play, pause, resume, seek, stop). When a movie or episode starts playing, it reads the metadata (title, year, TMDB/TVDB/IMDB IDs) and sends it to your Scrob instance via the webhook API. Progress updates are sent at the configured interval so Scrob can track your position in real time.

A title is reported as a completed watch when playback passes the **Mark watched at** percentage, when it plays to the very end, or when you mark it watched from the Kodi menu (Kodi's own `VideoLibrary.OnUpdate` event).

When **Keep Kodi library in sync from Scrob** is enabled, the add-on pulls your watch counts (and, optionally, ratings) from Scrob on startup — and, if **Re-sync from Scrob every** is set, on that interval — and writes them into the local library so watched state carries across multiple Kodi instances. Local values are only ever raised, never lowered, and a local rating is never overwritten.

Only `movie` and `episode` media types are scrobbled; music and other content is ignored.

## Repository URLs

If you prefer to add the repository manually in Kodi rather than installing from a zip:

| File | URL |
|---|---|
| `addons.xml` | `https://ellite.github.io/scrob-kodi/addons.xml` |
| `addons.xml.md5` | `https://ellite.github.io/scrob-kodi/addons.xml.md5` |
| Data directory | `https://ellite.github.io/scrob-kodi/` |

## License

GPL-3.0-only — see [LICENSE](LICENSE) for details.
