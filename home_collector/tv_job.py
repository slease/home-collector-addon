#!/usr/bin/env python3
"""The Home collector's TV job (roadmap R-0412 "Plex page", step 1.1, and R-0406 step 2.1;, approved 10/29/26).
collector.py's JOBS entry 'tv' calls run() from its loop; this module keeps its post to at most hourly itself.

Read only. It asks Sonarr on the house network and posts one report to theleases.org (src/plex.js):
  every hour  POST <site>/tv/library  { v: 1, series: [row], new?: [episode], soon?: [episode] }
    series  every show in Sonarr: sonarr_id, tvdb_id, title, year, network, status, seasons, files, episodes, size_bytes,
            rating, poster (an https TVDB or TMDb image address only), added, monitored
    new     episodes FIRST imported in the past NEW_DAYS days (Sonarr's history): series_id, season, episode, title, air_date,
            imported_at. An upgrade never counts: an episode with an earlier import in its show's history, an episodeFileDeleted
            marked Upgrade, or a show whose history could not be read is left out (only a first import is announced).
    soon    Sonarr's calendar for the next SOON_DAYS days: monitored, not aired yet, no file: series_id, season, episode, title,
            air_at
  A part that could not be read is left out of the post (the site then keeps what it had for that part); a failed series read
  sends nothing at all. From 1.0.12 series holds only the shows whose hash changed since the last good post, marked
  partial: true (the site removes no show for it), with a full list at least once a day and after any failed post.
No action is ever taken in Sonarr: every call here is a GET. The Sonarr key and address live only in the add-on's options and are
never sent to the site; no file path, quality, release group, indexer or download client leaves this machine.

Python 3 standard library only. The Sonarr v3 shapes (/api/v3/series with statistics and images, /api/v3/history/since with
includeEpisode, /api/v3/history/series, /api/v3/calendar) are from memory of its API, NOT checked against a live server; every
field is read as optional.

Entry point: run(cfg, now, http=None) -> the new state dict (collector.py saves it to /data/jobs/tv.json). cfg is the add-on's
options (push_url, sonarr_url, sonarr_key) plus cfg['state']. http(method, url, headers=None, body=None, timeout=20) ->
(status, bytes), collector.py's own; injectable for tests.
"""
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

EVERY_S = 60 * 60
NEW_DAYS = 8          # the site shows a week; a day more so an hour's gap never loses one
SOON_DAYS = 21
MAX_SERIES = 2000
MAX_NEW = 1500
MAX_SOON = 600
MAX_SHOW_HISTORY = 60  # shows whose full history is read in one run; any past that are left out of new (conservative)
UA = 'lease-home-collector/tv-1'
FIRST = ('downloadFolderImported', 'episodeFileImported')                        # a download's first import counts
IMPORTS = FIRST + ('seriesFolderImported',)                                      # any of these earlier means not new
POSTER_HOSTS = ('https://artworks.thetvdb.com/banners/', 'https://image.tmdb.org/t/p/')
PATH_OK = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-/.')
STATUS = ('continuing', 'ended', 'upcoming', 'deleted')


def default_http(method, url, headers=None, body=None, timeout=20):
    """The real network when collector.py passes none: urllib; an HTTP error is a status, not an exception."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(16 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        return e.code, e.read(4096)


class _Net:
    def __init__(self, fn):
        self.fn = fn

    def request(self, method, url, headers=None, body=None):
        return self.fn(method, url, dict({'User-Agent': UA, 'Accept': 'application/json'}, **(headers or {})), body)


def _json(status, data):
    if status < 200 or status >= 300:
        return None
    try:
        return json.loads((data or b'null').decode('utf-8'))
    except ValueError:
        return None


def site_base(cfg):
    s = str(cfg.get('push_url') or '').rstrip('/')
    if s.endswith('/push'):
        s = s[:-5]
    return s if s.startswith('https://') and '/collector/' in s else ''


def _int(v, lo=1, hi=10 ** 10):
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


def _text(v, n):
    return ' '.join(str(v).split())[:n] if isinstance(v, str) else ''


def _iso(v):
    """An ISO time from Sonarr as UTC to the second (2026-10-29T14:03:00Z), else None."""
    if not isinstance(v, str) or len(v) < 10:
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    if d.year < 1950 or d.year > 2100:
        return None
    return d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _day(v):
    return v if isinstance(v, str) and len(v) == 10 and v[4] == '-' and v[7] == '-' and v[:4].isdigit() and v[5:7].isdigit() and v[8:].isdigit() else None


def poster(images):
    """The poster's https address on TVDB or TMDb from Sonarr's images (remoteUrl only, never Sonarr's own copy), else None."""
    for im in images if isinstance(images, list) else []:
        if not isinstance(im, dict) or im.get('coverType') != 'poster':
            continue
        u = str(im.get('remoteUrl') or '').split('?')[0]
        host = next((h for h in POSTER_HOSTS if u.startswith(h)), None)
        if not host:
            continue
        rest = u[len(host):]
        if 4 <= len(rest) <= 160 and '..' not in rest and '//' not in rest and all(c in PATH_OK for c in rest) and rest.rsplit('.', 1)[-1].lower() in ('jpg', 'jpeg', 'png'):
            return u
    return None


def series_rows(items):
    """Sonarr /api/v3/series to the site's rows: the fourteen fields only. A row with no id or title is left out. Pure."""
    out = []
    for s in items if isinstance(items, list) else []:
        if not isinstance(s, dict):
            continue
        sid, title = _int(s.get('id')), _text(s.get('title'), 200)
        if not (sid and title):
            continue
        st = s.get('statistics') if isinstance(s.get('statistics'), dict) else {}
        r = s.get('ratings') if isinstance(s.get('ratings'), dict) else {}
        rv = r.get('value')
        status = str(s.get('status') or '').lower()
        net = _text(s.get('network'), 60)
        out.append({'sonarr_id': sid, 'tvdb_id': _int(s.get('tvdbId')), 'title': title, 'year': _int(s.get('year'), 1900, 2100),
                    'network': net or None, 'status': status if status in STATUS else None,
                    'seasons': _int(st.get('seasonCount'), 0, 500), 'files': _int(st.get('episodeFileCount'), 0, 100000),
                    'episodes': _int(st.get('episodeCount'), 0, 100000), 'size_bytes': _int(st.get('sizeOnDisk'), 0, 10 ** 15),
                    'rating': round(float(rv), 1) if isinstance(rv, (int, float)) and not isinstance(rv, bool) and 0 < rv <= 10 else None,
                    'poster': poster(s.get('images')), 'added': _iso(s.get('added')), 'monitored': s.get('monitored') is True})
    return out[:MAX_SERIES]


def _ep(rec):
    e = rec.get('episode') if isinstance(rec.get('episode'), dict) else {}
    return e


def _is_upgrade(rec):
    d = rec.get('data') if isinstance(rec.get('data'), dict) else {}
    return str(d.get('reason') or '').lower() == 'upgrade'


def _plus(at, seconds):
    return (datetime.strptime(at, '%Y-%m-%dT%H:%M:%SZ') + timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')


def candidates(history, since_iso):
    """History since the window's start to {episodeId: first import record in the window}, minus any episode the window itself
    marks as an upgrade at or before that import (an episodeFileDeleted with reason Upgrade, or an import marked so: Sonarr
    writes the deletion as the better copy is imported, so one within a minute of the import means it replaced a file). An
    upgrade later than that leaves the first import standing. Pure."""
    first, upgraded = {}, {}
    for h in history if isinstance(history, list) else []:
        if not isinstance(h, dict):
            continue
        eid, at = _int(h.get('episodeId')), _iso(h.get('date'))
        if not eid or not at:
            continue
        ev = str(h.get('eventType') or '')
        if _is_upgrade(h):
            upgraded[eid] = min(upgraded.get(eid, at), at)
            continue
        if ev in FIRST and at >= since_iso:
            if eid not in first or at < first[eid]['_at']:
                first[eid] = dict(h, _at=at)
    return {k: v for k, v in first.items() if not (k in upgraded and upgraded[k] <= _plus(v['_at'], 60))}


def earlier_import(show_history, eid, at):
    """True when the show's own history has an import of this episode before `at`, or an upgrade at or before it (within a
    minute after counts: the deletion is written as the better copy lands). A record with no readable time counts as earlier. Pure."""
    for h in show_history if isinstance(show_history, list) else []:
        if not isinstance(h, dict) or _int(h.get('episodeId')) != eid:
            continue
        t = _iso(h.get('date'))
        if _is_upgrade(h) and (not t or t <= _plus(at, 60)):
            return True
        if str(h.get('eventType') or '') in IMPORTS and (not t or t < at):
            return True
    return False


def new_episodes(history, show_histories, since_iso):
    """The first-time imports to the site's rows. show_histories: {series id: that show's full history, or None when it could
    not be read}; a show missing or None is left out (conservative: never announce what might be an upgrade). Pure."""
    out = []
    for eid, h in sorted(candidates(history, since_iso).items(), key=lambda kv: kv[1]['_at']):
        sid = _int(h.get('seriesId'))
        hist = show_histories.get(sid) if sid else None
        if hist is None or earlier_import(hist, eid, h['_at']):
            continue
        e = _ep(h)
        season, num = _int(e.get('seasonNumber'), 0, 500), _int(e.get('episodeNumber'), 0, 5000)
        if season is None or num is None:
            continue
        out.append({'series_id': sid, 'season': season, 'episode': num, 'title': _text(e.get('title'), 200) or None,
                    'air_date': _day(e.get('airDate')), 'imported_at': h['_at']})
    return out[:MAX_NEW]


def soon_rows(items, now_iso, end_iso):
    """Sonarr's calendar to the coming episodes: monitored, no file, airing after now and by the end. Pure."""
    out = []
    for e in items if isinstance(items, list) else []:
        if not isinstance(e, dict) or e.get('hasFile') is True or e.get('monitored') is False:
            continue
        sid, at = _int(e.get('seriesId')), _iso(e.get('airDateUtc'))
        season, num = _int(e.get('seasonNumber'), 0, 500), _int(e.get('episodeNumber'), 0, 5000)
        if not sid or not at or season is None or num is None or not (now_iso < at <= end_iso):
            continue
        out.append({'series_id': sid, 'season': season, 'episode': num, 'title': _text(e.get('title'), 200) or None, 'air_at': at})
    out.sort(key=lambda x: (x['air_at'], x['series_id'], x['season'], x['episode']))
    return out[:MAX_SOON]


def _stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def run(cfg, now, http=None):
    """One pass from the add-on's loop. Returns the new state; its 'log' holds this pass's lines (no secret in any of them)."""
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    state['log'] = _pass(cfg, now, _Net(http or default_http), state)
    for line in state['log']:
        print('[home-collector] ' + line, flush=True)
    return state


def _pass(cfg, now, http, state):
    site = site_base(cfg)
    base, key = str(cfg.get('sonarr_url') or '').rstrip('/'), str(cfg.get('sonarr_key') or '')
    if not site:
        return ['tv: no site link in the options']
    if not (base and key):
        return ['tv: Sonarr not set in the options']
    if now - float(state.get('tv_at') or 0) < EVERY_S:
        return []
    state['tv_at'] = now   # stamped before the work, so a failing run also waits
    hdr = {'X-Api-Key': key}
    get = lambda path: _json(*http.request('GET', base + path, hdr, None))
    series = get('/api/v3/series')
    if not isinstance(series, list):
        return ['tv: Sonarr did not answer the series list; nothing sent']
    body = {'v': 1, 'series': series_rows(series)}
    lines = []
    since = _stamp(now - NEW_DAYS * 86400)
    hist = get('/api/v3/history/since?' + urllib.parse.urlencode({'date': since, 'includeEpisode': 'true', 'includeSeries': 'false'}))
    if isinstance(hist, list):
        shows = sorted({_int(h.get('seriesId')) for h in candidates(hist, since).values() if _int(h.get('seriesId'))})
        histories = {}
        for sid in shows[:MAX_SHOW_HISTORY]:
            got = get('/api/v3/history/series?' + urllib.parse.urlencode({'seriesId': sid, 'includeEpisode': 'false', 'includeSeries': 'false'}))
            histories[sid] = got if isinstance(got, list) else None
        body['new'] = new_episodes(hist, histories, since)
        if len(shows) > MAX_SHOW_HISTORY:
            lines.append('tv: %d shows had imports; the first %d were checked, the rest wait' % (len(shows), MAX_SHOW_HISTORY))
    else:
        lines.append('tv: Sonarr history not read; new episodes not sent')
    cal = get('/api/v3/calendar?' + urllib.parse.urlencode({'start': _stamp(now), 'end': _stamp(now + SOON_DAYS * 86400), 'unmonitored': 'false',
                                                               'includeSeries': 'false', 'includeEpisodeFile': 'false'}))
    if isinstance(cal, list):
        body['soon'] = soon_rows(cal, _stamp(now), _stamp(now + SOON_DAYS * 86400))
    else:
        lines.append('tv: Sonarr calendar not read; coming soon not sent')
    # 1.0.12: only the shows that changed since the last good post (partial: the site removes no show), with a full post once
    # a day, so the site can drop a show Sonarr no longer has, and whenever no hashes are kept (a first run, a failed post)
    rows = body['series']
    full = not state.get('series_hashes') or now - float(state.get('tv_full_at') or 0) >= FULL_EVERY_S
    diff, hashes = changed(rows, state.get('series_hashes'), 'sonarr_id')
    if not full:
        body['series'], body['partial'] = diff, True
    post = lambda b: http.request('POST', site + '/tv/library', {'Content-Type': 'application/json'}, json.dumps(b).encode('utf-8'))[0]
    status = post(body)
    if status == 400 and not full:   # a site before 1.0.12 does not know partial: the post as 1.0.11 sent it
        body.pop('partial', None)
        body['series'], full = rows, True
        status = post(body)
    if status == 200:
        state['series_hashes'] = hashes
        if full:
            state['tv_full_at'] = now
    else:
        state.pop('series_hashes', None)   # the next post is whole
    lines.append('tv: sent %s, %s new episodes, %s coming, site answered %d' % (
                 ('%d shows' % len(rows)) if full else ('%d of %d shows changed' % (len(body['series']), len(rows))),
                 len(body['new']) if 'new' in body else 'no', len(body['soon']) if 'soon' in body else 'no', status))
    return lines


FULL_EVERY_S = 24 * 3600


def changed(rows, hashes, key):
    """(the rows whose hash is not the one kept from the last good post, every row's new hash by its key). Pure."""
    now_h = {str(r[key]): hashlib.sha1(json.dumps(r, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()[:16] for r in rows}
    return [r for r in rows if (hashes or {}).get(str(r[key])) != now_h[str(r[key])]], now_h
