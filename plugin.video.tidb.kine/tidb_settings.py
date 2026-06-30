# tidb_settings.py — shared, cached settings access for every add-on module.
#
# A long-lived xbmcaddon.Addon() returns a snapshot of settings from when it was
# created and does not reflect a GUI toggle made at runtime; building a fresh
# xbmcaddon.Addon(id) on every read is live but wasteful in hot paths. So values
# are cached here and the cache is cleared when Kodi reports a change — the
# service's Monitor.onSettingsChanged calls invalidate(). The module (and its
# cache) is shared by every importer, so a single invalidate() refreshes the
# whole add-on at once.
import xbmcaddon

_ADDON = xbmcaddon.Addon()
_ADDON_ID = _ADDON.getAddonInfo('id')
_cache = {}


def invalidate() -> None:
    """Drop cached values so the next read re-fetches them. Called from
    Monitor.onSettingsChanged; safe to call from another thread."""
    _cache.clear()


def get(key: str) -> str:
    """Cached setting value, refreshed whenever invalidate() is called."""
    try:
        return _cache[key]
    except KeyError:
        pass
    try:
        # A fresh Addon instance reads the live (post-change) value.
        value = xbmcaddon.Addon(_ADDON_ID).getSetting(key)
    except Exception:
        value = _ADDON.getSetting(key)
    _cache[key] = value
    return value


def get_bool(key: str) -> bool:
    return get(key) == 'true'


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(get(key))
    except (TypeError, ValueError):
        return default
