import json
import threading
import time
import xbmc
import tidb_settings
from typing import Optional, Dict, Any, Tuple, List, Union

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:
    from urllib2 import Request, urlopen, HTTPError, URLError

API_BASE = 'https://api.theintrodb.org/v3'
# Fallback source queried when the primary has no data for a title.
# Different API: TV episodes only, looked up by IMDb id, with a flat response
# shape (one object per type, 'outro' instead of 'credits', no 'preview').
API_BASE_FALLBACK = 'https://api.introdb.app'
SEGMENT_TYPES = ('intro', 'recap', 'credits', 'preview')
# TMDb is used only to resolve a TV show's IMDb id when TheIntroDB has no data and
# Kodi exposed no IMDb id, so the IMDb-only IntroDB.app fallback can still run.
TMDB_API_BASE = 'https://api.themoviedb.org/3'
TMDB_API_KEY = '5b6ab2d01b48b2149e3460be886dcb72'
_tmdb_imdb_cache = {}  # type: Dict[str, Optional[str]]  # tmdb show id -> imdb id / None
# Per-service rate limiting. Each upstream host keeps its own state so a backoff
# or pacing gap for one never throttles another (notably: a 429 from TheIntroDB
# must not stop us querying the IntroDB.app fallback). TheIntroDB documents a
# rate limit (429 + Retry-After), so we also keep a small proactive gap between
# consecutive requests to it; IntroDB.app has no documented limit, so we don't
# pace it proactively but still honor a 429 if it ever sends one. TMDb is a
# separate host and isn't routed through this limiter at all.
SERVICE_PRIMARY = 'theintrodb.org'
SERVICE_FALLBACK = 'introdb.app'
_PROACTIVE_GAP = {SERVICE_PRIMARY: 0.4}  # seconds between consecutive same-service requests
_rate_limit_lock = threading.Lock()  # guards _rate_limit_state across threads
_rate_limit_state = {}  # type: Dict[str, Dict[str, float]]  # service -> {'last', 'until'}


def _debug_logging() -> bool:
    return tidb_settings.get_bool('debug_logging')


def _log_resp(body: str) -> None:
    if not _debug_logging():
        return
    snippet = body[:500] if len(body) > 500 else body
    xbmc.log('[TheIntroDB] TheIntroDB response: {}'.format(snippet), xbmc.LOGINFO)


def _get_api_key() -> str:
    return (tidb_settings.get('introdb_api_key') or '').strip()


def _is_enabled() -> bool:
    return tidb_settings.get_bool('introdb_enabled')


def _wait_rate_limit(service: str) -> bool:
    """Pace requests to a single service; returns False if it's in 429 backoff."""
    gap = _PROACTIVE_GAP.get(service, 0.0)
    with _rate_limit_lock:
        st = _rate_limit_state.setdefault(service, {'last': 0.0, 'until': 0.0})
        now = time.time()
        if now < st['until']:
            xbmc.log('[TheIntroDB] {} rate-limited until {:.0f}'.format(service, st['until']),
                     xbmc.LOGINFO)
            return False
        if gap:
            wait = gap - (now - st['last'])
            if wait > 0:
                time.sleep(wait)
        st['last'] = time.time()
    return True


def _mark_rate_limited(service: str, retry: float) -> None:
    """Record a 429 backoff window for one service."""
    with _rate_limit_lock:
        st = _rate_limit_state.setdefault(service, {'last': 0.0, 'until': 0.0})
        st['until'] = time.time() + retry


def _do_request(url: str, api_key: str, service: str) -> Optional[Dict[str, Any]]:
    req = Request(url)
    req.add_header('Accept', 'application/json')
    req.add_header('User-Agent', 'TheIntroDB Kine Addon/1.0')
    if api_key:
        req.add_header('Authorization', 'Bearer {}'.format(api_key))

    try:
        resp = urlopen(req, timeout=8)
        body = resp.read().decode('utf-8')
        data = json.loads(body)
        _log_resp(body)
        return data
    except HTTPError as e:
        if e.code == 429:
            retry = 300
            for header in ('X-UsageLimit-Reset', 'X-RateLimit-Reset', 'Retry-After'):
                val = e.headers.get(header)
                if val:
                    try:
                        retry = int(val)
                    except ValueError:
                        pass
                    break
            _mark_rate_limited(service, retry)
            xbmc.log('[TheIntroDB] {} 429 rate limited for {}s'.format(service, retry),
                     xbmc.LOGWARNING)
        elif e.code == 404:
            xbmc.log('[TheIntroDB] {} 404: not in database'.format(service), xbmc.LOGINFO)
        else:
            xbmc.log('[TheIntroDB] {} HTTP {}'.format(service, e.code), xbmc.LOGWARNING)
        return None
    except URLError as e:
        xbmc.log('[TheIntroDB] {} network error: {}'.format(service, e.reason),
                 xbmc.LOGWARNING)
        return None
    except Exception as e:
        xbmc.log('[TheIntroDB] {} request failed: {}'.format(service, e),
                 xbmc.LOGERROR)
        return None


def _pick_best_segment(segments: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    # intro array may have multiple rows — take best score
    if not segments:
        return None, None

    best = None
    best_score = -1.0
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        start = seg.get('start_ms')
        end = seg.get('end_ms')
        if start is None:
            start = 0
        if end is None:
            continue
        if end <= start:
            continue
        conf = seg.get('confidence') if seg.get('confidence') is not None else 0.5
        count = seg.get('submission_count', 1)
        score = float(conf) + count * 0.001
        if score > best_score:
            best_score = score
            best = (start, end)

    if best:
        return best[0] / 1000.0, best[1] / 1000.0
    return None, None


# Intros/recaps live near the start; an "intro" sitting near the end is almost
# always a mis-tagged credits/outro (a real failure mode in the crowd-sourced
# data). Credits/preview live near the end. With the file duration we reject
# these regardless of which database submitted them.
_INTRO_MAX_START_FRACTION = 0.65   # intro/recap must start within the first 65%
_OUTRO_MIN_END_FRACTION = 0.50     # credits/preview must end after the first 50%
_CONSENSUS_BONUS = 0.5             # score boost when both databases agree on a window


def _plausible_position(segment_type: str, start_ms: Optional[float], end_ms: Optional[float],
                        duration_ms: Optional[float]) -> bool:
    if not duration_ms or duration_ms <= 0:
        return True  # no duration to judge against — don't filter
    if segment_type in ('intro', 'recap'):
        if start_ms is not None and start_ms > _INTRO_MAX_START_FRACTION * duration_ms:
            return False
    elif segment_type in ('credits', 'preview'):
        if end_ms is not None and end_ms < _OUTRO_MIN_END_FRACTION * duration_ms:
            return False
    return True


def _spans_overlap(a_start: float, a_end: Optional[float], b_start: float, b_end: Optional[float]) -> bool:
    # A None end means "to end of media"; treat as +inf for overlap purposes.
    a_e = a_end if a_end is not None else float('inf')
    b_e = b_end if b_end is not None else float('inf')
    return a_start < b_e and b_start < a_e


def _pick_best_segments_all_types(segments: List[Dict[str, Any]], segment_type: str,
                                  duration_ms: Optional[Union[str, int]] = None) -> List[Dict[str, Any]]:
    """Validate candidates, drop implausibly-placed ones, merge windows that both
    databases agree on (consensus), and return the best segment(s) for a type.

    Input segments are the merged candidates from both sources (each tagged with
    'source'). Output: list of {start, end (seconds), score, type}, best first.
    """
    if not segments:
        return []

    try:
        dur = float(duration_ms) if duration_ms else None
    except (TypeError, ValueError):
        dur = None

    valid = []
    for seg_idx, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        start = seg.get('start_ms')
        end = seg.get('end_ms')
        source = seg.get('source', '?')

        # Type rules: intro/recap need an end (start optional); credits/preview
        # need a start (end optional = end of media).
        if segment_type in ('intro', 'recap'):
            if end is None:
                continue
            if start is None:
                start = 0
        else:
            if start is None:
                continue
        if end is not None and end <= start:
            continue

        if not _plausible_position(segment_type, start, end, dur):
            if _debug_logging():
                xbmc.log('[TheIntroDB] Dropping implausible {} from {}: start_ms={} end_ms={} (duration_ms={})'.format(
                    segment_type, source, start, end, duration_ms), xbmc.LOGINFO)
            continue

        conf = seg.get('confidence') if seg.get('confidence') is not None else 0.5
        count = seg.get('submission_count', 1) or 1
        valid.append({'start_ms': start, 'end_ms': end,
                      'confidence': float(conf), 'submission_count': count, 'source': source})

    if not valid:
        return []

    # Cluster candidates whose windows overlap — the same real segment, possibly
    # submitted to both databases. Agreement across sources is our best signal.
    clusters = []  # type: List[Dict[str, Any]]
    for cand in sorted(valid, key=lambda x: x['start_ms']):
        for cl in clusters:
            if _spans_overlap(cand['start_ms'], cand['end_ms'], cl['start_ms'], cl['end_ms']):
                # Adopt the higher-confidence member's bounds; pool the rest.
                if (cand['confidence'], cand['submission_count']) > (cl['confidence'], cl['submission_count']):
                    cl['start_ms'], cl['end_ms'], cl['confidence'] = (
                        cand['start_ms'], cand['end_ms'], cand['confidence'])
                cl['submission_count'] += cand['submission_count']
                cl['sources'].add(cand['source'])
                break
        else:
            clusters.append({'start_ms': cand['start_ms'], 'end_ms': cand['end_ms'],
                             'confidence': cand['confidence'], 'submission_count': cand['submission_count'],
                             'sources': {cand['source']}})

    result = []
    for cl in clusters:
        score = cl['confidence'] + cl['submission_count'] * 0.001
        if len(cl['sources']) > 1:
            score += _CONSENSUS_BONUS
        result.append({
            'start': cl['start_ms'] / 1000.0 if cl['start_ms'] is not None else None,
            'end': cl['end_ms'] / 1000.0 if cl['end_ms'] is not None else None,
            'score': score,
            'type': segment_type,
        })
        if _debug_logging():
            xbmc.log('[TheIntroDB] {} candidate: start={} end={} sources={} score={:.3f}'.format(
                segment_type, result[-1]['start'], result[-1]['end'], sorted(cl['sources']), score), xbmc.LOGINFO)

    result.sort(key=lambda x: x['score'], reverse=True)
    return result


def _normalize_imdb(imdb_id: Optional[str]) -> Optional[str]:
    if not imdb_id:
        return None
    s = str(imdb_id).strip()
    if not s.startswith('tt'):
        return None
    return s


def _valid_tmdb(tmdb_id: Optional[Union[str, int]]) -> bool:
    try:
        return int(str(tmdb_id)) > 0
    except (ValueError, TypeError):
        return False


def _episode_nums(season: Optional[Union[str, int]], episode: Optional[Union[str, int]]) -> Tuple[Optional[int], Optional[int]]:
    try:
        s = int(season)
        e = int(episode)
        return s, e
    except (TypeError, ValueError):
        return None, None


def _build_url(tmdb_id: Optional[Union[str, int]], imdb_id: Optional[str], season: Optional[Union[str, int]], episode: Optional[Union[str, int]], is_movie: bool, duration_ms: Optional[Union[str, int]] = None) -> Tuple[Optional[str], Optional[str]]:
    # prefer tmdb; if missing use imdb (api matches show/episode)
    duration_q = ''
    try:
        if duration_ms is not None:
            dur_int = int(duration_ms)
            if dur_int > 0:
                duration_q = '&duration_ms={}'.format(dur_int)
    except (TypeError, ValueError):
        duration_q = ''

    if tmdb_id and _valid_tmdb(tmdb_id):
        tid = str(tmdb_id).strip()
        if is_movie:
            return '{}/media?tmdb_id={}{}'.format(API_BASE, tid, duration_q), 'tmdb'
        s, e = _episode_nums(season, episode)
        if s is None or e is None or s <= 0 or e <= 0:
            return None, None
        return (
            '{}/media?tmdb_id={}&season={}&episode={}{}'.format(API_BASE, tid, s, e, duration_q),
            'tmdb',
        )

    imdb = _normalize_imdb(imdb_id)
    if not imdb:
        return None, None

    if is_movie:
        return '{}/media?imdb_id={}{}'.format(API_BASE, imdb, duration_q), 'imdb'

    s, e = _episode_nums(season, episode)
    if s is None or e is None or s <= 0 or e <= 0:
        return None, None
    return '{}/media?imdb_id={}&season={}&episode={}{}'.format(
        API_BASE, imdb, s, e, duration_q), 'imdb'


def _query_primary(tmdb_id, imdb_id, season, episode, is_movie, duration_ms) -> Optional[Dict[str, Any]]:
    """Fetch the raw segment payload from TheIntroDB, or None on miss/error."""
    url, mode = _build_url(tmdb_id, imdb_id, season, episode, is_movie, duration_ms=duration_ms)
    if not url:
        return None

    xbmc.log('[TheIntroDB] TheIntroDB query ({}): {}'.format(mode, url), xbmc.LOGINFO)

    if not _wait_rate_limit(SERVICE_PRIMARY):
        return None

    data = _do_request(url, _get_api_key(), SERVICE_PRIMARY)
    if not data:
        return None
    if 'error' in data:
        xbmc.log('[TheIntroDB] TheIntroDB error: {}'.format(data['error']), xbmc.LOGINFO)
        return None
    return data


def _query_fallback(imdb_id, season, episode, is_movie) -> Optional[Dict[str, Any]]:
    """Fetch from IntroDB.app and normalize it into TheIntroDB's internal shape.

    IntroDB.app only serves TV episodes keyed by IMDb id, and returns a flat
    object with one segment (or null) per type using 'outro' for end credits
    and no 'preview'. We map it back to the list-per-type layout the segment
    processors expect: {'intro': [seg], 'recap': [seg], 'credits': [seg]}.
    """
    if is_movie:
        return None
    imdb = _normalize_imdb(imdb_id)
    if not imdb:
        return None
    s, e = _episode_nums(season, episode)
    if s is None or e is None or s <= 0 or e <= 0:
        return None

    url = '{}/segments?imdb_id={}&season={}&episode={}'.format(API_BASE_FALLBACK, imdb, s, e)
    xbmc.log('[TheIntroDB] IntroDB.app query: {}'.format(url), xbmc.LOGINFO)

    if not _wait_rate_limit(SERVICE_FALLBACK):
        return None

    # Public GET endpoint — no auth (the stored key is TheIntroDB's, wrong format here).
    data = _do_request(url, '', SERVICE_FALLBACK)
    if not isinstance(data, dict):
        return None

    # introdb.app key -> internal segment type
    key_map = (('intro', 'intro'), ('recap', 'recap'), ('outro', 'credits'))
    normalized = {}  # type: Dict[str, Any]
    for src_key, internal_key in key_map:
        seg = data.get(src_key)
        if isinstance(seg, dict):
            # Wrap the single object so it flows through the list-based processors.
            normalized[internal_key] = [seg]
    return normalized


def _resolve_imdb_from_tmdb(tmdb_id: Union[str, int]) -> Optional[str]:
    """Resolve a TV show's TMDb id to its IMDb id via the TMDb API.

    Last resort so the IMDb-only IntroDB.app fallback can run for titles where
    Kodi exposed no IMDb id. Cached (both hits and misses) for the service
    lifetime. Returns a 'tt…' id or None. Not gated by the TheIntroDB rate
    limiter — TMDb is a separate host and this fires at most once per show.
    """
    tid = str(tmdb_id).strip()
    if tid in _tmdb_imdb_cache:
        return _tmdb_imdb_cache[tid]

    imdb = None
    url = '{}/tv/{}/external_ids?api_key={}'.format(TMDB_API_BASE, tid, TMDB_API_KEY)
    xbmc.log('[TheIntroDB] TMDb external_ids lookup for tv/{}'.format(tid), xbmc.LOGINFO)
    try:
        req = Request(url)
        req.add_header('Accept', 'application/json')
        req.add_header('User-Agent', 'TheIntroDB Kine Addon/1.0')
        resp = urlopen(req, timeout=8)
        data = json.loads(resp.read().decode('utf-8'))
        candidate = data.get('imdb_id') if isinstance(data, dict) else None
        if candidate and str(candidate).startswith('tt'):
            imdb = str(candidate)
            xbmc.log('[TheIntroDB] TMDb resolved tv/{} -> {}'.format(tid, imdb), xbmc.LOGINFO)
        else:
            xbmc.log('[TheIntroDB] TMDb has no IMDb id for tv/{}'.format(tid), xbmc.LOGINFO)
    except HTTPError as e:
        xbmc.log('[TheIntroDB] TMDb HTTP {}'.format(e.code), xbmc.LOGWARNING)
    except URLError as e:
        xbmc.log('[TheIntroDB] TMDb network error: {}'.format(e.reason), xbmc.LOGWARNING)
    except Exception as e:
        xbmc.log('[TheIntroDB] TMDb lookup failed: {}'.format(e), xbmc.LOGERROR)

    _tmdb_imdb_cache[tid] = imdb
    return imdb


def _request_media(tmdb_id: Optional[Union[str, int]], imdb_id: Optional[str], season: Optional[Union[str, int]], episode: Optional[Union[str, int]], is_movie: bool, duration_ms: Optional[Union[str, int]], want_types: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
    """Fetch segment data, merging BOTH crowd-sourced databases.

    Neither TheIntroDB nor IntroDB.app is authoritative (both are user-submitted),
    so we query both and combine their candidates per type — tagged with 'source'
    — rather than letting one short-circuit the other. IntroDB.app is TV-only and
    keyed by IMDb id, so movies use only TheIntroDB; when Kodi exposed no IMDb id
    we resolve the show's from its TMDb id so the fallback can still run.
    Plausibility and consensus selection happen in _pick_best_segments_all_types.
    Returns the merged {type: [candidate, ...]} or None."""
    primary = _query_primary(tmdb_id, imdb_id, season, episode, is_movie, duration_ms)

    fallback = None
    if not is_movie:
        fb_imdb = imdb_id
        if not _normalize_imdb(fb_imdb) and tmdb_id and _valid_tmdb(tmdb_id):
            s, e = _episode_nums(season, episode)
            if s is not None and e is not None and s > 0 and e > 0:
                fb_imdb = _resolve_imdb_from_tmdb(tmdb_id)
        fallback = _query_fallback(fb_imdb, season, episode, is_movie)

    return _merge_sources(primary, fallback, want_types)


def _merge_sources(primary: Optional[Dict[str, Any]], fallback: Optional[Dict[str, Any]],
                   want_types: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
    """Combine candidates from both databases per type, tagging each with its source."""
    merged = {}  # type: Dict[str, List[Dict[str, Any]]]
    for source_name, src in (('theintrodb', primary), ('introdb.app', fallback)):
        if not isinstance(src, dict):
            continue
        for seg_type in want_types:
            for seg in (src.get(seg_type) or []):
                if not isinstance(seg, dict):
                    continue
                tagged = dict(seg)
                tagged['source'] = source_name
                merged.setdefault(seg_type, []).append(tagged)
    if merged and _debug_logging():
        for seg_type, segs in merged.items():
            xbmc.log('[TheIntroDB] Merged {}: {} candidate(s) from {}'.format(
                seg_type, len(segs), [s.get('source') for s in segs]), xbmc.LOGINFO)
    return merged or None


def query_intro(tmdb_id=None, imdb_id=None, season=None, episode=None, is_movie=False, duration_ms: Optional[Union[str, int]] = None):
    # returns intro start/end in seconds, or none
    if not _is_enabled():
        return None, None

    url, _ = _build_url(tmdb_id, imdb_id, season, episode, is_movie, duration_ms=duration_ms)
    if not url:
        if tmdb_id or imdb_id:
            xbmc.log(
                '[TheIntroDB] TheIntroDB: need TMDB id, or IMDb tt… id with season/episode for TV',
                xbmc.LOGINFO,
            )
        else:
            xbmc.log('[TheIntroDB] TheIntroDB: no TMDB or IMDb id', xbmc.LOGINFO)
        return None, None

    data = _request_media(tmdb_id, imdb_id, season, episode, is_movie, duration_ms,
                          want_types=('intro',))
    if not data:
        return None, None

    intro_start, intro_end = _pick_best_segment(data.get('intro', []))
    if intro_start is not None:
        xbmc.log('[TheIntroDB] TheIntroDB intro: {:.1f}s -> {:.1f}s'.format(
            intro_start, intro_end), xbmc.LOGINFO)
    else:
        xbmc.log('[TheIntroDB] TheIntroDB: no usable intro segment', xbmc.LOGINFO)

    return intro_start, intro_end


def submit_segment(tmdb_id: Optional[Union[str, int]] = None, imdb_id: Optional[str] = None, season: Optional[Union[str, int]] = None, episode: Optional[Union[str, int]] = None,
                    is_movie: bool = False, segment: str = 'intro', start_sec: Optional[float] = None, end_sec: Optional[float] = None, video_duration_ms: Optional[Union[str, int]] = None) -> Tuple[bool, str]:
    """Submit a segment timestamp to TheIntroDB.

    Returns (success, message) tuple.
    """
    api_key = _get_api_key()
    if not api_key:
        return False, 'API key required for submissions. Set it in addon settings.'

    if not tmdb_id or not _valid_tmdb(tmdb_id):
        # fall back to imdb_id if we have one — the API can resolve it
        if not _normalize_imdb(imdb_id):
            return False, 'Need a TMDB or IMDb ID to submit.'

    if not _wait_rate_limit(SERVICE_PRIMARY):
        return False, 'Rate limited. Try again later.'

    payload = {
        'segment': segment,
    }

    if tmdb_id and _valid_tmdb(tmdb_id):
        payload['tmdb_id'] = int(str(tmdb_id).strip())

    imdb = _normalize_imdb(imdb_id)
    if imdb:
        payload['imdb_id'] = imdb

    if is_movie:
        payload['type'] = 'movie'
    else:
        payload['type'] = 'tv'
        s, e = _episode_nums(season, episode)
        if s is None or e is None:
            return False, 'Season and episode are required for TV submissions.'
        payload['season'] = s
        payload['episode'] = e

    if start_sec is not None:
        payload['start_sec'] = round(float(start_sec), 1)
    else:
        payload['start_sec'] = None
    if end_sec is not None:
        payload['end_sec'] = round(float(end_sec), 1)
    else:
        payload['end_sec'] = None
    
    try:
        if video_duration_ms is not None:
            dur_int = int(video_duration_ms)
            if dur_int == 0 or (300000 <= dur_int <= 21600000):
                payload['video_duration_ms'] = dur_int
    except (TypeError, ValueError):
        pass

    url = '{}/submit'.format(API_BASE)
    xbmc.log('[TheIntroDB] Submitting segment: {} -> {}'.format(url, payload), xbmc.LOGINFO)

    body_bytes = json.dumps(payload).encode('utf-8')
    req = Request(url, data=body_bytes, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Accept', 'application/json')
    req.add_header('User-Agent', 'TheIntroDB Kine Addon/1.0')
    req.add_header('Authorization', 'Bearer {}'.format(api_key))

    try:
        resp = urlopen(req, timeout=10)
        resp_body = resp.read().decode('utf-8')
        data = json.loads(resp_body)
        _log_resp(resp_body)
        if data.get('ok') or data.get('submissions') or data.get('submission'):
            submissions = data.get('submissions') or []
            if submissions and isinstance(submissions, list) and isinstance(submissions[0], dict):
                status = submissions[0].get('status', 'pending')
                return True, 'Submitted! Status: {}'.format(status)
            status = (data.get('submission') or {}).get('status', 'pending')
            if status:
                return True, 'Submitted! Status: {}'.format(status)
        return True, 'Submitted successfully.'
    except HTTPError as e:
        try:
            err_body = e.read().decode('utf-8')
            err_data = json.loads(err_body)
            err_msg = err_data.get('error', 'HTTP {}'.format(e.code))
        except Exception:
            err_msg = 'HTTP {}'.format(e.code)
        if e.code == 429:
            retry = 300
            for header in ('X-UsageLimit-Reset', 'X-RateLimit-Reset', 'Retry-After'):
                val = e.headers.get(header)
                if val:
                    try:
                        retry = int(val)
                    except ValueError:
                        pass
                    break
            _mark_rate_limited(SERVICE_PRIMARY, retry)
        xbmc.log('[TheIntroDB] Submit failed: {}'.format(err_msg), xbmc.LOGWARNING)
        return False, err_msg
    except URLError as e:
        xbmc.log('[TheIntroDB] Submit network error: {}'.format(e.reason), xbmc.LOGWARNING)
        return False, 'Network error: {}'.format(e.reason)
    except Exception as e:
        xbmc.log('[TheIntroDB] Submit error: {}'.format(e), xbmc.LOGERROR)
        return False, str(e)


def query_all_segments(tmdb_id: Optional[Union[str, int]] = None, imdb_id: Optional[str] = None, season: Optional[Union[str, int]] = None, episode: Optional[Union[str, int]] = None, is_movie: bool = False, duration_ms: Optional[Union[str, int]] = None) -> Dict[str, Any]:
    # returns dict with all segment types and their segments
    if not _is_enabled():
        return {}

    url, _ = _build_url(tmdb_id, imdb_id, season, episode, is_movie, duration_ms=duration_ms)
    if not url:
        if tmdb_id or imdb_id:
            xbmc.log(
                '[TheIntroDB] TheIntroDB: need TMDB id, or IMDb tt… id with season/episode for TV',
                xbmc.LOGINFO,
            )
        else:
            xbmc.log('[TheIntroDB] TheIntroDB: no TMDB or IMDb id', xbmc.LOGINFO)
        return {}

    data = _request_media(tmdb_id, imdb_id, season, episode, is_movie, duration_ms,
                          want_types=SEGMENT_TYPES)
    if not data:
        return {}

    # Process all segment types
    all_segments = {}
    
    # Debug: Log what the API actually returned (only if debug logging is enabled)
    if _debug_logging():
        xbmc.log('[TheIntroDB] API response keys: {}'.format(list(data.keys())), xbmc.LOGINFO)
        xbmc.log('[TheIntroDB] Full API response (first 500 chars): {}'.format(str(data)[:500]), xbmc.LOGINFO)
        for key in data.keys():
            if key in ['intro', 'recap', 'credits', 'preview']:
                xbmc.log('[TheIntroDB] API {} raw data: {}'.format(key, len(data.get(key, []))), xbmc.LOGINFO)
    
    for seg_type in SEGMENT_TYPES:
        raw_segments = data.get(seg_type, [])
        if _debug_logging():
            xbmc.log('[TheIntroDB] Processing {}: {} raw segments'.format(seg_type, len(raw_segments)), xbmc.LOGINFO)
        segments = _pick_best_segments_all_types(raw_segments, seg_type, duration_ms)
        if segments:
            all_segments[seg_type] = segments
            if _debug_logging():
                xbmc.log('[TheIntroDB] TheIntroDB {}: {} valid segments'.format(seg_type, len(segments)), xbmc.LOGINFO)
        else:
            if _debug_logging():
                xbmc.log('[TheIntroDB] TheIntroDB {}: no valid segments'.format(seg_type), xbmc.LOGINFO)
    
    if _debug_logging():
        xbmc.log('[TheIntroDB] Final segments dict: {}'.format(list(all_segments.keys())), xbmc.LOGINFO)
    return all_segments
