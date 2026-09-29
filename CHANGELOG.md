# Changelog

## 1.3.0

- New rating popup (#5): landscape and Scrob-branded, with the title's poster and fanart from your Kodi library, a row of ten stars that fill up to your selection and the number shown large underneath, replacing the plain 1 to 10 list. Left/Right to choose, OK to rate, Back to skip
- New **Default rating in the rating popup** setting (1 to 10, default 5) so the popup opens on a sensible value and a remote only needs OK
- Fix: syncing from Scrob could loop forever, re-scrobbling everything as new plays under one date. Kodi reports the playcounts the add-on writes itself as "marked as watched", the time-based mute could expire before those notifications arrived, and every echo added a play in Scrob that pushed the next sync's count higher. The add-on now recognises its own writes by item, only fills titles Kodi has as unwatched (never raises an existing count), and stops scrobbling mark-as-watched for 10 minutes if more than 30 arrive within a minute (a library scan or sync, not a person)
- Library mark-as-watched scrobbles are now flagged so Scrob ignores them for titles it already has a play for

## 1.2.3

- Fix: the **Status** line was not rendering in the settings screen (Kodi settings format has no `label` control type; updated to use a disabled `edit` control)
- Fix: the **API Key** field was missing from the settings screen entirely — Kodi silently drops a `type="string"` setting that has an empty default and isn't marked `<allowempty>`. Regressed in 1.2.0 when the placeholder default was removed

## 1.2.2

- Fix: all add-on settings are now visible at the **Basic** settings level — the Scrob URL, API Key and Sync options were only shown at Standard or higher
- Fix: the **Status** line renders on Kodi versions that rejected the previous `label` control definition

## 1.2.1

- Connection settings show a read-only **Status** line — whether the add-on is authorized with Scrob, using an API key, or not connected

## 1.2.0

- **Authorize with Scrob**: link the add-on with an OAuth device code (open `your-scrob-url/link`, sign in, approve) instead of pasting an API key. Works with 2FA accounts, revocable per-device from Connections → Connected Apps. The API key still works and stays as a fallback
- Mark a title watched once playback passes a configurable percentage of the runtime (**Mark watched at**, default 90%), instead of only when it plays to the very end (#2)
- Scrobble titles that are marked as watched from the Kodi menu, or auto-marked watched by Kodi near the end of playback (#2)
- **Also sync ratings from Scrob**: pull your Scrob ratings into the Kodi library as `userrating` (only fills titles with no local rating) (#1)
- **Re-sync from Scrob every N minutes**: optional periodic pull so several Kodi boxes sharing one Scrob account stay in sync, not just at startup (#3)

## 1.1.3

- Fix episodes and movies not marked as watched after resume-from-stop

## 1.1.2

- Fix scrobbling not working for TV episodes (switch to JSON-RPC Player.GetItem)

## 1.1.1

- Fix scrobbling not working for TV episodes (retry reading metadata after OnPlay)

## 1.1.0

- New **Connection** settings section (renamed from General)
- New **Sync** settings section:
  - Toggle scrobbling playback events to Scrob (default on)
  - Toggle library sync from Scrob on startup (default on)
  - Rate movies after watching with a 1–10 star popup (default off)
  - Rate episodes after watching with a 1–10 star popup (default off)

## 1.0.0

- Initial release
- Scrobble playback events to Scrob (play, pause, resume, stop, progress)
