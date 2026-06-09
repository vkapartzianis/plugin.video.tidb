# TheIntroDB – Kine Addon

<p align="center">
  <img src="https://raw.githubusercontent.com/TheIntroDB/theintrodb-assets/main/logo-banner.png">
</p>

> **Fork notice:** This is a fork of the original [TheIntroDB Kodi add-on](https://github.com/TheIntroDB/kodi-addon), rebranded as `plugin.video.tidb.kine` for bundling with **Kine** — a free, Quest-optimized Kodi fork available on the Meta Store. It works in stock Kodi too, but it ships pre-installed with Kine.

Kodi service add-on that gets intro, recap, credits, and preview segments from **[TheIntroDB](https://theintrodb.org)** for movies and TV shows and shows a skip button or auto skips for you!

**Requirements:** Kodi 19+ (or Kine). **TMDb metadata is recommended** for best accuracy. IMDb works as a fallback for supported items.

**Important:** Lookups happen when playback starts. If the current item does not expose a usable **TMDb** or **IMDb** ID, the add-on cannot match it with TheIntroDB.

**Troubleshooting (no skip button):** See the Metadata Requirements and Installation sections below.

---

## Installation

If you are running **Kine**, the add-on is already bundled — no installation needed. To install it manually in stock Kodi:

### Option A: Add-on zip only

1. Obtain the add-on zip `plugin.video.tidb.kine-<version>.zip`.
2. In Kodi, choose **Settings → Add-ons → Install from zip file**.
3. Select the zip to install the add-on directly.

### Option B: Copy into the Kodi add-ons folder

1. Unzip the add-on folder and rename it to `plugin.video.tidb.kine`, move it into Kodi’s add-ons directory for your platform.
2. Restart Kodi or reload add-ons.

---

### Metadata Requirements

**TMDB is recommended.** The add-on prefers TMDB IDs for matching. If TMDB is unavailable, it can fall back to IMDb `tt...` IDs.

For TV episodes, the playing item also needs valid **season** and **episode** numbers. If your source add-on or library does not expose provider IDs, TheIntroDB cannot match the item.

## How It Works

On each new playback, the add-on reads what Kodi exposes for the current item through JSON-RPC `Player.GetItem` and `VideoInfoTag`: **TMDb ID** when available, otherwise **IMDb** `tt...` ID, plus **season** and **episode** for TV content.

It then calls TheIntroDB, retrieves segment **start** and **end** times, waits until the segment window begins, and either shows the skip overlay or seeks automatically depending on your settings.

## Configuration

TheIntroDB Kine Addon includes a few settings to adjust behavior:

- **Auto-skip**: Skip without showing the button
- **Extra seconds after segment end**: Adds a small offset to the skip target
- **Enable lookups**: Turns TheIntroDB requests on or off
- **API key**: Lets you use your TheIntroDB API key if required
- **Debug options**: Enables verbose logging and on-screen notifications

---

## Credits

- **TheIntroDB** — database and API: [theintrodb.org](https://theintrodb.org) · [github.com/TheIntroDB](https://github.com/TheIntroDB)
- **JZOnTheGit** — original addon creator: [github.com/JZOnTheGit](https://github.com/JZOnTheGit)
- <img src="https://github.com/user-attachments/assets/8e01ae1f-9c57-499f-a513-de6fd9ea97d8" alt="TMDB" height="11"> — This product uses the TMDB API but is not endorsed or certified by TMDB.

## License

See [LICENSE](LICENSE) for details.
