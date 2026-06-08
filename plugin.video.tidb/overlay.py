# skip intro button windowxml — background thread closes when playhead passes intro end
import os
import threading
import time
import xbmc
import xbmcgui
import xbmcaddon
import xbmcvfs
from typing import Optional, Callable

ADDON = xbmcaddon.Addon()
ADDON_PATH: str = ADDON.getAddonInfo('path')
# Must match the last argument to SkipOverlay / WindowXMLDialog (folder under resources/skins/default/).
_OVERLAY_RES = '1080i'
# Pill image controls (shadow, default fill, focus fill). Textures are set from
# Python by absolute path; the visible background comes from these controls, not
# the button texture (relative button textures don't render on some platforms).
_BG_IMAGE_IDS = (3003, 3004, 3006)

# String IDs for localization
STR_SKIP_INTRO = 32001
STR_SKIP_RECAP = 32003
STR_SKIP_CREDITS = 32004
STR_SKIP_PREVIEW = 32005
STR_NEXT_EPISODE = 32012
STR_WATCH_INTRO = 32040
STR_WATCH_RECAP = 32041
STR_WATCH_CREDITS = 32042
STR_WATCH_PREVIEW = 32043

# must match overlay.xml window id
OVERLAY_WINDOW_ID = 14000

ACTION_SELECT = 7
ACTION_PREVIOUS_MENU = 10
ACTION_BACK = 92
BUTTON_ID = 3001

_POLL_INTERVAL = 0.5
_START_POLL_INTERVAL = 0.25
_CLOCK_ADVANCE_EPSILON = 0.05
_DISPLAY_DURATION = 5.0
# Kodi's playback OSD (control bar). The Skip/Watch choice mirrors its visibility.
_OSD_VISIBLE_CONDITION = 'Window.IsVisible(videoosd)'
# Grace before giving up if the OSD is never detected after the choice opens.
_CHOICE_STARTUP_GRACE = 1.5


def _osd_visible() -> bool:
    try:
        return bool(xbmc.getCondVisibility(_OSD_VISIBLE_CONDITION))
    except Exception:
        return False


def _rounded_rect_texture_path() -> str:
    joined = os.path.join(
        ADDON_PATH, 'resources', 'skins', 'default', _OVERLAY_RES, 'rounded_rect.png')
    return xbmcvfs.translatePath(joined)


class SkipOverlay(xbmcgui.WindowXMLDialog):

    def __new__(cls, xml_file: str, addon_path: str, skin: str, res: str,
                callback: Optional[Callable[[], None]] = None, intro_end: Optional[float] = None,
                player: Optional[xbmc.Player] = None, monitor: Optional[xbmc.Monitor] = None,
                segment_type: str = 'intro', segment_index: int = 0) -> 'SkipOverlay':
        return super(SkipOverlay, cls).__new__(cls, xml_file, addon_path, skin, res)  # type: ignore[call-arg]

    def __init__(self, xml_file: str, addon_path: str, skin: str, res: str,
                 callback: Optional[Callable[[], None]] = None, intro_end: Optional[float] = None,
                 player: Optional[xbmc.Player] = None, monitor: Optional[xbmc.Monitor] = None,
                 segment_type: str = 'intro', segment_index: int = 0) -> None:
        super(SkipOverlay, self).__init__(xml_file, addon_path, skin, res)
        self._skip_pressed: bool = False
        self._callback: Optional[Callable[[], None]] = callback
        self._intro_end: Optional[float] = intro_end
        self._player: Optional[xbmc.Player] = player
        self._monitor: Optional[xbmc.Monitor] = monitor
        self._poll_thread: Optional[threading.Thread] = None
        self._closed: bool = False
        self._lock: threading.Lock = threading.Lock()
        self._segment_type: str = segment_type
        self._segment_index: int = segment_index
        self._display_deadline: Optional[float] = None

    @property
    def skip_pressed(self) -> bool:
        return self._skip_pressed

    def _get_segment_button_text(self, segment_type: str) -> str:
        """Get the appropriate button text for the segment type."""
        segment_texts = {
            'intro': ADDON.getLocalizedString(STR_SKIP_INTRO),
            'recap': ADDON.getLocalizedString(STR_SKIP_RECAP),
            'credits': ADDON.getLocalizedString(STR_SKIP_CREDITS),
            'preview': ADDON.getLocalizedString(STR_SKIP_PREVIEW),
            'next_episode': ADDON.getLocalizedString(STR_NEXT_EPISODE)
        }
        base_text = segment_texts.get(segment_type, ADDON.getLocalizedString(STR_SKIP_INTRO))
        
        return base_text

    def onInit(self) -> None:
        mon = self._monitor if self._monitor is not None else xbmc.Monitor()
        if mon.abortRequested():
            self._dismiss_main_thread()
            return
        
        # Reload pill textures by absolute path (relative paths sometimes fail for addon WindowXML on some platforms).
        try:
            tex = _rounded_rect_texture_path()
            for image_id in _BG_IMAGE_IDS:
                self.getControl(image_id).setImage(tex)
        except Exception as e:
            xbmc.log('[TheIntroDB] Overlay background textures: {}'.format(e), xbmc.LOGWARNING)
        
        # Set dynamic button text based on segment type
        try:
            button_text = self._get_segment_button_text(self._segment_type)
            button_control = self.getControl(BUTTON_ID)
            if isinstance(button_control, xbmcgui.ControlButton):
                button_control.setLabel(button_text)
        except Exception as e:
            xbmc.log('[TheIntroDB] Failed to set button text: {}'.format(e), xbmc.LOGWARNING)
        
        try:
            self.setFocusId(BUTTON_ID)
        except Exception:
            pass
        if self._intro_end is not None and self._player is not None:
            self._display_deadline = time.time() + _DISPLAY_DURATION
            self._poll_thread = threading.Thread(target=self._poll_loop)
            self._poll_thread.daemon = True
            self._poll_thread.start()

    def onClick(self, controlId: int) -> None:
        if controlId == BUTTON_ID:
            self._do_skip()

    def onAction(self, action: xbmcgui.Action) -> None:
        aid = action.getId()
        if aid == ACTION_SELECT:
            try:
                if self.getFocusId() == BUTTON_ID:
                    self._do_skip()
            except Exception:
                pass
            return
        if aid in (ACTION_PREVIOUS_MENU, ACTION_BACK):
            self._dismiss_main_thread()

    def _do_skip(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._skip_pressed = True
        self._stop_poll_thread()
        if self._callback:
            try:
                self._callback()
            except Exception:
                pass
        self._dismiss_main_thread()

    def _poll_loop(self) -> None:
        # close from worker thread using dialog.close builtin — kodi does not like gui from random threads otherwise
        mon = self._monitor if self._monitor is not None else xbmc.Monitor()
        # No immediate getTime check: playback advances while WindowXML loads; that race closed the dialog instantly.
        while True:
            with self._lock:
                if self._closed:
                    return
            if mon.abortRequested():
                self._close_from_bg_thread()
                return
            if mon.waitForAbort(_POLL_INTERVAL):
                self._close_from_bg_thread()
                return
            try:
                pl = self._player
                if self._display_deadline is not None and time.time() >= self._display_deadline:
                    self._close_from_bg_thread()
                    return
                if pl and pl.isPlaying() and self._intro_end is not None:
                    current_time = pl.getTime()
                    if current_time >= self._intro_end:
                        self._close_from_bg_thread()
                        return
                elif pl and not pl.isPlaying():
                    self._close_from_bg_thread()
                    return
            except Exception:
                pass

    def _close_from_bg_thread(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.close()
        except Exception as e:
            xbmc.log('[TheIntroDB] Overlay close failed: {}'.format(e), xbmc.LOGWARNING)

    def _stop_poll_thread(self) -> None:
        with self._lock:
            self._closed = True

    def _dismiss_main_thread(self) -> None:
        self._stop_poll_thread()
        try:
            self.close()
        except Exception:
            pass


def show_skip_overlay(callback: Optional[Callable[[], None]] = None, intro_end: Optional[float] = None,
                      player: Optional[xbmc.Player] = None, monitor: Optional[xbmc.Monitor] = None,
                      segment_type: str = 'intro', segment_index: int = 0) -> bool:
    # blocks until window closes; true if user hit skip
    mon = monitor if monitor is not None else xbmc.Monitor()
    if mon.abortRequested():
        return False
    if not _wait_for_playback_clock(player, mon, intro_end):
        return False
    try:
        wnd = SkipOverlay(
            'overlay.xml',
            ADDON_PATH,
            'default',
            _OVERLAY_RES,
            callback=callback,
            intro_end=intro_end,
            player=player,
            monitor=monitor,
            segment_type=segment_type,
            segment_index=segment_index,
        )
        wnd.doModal()
        pressed = wnd.skip_pressed
        del wnd
        return pressed
    except Exception as e:
        xbmc.log('[TheIntroDB] Overlay error: {}'.format(e), xbmc.LOGERROR)
        return False


def _wait_for_playback_clock(player: Optional[xbmc.Player], monitor: xbmc.Monitor,
                             intro_end: Optional[float]) -> bool:
    """Wait until Kodi's playback clock is advancing before opening the overlay."""
    if player is None:
        return True

    previous_time: Optional[float] = None

    while not monitor.abortRequested():
        try:
            playback_started = getattr(player, 'playback_started', True)
            if not playback_started:
                return False

            if player.isPlaying():
                current_time = player.getTime()
                if intro_end is not None and current_time >= intro_end:
                    return False
                if previous_time is not None and current_time > previous_time + _CLOCK_ADVANCE_EPSILON:
                    return True
                previous_time = current_time
            else:
                previous_time = None
        except Exception:
            previous_time = None

        if monitor.waitForAbort(_START_POLL_INTERVAL):
            return False

    return False


# ── Skip / Watch choice overlay (shown when the OSD is up mid-segment) ──────

CHOICE_WINDOW_ID = 14001
SKIP_BUTTON_ID = 3001
WATCH_BUTTON_ID = 3002
# Per pill: shadow, default fill, focus fill (skip 3013/3014/3016 / watch 3023/3024/3026).
_CHOICE_IMAGE_IDS = (3013, 3014, 3016, 3023, 3024, 3026)


class SkipChoiceOverlay(xbmcgui.WindowXMLDialog):
    """Two-button dialog ("Skip X" / "Watch X").

    Returned to the caller via skip_pressed. Closing it (Watch, Back, timeout,
    segment end, or stop) hands input back to whatever is underneath — e.g. the
    Kodi OSD that triggered it.
    """

    def __new__(cls, xml_file: str, addon_path: str, skin: str, res: str,
                intro_end: Optional[float] = None, player: Optional[xbmc.Player] = None,
                monitor: Optional[xbmc.Monitor] = None, segment_type: str = 'intro',
                segment_index: int = 0) -> 'SkipChoiceOverlay':
        return super(SkipChoiceOverlay, cls).__new__(cls, xml_file, addon_path, skin, res)  # type: ignore[call-arg]

    def __init__(self, xml_file: str, addon_path: str, skin: str, res: str,
                 intro_end: Optional[float] = None, player: Optional[xbmc.Player] = None,
                 monitor: Optional[xbmc.Monitor] = None, segment_type: str = 'intro',
                 segment_index: int = 0) -> None:
        super(SkipChoiceOverlay, self).__init__(xml_file, addon_path, skin, res)
        self._result: Optional[str] = None  # 'skip' | 'watch' | None (timeout/dismiss)
        self._intro_end: Optional[float] = intro_end
        self._player: Optional[xbmc.Player] = player
        self._monitor: Optional[xbmc.Monitor] = monitor
        self._poll_thread: Optional[threading.Thread] = None
        self._closed: bool = False
        self._lock: threading.Lock = threading.Lock()
        self._segment_type: str = segment_type
        self._segment_index: int = segment_index
        self._osd_seen: bool = False
        self._startup_deadline: Optional[float] = None

    @property
    def result(self) -> str:
        # 'skip' | 'watch' | 'closed' (OSD hidden / intro end / stop / abort)
        return self._result or 'closed'

    @property
    def skip_pressed(self) -> bool:
        return self._result == 'skip'

    def onInit(self) -> None:
        mon = self._monitor if self._monitor is not None else xbmc.Monitor()
        if mon.abortRequested():
            self._dismiss_main_thread()
            return

        try:
            tex = _rounded_rect_texture_path()
            for image_id in _CHOICE_IMAGE_IDS:
                self.getControl(image_id).setImage(tex)
        except Exception as e:
            xbmc.log('[TheIntroDB] Choice overlay textures: {}'.format(e), xbmc.LOGWARNING)

        skip_texts = {
            'intro': STR_SKIP_INTRO, 'recap': STR_SKIP_RECAP,
            'credits': STR_SKIP_CREDITS, 'preview': STR_SKIP_PREVIEW,
        }
        watch_texts = {
            'intro': STR_WATCH_INTRO, 'recap': STR_WATCH_RECAP,
            'credits': STR_WATCH_CREDITS, 'preview': STR_WATCH_PREVIEW,
        }
        try:
            skip_label = ADDON.getLocalizedString(skip_texts.get(self._segment_type, STR_SKIP_INTRO))
            watch_label = ADDON.getLocalizedString(watch_texts.get(self._segment_type, STR_WATCH_INTRO))
            skip_btn = self.getControl(SKIP_BUTTON_ID)
            watch_btn = self.getControl(WATCH_BUTTON_ID)
            if isinstance(skip_btn, xbmcgui.ControlButton):
                skip_btn.setLabel(skip_label)
            if isinstance(watch_btn, xbmcgui.ControlButton):
                watch_btn.setLabel(watch_label)
        except Exception as e:
            xbmc.log('[TheIntroDB] Choice overlay labels: {}'.format(e), xbmc.LOGWARNING)

        try:
            self.setFocusId(SKIP_BUTTON_ID)
        except Exception:
            pass

        if self._intro_end is not None and self._player is not None:
            self._startup_deadline = time.time() + _CHOICE_STARTUP_GRACE
            self._poll_thread = threading.Thread(target=self._poll_loop)
            self._poll_thread.daemon = True
            self._poll_thread.start()

    def onClick(self, controlId: int) -> None:
        if controlId == SKIP_BUTTON_ID:
            self._finish('skip')
        elif controlId == WATCH_BUTTON_ID:
            self._finish('watch')

    def onAction(self, action: xbmcgui.Action) -> None:
        aid = action.getId()
        if aid == ACTION_SELECT:
            try:
                focus = self.getFocusId()
            except Exception:
                focus = SKIP_BUTTON_ID
            self._finish('skip' if focus == SKIP_BUTTON_ID else 'watch')
            return
        if aid in (ACTION_PREVIOUS_MENU, ACTION_BACK):
            # Back simply dismisses (same as "Watch"): hand input back to the OSD.
            self._finish('watch')

    def _finish(self, result: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._result = result
        self._stop_poll_thread()
        self._dismiss_main_thread()

    def _poll_loop(self) -> None:
        mon = self._monitor if self._monitor is not None else xbmc.Monitor()
        while True:
            with self._lock:
                if self._closed:
                    return
            if mon.abortRequested():
                self._close_from_bg_thread()
                return
            if mon.waitForAbort(_POLL_INTERVAL):
                self._close_from_bg_thread()
                return
            try:
                pl = self._player
                # The choice mirrors the OSD: stay while it's visible, close when
                # it hides. _result stays None on these closes -> reported as
                # 'closed', so the service may re-show on the next OSD open.
                if _osd_visible():
                    self._osd_seen = True
                elif self._osd_seen:
                    self._close_from_bg_thread()
                    return
                elif self._startup_deadline is not None and time.time() >= self._startup_deadline:
                    # OSD never (re)appeared after we opened — don't hang.
                    self._close_from_bg_thread()
                    return
                if pl and pl.isPlaying() and self._intro_end is not None:
                    if pl.getTime() >= self._intro_end:
                        self._close_from_bg_thread()
                        return
                elif pl and not pl.isPlaying():
                    self._close_from_bg_thread()
                    return
            except Exception:
                pass

    def _close_from_bg_thread(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.close()
        except Exception as e:
            xbmc.log('[TheIntroDB] Choice overlay close failed: {}'.format(e), xbmc.LOGWARNING)

    def _stop_poll_thread(self) -> None:
        with self._lock:
            self._closed = True

    def _dismiss_main_thread(self) -> None:
        self._stop_poll_thread()
        try:
            self.close()
        except Exception:
            pass


def show_skip_choice_overlay(intro_end: Optional[float] = None, player: Optional[xbmc.Player] = None,
                             monitor: Optional[xbmc.Monitor] = None, segment_type: str = 'intro',
                             segment_index: int = 0) -> str:
    """Show the Skip/Watch choice while the OSD is up. Blocks until it closes.

    Returns 'skip' (user chose Skip), 'watch' (user dismissed), or 'closed'
    (OSD hidden / intro ended / playback stopped / abort — eligible for re-show).
    """
    mon = monitor if monitor is not None else xbmc.Monitor()
    if mon.abortRequested():
        return 'closed'
    if not _wait_for_playback_clock(player, mon, intro_end):
        return 'closed'
    try:
        wnd = SkipChoiceOverlay(
            'overlay_choice.xml',
            ADDON_PATH,
            'default',
            _OVERLAY_RES,
            intro_end=intro_end,
            player=player,
            monitor=monitor,
            segment_type=segment_type,
            segment_index=segment_index,
        )
        wnd.doModal()
        result = wnd.result
        del wnd
        return result
    except Exception as e:
        xbmc.log('[TheIntroDB] Choice overlay error: {}'.format(e), xbmc.LOGERROR)
        return 'closed'
