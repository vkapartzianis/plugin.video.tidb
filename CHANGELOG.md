# Changelog

All notable changes to the **TheIntroDB Kine** add-on are documented here. This
project loosely follows [Keep a Changelog](https://keepachangelog.com) and
[Semantic Versioning](https://semver.org).

## [3.0.0] - 2026-06-30

### Changed
- **Segment matching now merges both databases.** Lookups query TheIntroDB *and*
  IntroDB.app and combine the results per type instead of trusting whichever
  responds first — neither is authoritative (both are user-submitted). Movies use
  TheIntroDB only (IntroDB.app is TV/IMDb-only).
- **Plausibility filter.** Candidates positioned implausibly for their type are
  rejected using the file duration — an `intro`/`recap` starting past 65% of
  runtime, or `credits`/`preview` ending before 50%. This discards mis-tagged
  entries (e.g. an "intro" sitting at the very end of the file) regardless of
  source.
- **Consensus.** When both databases agree on an overlapping window, the
  candidates are merged and the result is scored higher.

### Fixed
- Setting reads are now centralized in a shared, `onSettingsChanged`-invalidated
  cache (`tidb_settings`), so live toggles — including debug logging in the
  lookup module — apply consistently across the add-on.
- Guarded the request rate-limit globals with a lock (backported from upstream
  TheIntroDB/kodi-addon) now that lookups can issue more requests.

## [2.1.0] - 2026-06-29

### Added
- **Skip progress properties for skins.** While a segment is skippable the
  service publishes `TheIntroDB.Skip.RemainingSeconds`, `…RemainingLabel`
  (mm:ss), `…DurationSeconds`, and `…ProgressPercent` (0–100) on the Home
  window, refreshed each tick, so a skin can render a countdown or progress bar.

### Changed
- When a segment becomes skippable while the OSD is already up and the skin
  advertises native skip-button support, the standalone skip pill is no longer
  shown — the skin's button covers it.

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
