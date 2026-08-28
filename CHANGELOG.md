# Changelog

## 1.2.0

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
