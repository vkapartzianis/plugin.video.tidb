# seek to intro end + offset from settings
import xbmc
import xbmcaddon
from typing import Optional

ADDON = xbmcaddon.Addon()


def execute_skip(player: xbmc.Player, intro_start: float, intro_end: float, filename: Optional[str] = None, segment_type: str = 'intro') -> bool:
    if not player.isPlaying():
        return False

    offset = int(ADDON.getSetting('skip_offset') or 2)
    target = intro_end + offset

    total_time = player.getTotalTime()
    if target >= total_time:
        # End-of-media skip (e.g. end credits): land 2s before the end so the
        # file finishes naturally rather than seeking past it.
        target = total_time - 2

    segment_names = {
        'intro': 'intro',
        'recap': 'recap',
        'credits': 'credits',
        'preview': 'preview'
    }
    segment_name = segment_names.get(segment_type, 'intro')

    xbmc.log('[IntroSkip] Skipping {}: {:.1f}s -> {:.1f}s (target {:.1f}s)'.format(
        segment_name, intro_start, intro_end, target), xbmc.LOGINFO)

    player.seekTime(target)
    return True
