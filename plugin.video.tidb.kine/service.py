# kodi service entry: poll playback, query theintrodb, show skip ui or auto-seek
import threading

import xbmc
import xbmcaddon
import xbmcgui
from typing import List, Dict, Optional, Any, Tuple

from player import TIDBPlayer
import skipper
import overlay as overlay_mod
import submit_overlay
import introdb
# aptabase_analytics is imported lazily, only when the user has opted in — see
# _reconcile_reporter — so nothing analytics-related loads otherwise.

ADDON = xbmcaddon.Addon()
_ADDON_ID = ADDON.getAddonInfo('id')
ADDON_NAME = ADDON.getAddonInfo('name')
REPORTER = None

# String IDs for localization
STR_SKIP_INTRO = 32001
STR_SKIP_RECAP = 32003
STR_SKIP_CREDITS = 32004
STR_SKIP_PREVIEW = 32005
STR_MARK_START = 32020
STR_MARK_END = 32021
STR_SUBMIT_SUCCESS = 32022
STR_SUBMIT_FAILED = 32023

SUBMIT_WINDOW_SECS = 300.0  # first 5 minutes


# ── Settings cache ────────────────────────────────────────────────────────
# Reading a setting used to build a fresh xbmcaddon.Addon() on every call
# (once per second in the main loop) so GUI edits applied without a restart.
# Instead we cache values in memory and clear the cache when Kodi notifies us
# of a change, so edits still apply live but the hot path stays cheap.

_settings_cache = {}  # type: Dict[str, str]


def _cached_setting(key: str) -> str:
    try:
        return _settings_cache[key]
    except KeyError:
        pass
    try:
        value = xbmcaddon.Addon(_ADDON_ID).getSetting(key)
    except Exception:
        value = ADDON.getSetting(key)
    _settings_cache[key] = value
    return value


def _invalidate_settings_cache() -> None:
    _settings_cache.clear()


class TIDBMonitor(xbmc.Monitor):
    def onSettingsChanged(self) -> None:
        # Kodi has already persisted the new values; drop the cache so the next
        # read re-fetches them. Fires on a separate thread from the main loop.
        _invalidate_settings_cache()
        # Start/stop analytics live when the opt-in toggle changes.
        _reconcile_reporter()

    def onNotification(self, sender: str, method: str, data: str) -> None:
        # A skin's native OSD "Skip" button calls NotifyAll(plugin.video.tidb.kine,
        # SkipCurrent). Match the message tail so TheIntroDB.SkipCurrent works too.
        # Fires on a separate thread from the main loop.
        try:
            tail = (method or '').rsplit('.', 1)[-1].lower()
        except Exception:
            tail = ''
        if tail == SKIP_NOTIFY_MESSAGE.lower():
            _skip_active_segment()


# ── Analytics lifecycle ───────────────────────────────────────────────────
# The analytics engine is only instantiated while the user has opted in, so
# nothing loads, spawns a thread, or touches the network unless reporting is
# explicitly enabled. We reconcile at startup and whenever settings change.

_reporter_lock = threading.Lock()


def _analytics_opted_in() -> bool:
    return _cached_setting('anonymous_usage_reporting_kine') == 'true'


def _reconcile_reporter() -> None:
    global REPORTER
    if _analytics_opted_in():
        with _reporter_lock:
            if REPORTER is None:
                import aptabase_analytics
                REPORTER = aptabase_analytics.AptabaseReporter()
    else:
        _stop_reporter()


def _stop_reporter() -> None:
    global REPORTER
    with _reporter_lock:
        reporter, REPORTER = REPORTER, None
    if reporter is not None:
        try:
            reporter.flush(1.0)
            reporter.close(1.0)
        except Exception:
            pass


def _debug_osd(message: str) -> None:
    # optional toast spam for debugging
    if _cached_setting('debug_osd') == 'true':
        xbmc.executebuiltin('Notification(TIDB, {}, 1500)'.format(message))


def _fresh_bool(key: str) -> bool:
    # served from the cache; refreshed on Monitor.onSettingsChanged
    return _cached_setting(key) == 'true'


def _debug_logging() -> bool:
    return _cached_setting('debug_logging') == 'true'


# What to do at open-ended end credits: 'skip_to_end' (default) or 'play_next'.
_END_CREDITS_ACTIONS = ('skip_to_end', 'play_next')


def _end_credits_action() -> str:
    val = (_cached_setting('end_credits_action') or '').strip()
    return val if val in _END_CREDITS_ACTIONS else 'skip_to_end'


# Kodi's playback OSD (control bar). Visibility is independent of input focus,
# so this stays true while it's on screen even though our dialog is modal.
_OSD_VISIBLE_CONDITION = 'Window.IsVisible(videoosd)'


def _osd_visible() -> bool:
    try:
        return bool(xbmc.getCondVisibility(_OSD_VISIBLE_CONDITION))
    except Exception:
        return False


# ── Skin integration contract ─────────────────────────────────────────────
# While a segment is manually skippable we publish state on the Home window so
# a skin can show its own native OSD button instead of our modal fallback:
#
#   Window(Home).Property(TheIntroDB.Skip.Active) = true
#   Window(Home).Property(TheIntroDB.Skip.Label)  = Skip Intro / Skip Recap / ...
#   Window(Home).Property(TheIntroDB.Skip.Type)   = intro / recap / credits / preview
#
# The skin advertises that its currently-loaded OSD owns the button by setting
#   Window(Home).Property(TheIntroDB.Kine.OSDButtonSupported) = true
# (cleared on OSD unload). When that flag is set we suppress the skip-choice
# fallback and let the skin's button drive skipping. The button skips by sending
#   NotifyAll(plugin.video.tidb.kine, SkipCurrent)
# which lands in TIDBMonitor.onNotification and skips the active segment.

HOME_WINDOW_ID = 10000
PROP_SKIP_ACTIVE = 'TheIntroDB.Skip.Active'
PROP_SKIP_LABEL = 'TheIntroDB.Skip.Label'
PROP_SKIP_TYPE = 'TheIntroDB.Skip.Type'
PROP_OSD_BUTTON_SUPPORTED = 'TheIntroDB.Kine.OSDButtonSupported'
SKIP_NOTIFY_MESSAGE = 'SkipCurrent'

_SKIP_LABEL_STRINGS = {
    'intro': STR_SKIP_INTRO,
    'recap': STR_SKIP_RECAP,
    'credits': STR_SKIP_CREDITS,
    'preview': STR_SKIP_PREVIEW,
}

# The active manually-skippable segment, shared with the notification thread.
_active_skip_lock = threading.Lock()
_active_skip = None  # type: Optional[Dict[str, Any]]


def _skip_label(segment_type: str) -> str:
    return ADDON.getLocalizedString(_SKIP_LABEL_STRINGS.get(segment_type, STR_SKIP_INTRO))


def _home_window() -> 'xbmcgui.Window':
    return xbmcgui.Window(HOME_WINDOW_ID)


def _osd_button_supported() -> bool:
    # True only while the visible OSD has advertised native skip-button support.
    try:
        return _home_window().getProperty(PROP_OSD_BUTTON_SUPPORTED) == 'true'
    except Exception:
        return False


def _publish_skip_properties(segment_type: str, label: str) -> None:
    try:
        win = _home_window()
        win.setProperty(PROP_SKIP_ACTIVE, 'true')
        win.setProperty(PROP_SKIP_TYPE, segment_type)
        win.setProperty(PROP_SKIP_LABEL, label)
    except Exception:
        pass


def _clear_skip_properties() -> None:
    try:
        win = _home_window()
        win.clearProperty(PROP_SKIP_ACTIVE)
        win.clearProperty(PROP_SKIP_TYPE)
        win.clearProperty(PROP_SKIP_LABEL)
    except Exception:
        pass


def _compute_active_skip(session: 'PlaybackSession', player: TIDBPlayer,
                         enabled_segments: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The manually-skippable segment the playhead is currently inside, if any.

    Excludes auto-skip segments (no user button) and next-episode candidates
    (handled by their own overlay). Indexing matches the main loop's enumerate.
    """
    if not player.isPlaying():
        return None
    try:
        current_time = player.getTime()
    except Exception:
        return None
    margin = 0.25
    for idx, segment in enumerate(enabled_segments):
        bounds = _resolve_segment_bounds(segment, player)
        if bounds is None:
            continue
        api_start, api_end, is_next_ep = bounds
        segment_type = segment['type']
        if is_next_ep or _fresh_bool('auto_skip_{}'.format(segment_type)):
            continue
        if api_start <= current_time < (api_end - margin):
            return {
                'type': segment_type,
                'index': idx,
                'start': api_start,
                'end': api_end,
                'filename': session.current_file,
                'label': _skip_label(segment_type),
                'player': player,
            }
    return None


def _update_active_skip(session: 'PlaybackSession', player: TIDBPlayer,
                        enabled_segments: List[Dict[str, Any]]) -> None:
    """Recompute the active segment and (un)publish the skin properties.

    Active state survives the standalone button timing out; it clears only when
    the segment ends, playback stops/changes, or the segment is skipped.
    """
    global _active_skip
    ctx = _compute_active_skip(session, player, enabled_segments)
    with _active_skip_lock:
        _active_skip = ctx
    if ctx:
        _publish_skip_properties(ctx['type'], ctx['label'])
    else:
        _clear_skip_properties()


def _clear_skip_state() -> None:
    global _active_skip
    with _active_skip_lock:
        _active_skip = None
    _clear_skip_properties()


def _skip_active_segment() -> None:
    """Skip the currently active segment — invoked by the skin's OSD button."""
    with _active_skip_lock:
        ctx = _active_skip
    if not ctx:
        return
    player = ctx.get('player')
    if not player or not player.isPlaying():
        return
    try:
        current_time = player.getTime()
    except Exception:
        current_time = None
    if current_time is not None and current_time >= ctx['end']:
        return  # already past the segment
    skipper.execute_skip(player, ctx['start'], ctx['end'], ctx['filename'], ctx['type'])
    if REPORTER:
        REPORTER.track('segment_skipped', {'segment_type': ctx['type']})
    _debug_osd('Skipped {} (OSD button)'.format(ctx['type']))
    xbmc.log('[TheIntroDB] Skipped {} via skin OSD button'.format(ctx['type']), xbmc.LOGINFO)
    _clear_skip_state()


# ── Playback session state ────────────────────────────────────────────────

class PlaybackSession:
    """Holds all mutable state for one file's playback."""
    current_file: Optional[str]
    media_ids: Optional[Dict[str, Any]]
    all_segments: Optional[Dict[str, Any]]
    processed_segments: Dict[str, Dict[str, Any]]
    next_episode_info: Optional[Dict[str, Any]]
    next_episode_checked: bool
    submit_start_sec: Optional[float]
    submit_prompted_this_pause: bool
    submit_done_for_file: bool
    last_seen_pause_count: int

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.current_file = None
        self.media_ids = None
        self.all_segments = None
        self.processed_segments = {}
        self.next_episode_info = None
        self.next_episode_checked = False
        # Submission state
        self.submit_start_sec = None
        self.submit_prompted_this_pause = False
        self.submit_done_for_file = False
        self.last_seen_pause_count = 0


# ── Segment collection ────────────────────────────────────────────────────

def _collect_enabled_segments(all_segments: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Gather all enabled segments from the API response, sorted chronologically."""
    result = []
    for segment_type in ('intro', 'recap', 'preview', 'credits'):
        if not _fresh_bool('enable_{}'.format(segment_type)):
            continue
        segments = all_segments.get(segment_type, [])
        for idx, seg in enumerate(segments):
            entry = seg.copy()
            entry['type'] = segment_type
            entry['index'] = idx
            result.append(entry)

    result.sort(key=lambda x: x['start'] if x['start'] is not None else 0)
    return result


# ── Skip / next-episode handling ──────────────────────────────────────────

def _resolve_segment_bounds(segment: Dict[str, Any], player: TIDBPlayer) -> Optional[Tuple[float, float, bool]]:
    """Normalise start/end; returns (start, end, is_next_episode_candidate) or None."""
    api_start = segment['start']
    api_end = segment['end']

    if api_start is None and api_end is None:
        return None

    if api_start is None:
        api_start = 0

    seg_type = segment['type']
    # end_credits_action determines what skipping a credits or preview segment
    # always does, regardless of whether the DB gave an end time: skip to the
    # end (default) or play the next episode.
    is_next_ep = seg_type in ('credits', 'preview') and _end_credits_action() == 'play_next'

    if api_end is None:
        try:
            # No end time given: end 2 seconds before the media ends.
            api_end = player.getTotalTime() - 2
        except Exception:
            return None

    return api_start, api_end, is_next_ep


def _show_skip_overlay(player: TIDBPlayer, monitor: xbmc.Monitor, api_end: float, segment_type: str, segment_idx: int) -> Optional[bool]:
    """Show the standard skip pill and return whether it was pressed."""
    if monitor.abortRequested():
        return None  # signal to break the outer loop
    return overlay_mod.show_skip_overlay(
        intro_end=api_end,
        player=player,
        monitor=monitor,
        segment_type=segment_type,
        segment_index=segment_idx,
    )


def _handle_segment(segment: Dict[str, Any], segment_idx: int, player: TIDBPlayer, monitor: xbmc.Monitor, session: PlaybackSession, filename: str) -> Optional[str]:
    """Process a single segment: auto-skip, next-episode, or show skip button.

    Returns 'break' if the service loop should exit, else None.
    """
    bounds = _resolve_segment_bounds(segment, player)
    if bounds is None:
        return None
    api_start, api_end, is_next_ep = bounds

    segment_type = segment['type']
    segment_key = '{}_{}'.format(segment_type, segment_idx)

    if _debug_logging():
        xbmc.log('[TheIntroDB] Processing {} segment {}: start={}, end={}'.format(
            segment_type, segment_idx, api_start, api_end), xbmc.LOGINFO)

    current_time = player.getTime() if player.isPlaying() else 0

    if not _should_show_segment_button(session.processed_segments, segment_key,
                                       current_time, api_start, api_end):
        # Start prompt already shown (or we're outside the segment). If the OSD
        # is up while we're still inside, show the Skip/Watch choice on top.
        _maybe_show_dual(segment, segment_idx, player, monitor, session,
                         filename, api_start, api_end, is_next_ep)
        return None

    if not player.isPlaying():
        return None

    # Resolve display name
    segment_names = {
        'intro': ADDON.getLocalizedString(STR_SKIP_INTRO),
        'recap': ADDON.getLocalizedString(STR_SKIP_RECAP),
        'credits': ADDON.getLocalizedString(STR_SKIP_CREDITS),
        'preview': ADDON.getLocalizedString(STR_SKIP_PREVIEW),
    }
    segment_name = segment_names.get(segment_type, segment_type.title())
    overlay_type = segment_type

    if _fresh_bool('auto_skip_{}'.format(segment_type)):
        skipper.execute_skip(player, api_start, api_end, filename, segment_type)
        if REPORTER:
            REPORTER.track('segment_auto_skipped', {'segment_type': segment_type})
        _debug_osd('Auto-skipped {}'.format(segment_name))
        xbmc.log('[TheIntroDB] Auto-skipped {} to {:.1f}s'.format(segment_name, api_end), xbmc.LOGINFO)
        return None

    # Check for next-episode promotion
    if is_next_ep:
        if not session.next_episode_checked:
            session.next_episode_info = player.get_next_episode()
            session.next_episode_checked = True
        if session.next_episode_info:
            return _handle_next_episode(
                player, monitor, session, api_end, segment_type, segment_idx)

    # Show skip button
    xbmc.log('[TheIntroDB] Showing skip overlay for {}'.format(segment_name), xbmc.LOGINFO)
    pressed = _show_skip_overlay(player, monitor, api_end, overlay_type, segment_idx)
    if pressed is None:
        return 'break'
    if pressed:
        xbmc.log('[TheIntroDB] User pressed Skip {}'.format(segment_name), xbmc.LOGINFO)
        skipper.execute_skip(player, api_start, api_end, filename, segment_type)
        if REPORTER:
            REPORTER.track('segment_skipped', {'segment_type': segment_type})
        _debug_osd('Skipped {} to {:.1f}s'.format(segment_name, api_end))
    else:
        xbmc.log('[TheIntroDB] User did NOT skip {}'.format(segment_name), xbmc.LOGINFO)
        # Start prompt timed out without a skip. If the OSD is up right now and
        # we're still inside, transition immediately to the Skip/Watch choice.
        _maybe_show_dual(segment, segment_idx, player, monitor, session,
                         filename, api_start, api_end, is_next_ep)
    return None


def _maybe_show_dual(segment: Dict[str, Any], segment_idx: int, player: TIDBPlayer, monitor: xbmc.Monitor,
                     session: PlaybackSession, filename: str, api_start: float, api_end: float,
                     is_next_ep: bool) -> None:
    """Show the two-button Skip/Watch dialog while the OSD is up mid-segment.

    The dialog mirrors the OSD: it stays while the OSD is visible and re-appears
    each time the OSD re-opens. Suppressed for the rest of the segment entry only
    if the user chose "Watch"; only "Skip" seeks. Used both for the immediate
    single->dual transition (after the start prompt) and for later OSD re-opens.
    """
    segment_type = segment['type']
    # Next-episode segments use their own promotion flow; auto-skip needs no button.
    if is_next_ep or _fresh_bool('auto_skip_{}'.format(segment_type)):
        return
    if not player.isPlaying() or not _osd_visible():
        return
    # If the visible OSD has a native TheIntroDB button, let the skin own it and
    # skip our modal fallback; the published Skip.* properties drive that button.
    if _osd_button_supported():
        return

    # Re-evaluate position: the start prompt may have run for several seconds.
    try:
        current_time = player.getTime()
    except Exception:
        return
    margin = 0.25
    if not (api_start <= current_time < (api_end - margin)):
        return

    segment_key = '{}_{}'.format(segment_type, segment_idx)
    state = session.processed_segments.get(segment_key)
    if not state or state.get('watch_dismissed'):
        return

    xbmc.log('[TheIntroDB] OSD up during {} — showing Skip/Watch choice'.format(segment_type),
             xbmc.LOGINFO)
    result = overlay_mod.show_skip_choice_overlay(
        intro_end=api_end,
        player=player,
        monitor=monitor,
        segment_type=segment_type,
        segment_index=segment_idx,
    )

    segment_names = {
        'intro': ADDON.getLocalizedString(STR_SKIP_INTRO),
        'recap': ADDON.getLocalizedString(STR_SKIP_RECAP),
        'credits': ADDON.getLocalizedString(STR_SKIP_CREDITS),
        'preview': ADDON.getLocalizedString(STR_SKIP_PREVIEW),
    }
    segment_name = segment_names.get(segment_type, segment_type.title())

    if result == 'skip':
        xbmc.log('[TheIntroDB] User pressed Skip {} (OSD choice)'.format(segment_name), xbmc.LOGINFO)
        skipper.execute_skip(player, api_start, api_end, filename, segment_type)
        if REPORTER:
            REPORTER.track('segment_skipped', {'segment_type': segment_type})
        _debug_osd('Skipped {} to {:.1f}s'.format(segment_name, api_end))
    elif result == 'watch':
        state['watch_dismissed'] = True
        xbmc.log('[TheIntroDB] User chose Watch {} — suppressing for this entry'.format(segment_name),
                 xbmc.LOGINFO)
    # 'closed' (OSD hid / intro ended / stopped): leave watch_dismissed False so
    # the dialog can re-appear when the OSD next opens.


def _handle_next_episode(player: TIDBPlayer, monitor: xbmc.Monitor, session: PlaybackSession, api_end: float, segment_type: str, segment_idx: int) -> Optional[str]:
    """Show 'Next Episode' overlay and act on the result."""
    if monitor.abortRequested():
        return 'break'

    overlay_mod.ADDON.getLocalizedString(overlay_mod.STR_NEXT_EPISODE)
    xbmc.log('[TheIntroDB] Showing Next Episode for end-of-media {} segment'.format(segment_type),
             xbmc.LOGINFO)

    pressed = overlay_mod.show_skip_overlay(
        intro_end=api_end,
        player=player,
        monitor=monitor,
        segment_type='next_episode',
        segment_index=segment_idx,
    )
    if pressed:
        xbmc.log('[TheIntroDB] User pressed Next Episode', xbmc.LOGINFO)
        if REPORTER:
            REPORTER.track('next_episode_pressed', {'segment_type': segment_type})
        was_opened = player.play_next_episode(session.next_episode_info)
        if was_opened:
            _debug_osd('Next Episode')
        else:
            xbmc.log('[TheIntroDB] Next episode was no longer available to open', xbmc.LOGWARNING)
    else:
        xbmc.log('[TheIntroDB] User did NOT press Next Episode', xbmc.LOGINFO)
    return None


# ── Pause-detection submission ────────────────────────────────────────────

def _handle_submit_tick(session: PlaybackSession, player: TIDBPlayer, monitor: xbmc.Monitor, all_segments: Dict[str, Any], media_ids: Dict[str, Any]) -> bool:
    """Check for a pause event and run the mark-start / mark-end / submit flow.

    Mutates session in place. Returns True if the segment cache should be invalidated.
    """
    is_paused = player.is_paused
    current_pause_count = player.pause_count

    # Detect fresh pause edge via callback-driven counter
    if current_pause_count > session.last_seen_pause_count:
        session.submit_prompted_this_pause = False
        session.last_seen_pause_count = current_pause_count

    if not _should_offer_submit(session, is_paused, all_segments):
        return False

    try:
        current_time = player.getTime()
    except Exception:
        return False

    if current_time > SUBMIT_WINDOW_SECS:
        xbmc.log('[TheIntroDB] Submit blocked: past {:.0f}s window (at {:.1f}s)'.format(
            SUBMIT_WINDOW_SECS, current_time), xbmc.LOGINFO)
        return False

    xbmc.log('[TheIntroDB] Submit flow active at {:.1f}s'.format(current_time), xbmc.LOGINFO)
    session.submit_prompted_this_pause = True

    if session.submit_start_sec is None:
        _mark_start(session, player, monitor, current_time)
        return False

    if current_time <= session.submit_start_sec:
        return False

    return _mark_end_and_submit(session, player, monitor, current_time, media_ids)


def _should_offer_submit(session: PlaybackSession, is_paused: bool, all_segments: Dict[str, Any]) -> bool:
    """Guard: all preconditions for showing the submit prompt."""
    if not _fresh_bool('enable_submissions'):
        if is_paused and not session.submit_prompted_this_pause:
            xbmc.log('[TheIntroDB] Submit blocked: enable_submissions is off', xbmc.LOGINFO)
        return False
    api_key = (_cached_setting('introdb_api_key') or '').strip()
    if not api_key:
        if is_paused and not session.submit_prompted_this_pause:
            xbmc.log('[TheIntroDB] Submit blocked: no API key configured', xbmc.LOGINFO)
        return False
    if bool(all_segments.get('intro')):
        if is_paused and not session.submit_prompted_this_pause:
            xbmc.log('[TheIntroDB] Submit blocked: intro segments already exist', xbmc.LOGINFO)
        return False
    if session.submit_done_for_file:
        if is_paused and not session.submit_prompted_this_pause:
            xbmc.log('[TheIntroDB] Submit blocked: already submitted for this file', xbmc.LOGINFO)
        return False
    if not is_paused:
        return False
    if session.submit_prompted_this_pause:
        return False
    xbmc.log('[TheIntroDB] Submit guards passed — showing overlay', xbmc.LOGINFO)
    return True


def _mark_start(session: PlaybackSession, player: TIDBPlayer, monitor: xbmc.Monitor, current_time: float) -> None:
    """Phase 1: show the 'Mark Intro Start' pill."""
    label = ADDON.getLocalizedString(STR_MARK_START)
    xbmc.log('[TheIntroDB] Showing Mark Start at {:.1f}s'.format(current_time), xbmc.LOGINFO)

    pressed = submit_overlay.show_submit_mark_overlay(
        label_text=label, player=player, monitor=monitor)
    if pressed:
        session.submit_start_sec = current_time
        xbmc.log('[TheIntroDB] Marked intro start: {:.1f}s'.format(current_time), xbmc.LOGINFO)
        xbmc.executebuiltin(
            'Notification(TheIntroDB, Start marked at {:.0f}s — pause at end of intro, 2000)'.format(
                current_time))


def _mark_end_and_submit(session: PlaybackSession, player: TIDBPlayer, monitor: xbmc.Monitor, current_time: float, media_ids: Dict[str, Any]) -> bool:
    """Phase 2: show the 'Mark Intro End' pill, validate, confirm, and submit.

    Returns True if the segment cache should be invalidated.
    """
    label = ADDON.getLocalizedString(STR_MARK_END)
    xbmc.log('[TheIntroDB] Showing Mark End at {:.1f}s'.format(current_time), xbmc.LOGINFO)

    pressed = submit_overlay.show_submit_mark_overlay(
        label_text=label, player=player, monitor=monitor)
    if not pressed:
        return False

    start = session.submit_start_sec
    end = current_time
    duration = end - start
    xbmc.log('[TheIntroDB] Marked intro end: {:.1f}s (duration {:.1f}s)'.format(end, duration),
             xbmc.LOGINFO)

    # Validate duration (API: intro must be 5–200s)
    if duration < 5.0:
        xbmc.executebuiltin(
            'Notification(TheIntroDB, Too short — must be at least 5 seconds, 3000)')
        session.submit_start_sec = None
        return False
    if duration > 200.0:
        xbmc.executebuiltin(
            'Notification(TheIntroDB, Too long — intro max is 200 seconds, 3000)')
        session.submit_start_sec = None
        return False

    # Confirm
    confirm = xbmcgui.Dialog().yesno(
        'TheIntroDB',
        'Submit intro: {:.0f}s \u2192 {:.0f}s ({:.0f}s)?'.format(start, end, duration),
    )
    if not confirm:
        session.submit_start_sec = None
        xbmc.log('[TheIntroDB] User cancelled submission', xbmc.LOGINFO)
        return False

    # Submit
    success, msg = introdb.submit_segment(
        tmdb_id=media_ids.get('tmdb_id'),
        imdb_id=media_ids.get('imdb_id'),
        season=media_ids.get('season'),
        episode=media_ids.get('episode'),
        is_movie=media_ids.get('is_movie', False),
        segment='intro',
        start_sec=start,
        end_sec=end,
        video_duration_ms=media_ids.get('duration_ms'),
    )
    if success:
        xbmc.executebuiltin('Notification(TheIntroDB, {}, 3000)'.format(msg))
        session.submit_done_for_file = True
        return True  # invalidate cache
    else:
        xbmc.executebuiltin('Notification(TheIntroDB, {}, 4000)'.format(msg))
        session.submit_start_sec = None
        return False


# ── Main service loop ─────────────────────────────────────────────────────

def _run_service() -> None:
    monitor = TIDBMonitor()
    player = TIDBPlayer()
    session = PlaybackSession()
    # Only loads the analytics engine if the user has opted in.
    _reconcile_reporter()

    xbmc.log('[TheIntroDB] Service started', xbmc.LOGINFO)

    while not monitor.abortRequested():
        if monitor.waitForAbort(1.0):
            break

        if not player.playback_started:
            session.reset()
            _clear_skip_state()
            continue

        # skip movies that do not look like tv; player decides
        if not player.is_tv_content:
            _clear_skip_state()
            continue

        filename = player.filename
        if not filename:
            _clear_skip_state()
            continue

        # New file — reset everything
        if filename != session.current_file:
            session.reset()
            session.current_file = filename
            xbmc.log('[TheIntroDB] Reset segment tracking for file: {}'.format(filename),
                     xbmc.LOGINFO)
            if REPORTER:
                REPORTER.track_daily('addon_active')

        _debug_osd('Monitoring: {}'.format(filename[-40:]))

        # ── Fetch media IDs (cached) ──
        if session.media_ids is None:
            session.media_ids = player.get_media_ids()
            xbmc.log('[TheIntroDB] Media IDs: tmdb={} imdb={} S{}E{} movie={}'.format(
                session.media_ids.get('tmdb_id'), session.media_ids.get('imdb_id'),
                session.media_ids.get('season'), session.media_ids.get('episode'),
                session.media_ids.get('is_movie', False)), xbmc.LOGINFO)
        media_ids = session.media_ids
        tmdb = media_ids.get('tmdb_id')
        imdb = media_ids.get('imdb_id')
        m_season = media_ids.get('season')
        m_episode = media_ids.get('episode')
        m_movie = media_ids.get('is_movie', False)

        introdb_on = _fresh_bool('introdb_enabled')
        if _debug_logging():
            _raw = _cached_setting('introdb_enabled')
            xbmc.log('[TheIntroDB] introdb_enabled raw={!r} lookups_on={}'.format(
                _raw, introdb_on), xbmc.LOGINFO)

        # ── Fetch segments (cached) ──
        all_segments = {}
        if introdb_on and (tmdb or imdb):
            if session.all_segments is None:
                session.all_segments = introdb.query_all_segments(
                    tmdb_id=tmdb, imdb_id=imdb,
                    season=m_season, episode=m_episode, is_movie=m_movie,
                    duration_ms=media_ids.get('duration_ms'),
                )
            all_segments = session.all_segments or {}

        if all_segments and _debug_logging():
            xbmc.log('[TheIntroDB] API returned segments: {}'.format(
                list(all_segments.keys())), xbmc.LOGINFO)
            for seg_type, segs in all_segments.items():
                xbmc.log('[TheIntroDB] {} segments: {}'.format(seg_type, len(segs)),
                         xbmc.LOGINFO)

        # ── Process skip buttons ──
        enabled_segments = _collect_enabled_segments(all_segments)
        if _debug_logging():
            xbmc.log('[TheIntroDB] Total enabled segments to process: {}'.format(
                len(enabled_segments)), xbmc.LOGINFO)

        # Publish skip state for the skin before any (blocking) overlay shows.
        _update_active_skip(session, player, enabled_segments)

        for seg_idx, segment in enumerate(enabled_segments):
            result = _handle_segment(
                segment, seg_idx, player, monitor, session, filename)
            if result == 'break':
                break

        # ── Pause-to-submit ──
        cache_dirty = _handle_submit_tick(
            session, player, monitor, all_segments, media_ids)
        if cache_dirty:
            session.all_segments = None

    xbmc.log('[TheIntroDB] Service stopped', xbmc.LOGINFO)
    _clear_skip_state()
    _stop_reporter()


# ── Segment button timing ────────────────────────────────────────────────

def _should_show_segment_button(processed_segments: Dict[str, Dict[str, Any]], segment_key: str, current_time: float,
                                segment_start: float, segment_end: float, margin: float = 0.25) -> bool:
    """
    Show the skip button once per segment entry.

    If playback exits a segment and later re-enters it, including by seeking into
    the middle of the segment, the next entry gets a fresh 5 second overlay.
    """
    state = processed_segments.setdefault(segment_key, {
        'inside': False,
        'shown_for_entry': False,
        'watch_dismissed': False,
        'last_time': None,
    })

    inside_segment = segment_start <= current_time < (segment_end - margin)
    previous_time = state.get('last_time')

    if not inside_segment:
        state['inside'] = False
        state['shown_for_entry'] = False
        state['watch_dismissed'] = False
        state['last_time'] = current_time
        return False

    reentered = (not state['inside'])
    if previous_time is not None and current_time + margin < previous_time:
        reentered = True

    if reentered:
        if _debug_logging():
            xbmc.log('[TheIntroDB] Entry detected for {} at {:.1f}s'.format(
                segment_key, current_time), xbmc.LOGINFO)
        state['shown_for_entry'] = False
        state['watch_dismissed'] = False

    state['inside'] = True
    state['last_time'] = current_time

    if state['shown_for_entry']:
        return False

    state['shown_for_entry'] = True
    return True

if __name__ == '__main__':
    _run_service()
