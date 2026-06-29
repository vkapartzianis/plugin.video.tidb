# Changelog

All notable changes to the **TheIntroDB Kine** add-on are documented here. This
project loosely follows [Keep a Changelog](https://keepachangelog.com) and
[Semantic Versioning](https://semver.org).

## [2.0.0] - 2026-06-29

### Added
- **Native in-OSD skip button for skins.** The service now publishes the active
  skippable segment on the Home window (`TheIntroDB.Skip.Active` / `.Label` /
  `.Type`); a skin advertises support via
  `TheIntroDB.Kine.OSDButtonSupported` and skips by sending
  `NotifyAll(plugin.video.tidb.kine, SkipCurrent)`. See `SKIN_INTEGRATION.md`.
- **"End credits action" setting** — choose whether skipping a credits/preview
  segment seeks to the end (default) or plays the next episode.
- **TMDb → IMDb resolution** for the IntroDB.app fallback, so TV episodes that
  only expose a TMDb id can still be matched. Adds the required TMDB attribution
  to the add-on metadata and README.

### Changed
- Settings converted to Kodi's modern (`version="1"`) schema, removing the
  repeated "trying to load setting definitions from old format" log warnings;
  the service now caches setting reads instead of re-reading every second.
- Overlay skin files renamed to overridable `script-theintrodb-kine-*.xml`
  templates (`-skip`, `-skip-choice`, `-overlay`) so skins can restyle them.
- End-of-media segments with no end time now end **2 seconds** before the media
  ends (was 10), and the end-of-media skip lands there.

### Privacy
- Anonymous usage analytics is now **opt-in and off by default**. The analytics
  engine is not imported, started, or given a state file unless you opt in, and
  toggling the setting starts/stops it live.

## [1.6.0]

- Added per-segment automatic skipping toggles. Fixed translations in settings.
