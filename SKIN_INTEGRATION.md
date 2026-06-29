# Skin integration — native OSD skip button

This add-on can drive a **skin-owned** "Skip Intro / Recap / Credits" button in
the video OSD, instead of its own modal overlay. A skin opts in with a small
capability handshake; no add-on changes or skin-name whitelist are involved.

## How it works

1. **The plugin publishes skip state.** While a segment is manually skippable
   (i.e. not auto-skipped and not a next-episode prompt), the service sets these
   properties on the **Home** window (`id 10000`):

   | Property | Example value | Meaning |
   |---|---|---|
   | `TheIntroDB.Skip.Active` | `true` | A segment is skippable right now |
   | `TheIntroDB.Skip.Label`  | `Skip Intro` | Localized button label |
   | `TheIntroDB.Skip.Type`   | `intro` | `intro` / `recap` / `credits` / `preview` |

   `TheIntroDB.Skip.Active` stays `true` from the moment the segment becomes
   skippable until it ends, playback stops or changes file, the segment is
   auto-skipped, or the user skips it. It is **not** cleared just because the
   initial few-second standalone button timed out.

2. **The skin advertises support for the current OSD.** In your `VideoOSD.xml`,
   set a transient flag while the real OSD is loaded, and clear it on unload:

   ```xml
   <onload>SetProperty(TheIntroDB.Kine.OSDButtonSupported,true,Home)</onload>
   <onunload>ClearProperty(TheIntroDB.Kine.OSDButtonSupported,Home)</onunload>
   ```

   The flag means "the **currently visible** OSD has a native TheIntroDB button",
   not merely "this skin might support it". When it is set, the plugin suppresses
   its modal `script-theintrodb-kine-skip-choice.xml` fallback and lets your
   button drive skipping.

3. **The skin shows a button bound to those properties** — visible only while a
   segment is skippable:

   ```xml
   <control type="button" id="9101">
       <visible>String.IsEqual(Window(Home).Property(TheIntroDB.Skip.Active),true)</visible>
       <label>$INFO[Window(Home).Property(TheIntroDB.Skip.Label)]</label>
       <onclick>CancelAlarm(osd_timeout,true)</onclick>
       <onclick>NotifyAll(plugin.video.tidb.kine,SkipCurrent)</onclick>
   </control>
   ```

   (Use whatever alarm name your skin uses for the OSD auto-close timeout, or
   drop that line if you do not auto-close.)

4. **The plugin skips on notification.** `NotifyAll(plugin.video.tidb.kine,
   SkipCurrent)` is received in the service's `Monitor.onNotification` and skips
   the currently active segment from the service's own bookkeeping (it already
   owns the segment bounds). The match is on the message tail `SkipCurrent`, so
   `NotifyAll(<your-skin-id>,TheIntroDB.SkipCurrent)` works equally well.

## Behavior summary

- **Segment start:** the plugin briefly shows its own standalone pill
  (`script-theintrodb-kine-skip.xml`) for a few seconds.
- **That times out:** `TheIntroDB.Skip.Active` stays `true`.
- **User opens the OSD while still inside the segment:** your button appears.
- **User clicks it:** your skin sends `NotifyAll`; the plugin skips.
- **OSD auto-closes:** the button disappears with the OSD, but `Skip.Active`
  remains until the segment actually ends.

## Fallback (skins without native support)

If `TheIntroDB.Kine.OSDButtonSupported` is **not** set when the OSD is open
mid-segment, the plugin falls back to its modal two-button
`script-theintrodb-kine-skip-choice.xml` overlay. This keeps stock skins and
older skins working unchanged.
