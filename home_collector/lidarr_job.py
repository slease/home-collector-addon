#!/usr/bin/env python3
"""The Home collector's Lidarr job (roadmap R-0479 "Lidarr for music", step 1.1, add-on 1.0.51).
collector.py's JOBS entry 'lidarr' calls run() from its loop; this module keeps its post to at most hourly itself.

Read only. It asks Lidarr on the house network and posts one report to theleases.org (src/lidarr.js):
  every hour  POST <site>/music/lidarr  { v: 1, artists?: [row], wanted?: [row], queue?: [row] }
    artists  every artist in Lidarr: lidarr_id, name, mbid (MusicBrainz id), status (continuing or ended), monitored, albums,
             tracks (tracks with a file), total_tracks, size_bytes, added
    wanted   Lidarr's monitored missing albums, newest release first, at most MAX_WANTED: album_id, artist_id, artist, title,
             type (Album, EP, Single ...), release (a day)
    queue    Lidarr's download queue, a row per album: album_id, artist_id, artist, title, percent (0 to 100), status, eta
  A part that could not be read is left out of the post (the site keeps what it had for that part); a failed artist read sends
  nothing at all.
No action is ever taken in Lidarr here: every call in this job is a GET (the owner allowed adds and removes on 11/20/26 with the same
rules as Radarr and Sonarr; those come with the request steps, R-0479 phase 2, never here). The Lidarr key and address live only in
the add-on's options and are never sent to the site or printed: no line this job writes holds either. No file path, quality,
release group, indexer or download client leaves this machine.

Python 3 standard library only. The Lidarr v1 shapes (/api/v1/artist with statistics, /api/v1/wanted/missing and /api/v1/queue,
both paged with records) are from memory of its API, NOT checked against a live server; every field is read as optional.

Entry point: run(cfg, now, http=None) -> the new state dict (collector.py saves it to /data/jobs/lidarr.json). cfg is the add-on's
options (push_url, lidarr_url, lidarr_key) plus cfg['state']. http(method, url, headers=None, body=None, timeout=20) ->
(status, bytes), collector.py's own; injectable for tests.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

EVERY_S = 60 * 60
MAX_ARTISTS = 3000
MAX_WANTED = 300
MAX_QUEUE = 200
UA = 'lease-home-collector/lidarr-1'
STATUS = ('continuing', 'ended')
QUEUE_STATUS = ('queued', 'paused', 'downloading', 'completed', 'delay', 'downloadclientunavailable', 'fallback', 'failed', 'warning')
MBID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


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


def _int(v, lo=0, hi=10 ** 15):
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


def _text(v, n):
    return ' '.join(str(v).split())[:n] if isinstance(v, str) else ''


def _iso(v):
    """An ISO time from Lidarr as UTC to the second (2026-11-28T14:03:00Z), else None."""
    if not isinstance(v, str) or len(v) < 10:
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    if d.year < 1900 or d.year > 2100:
        return None
    return d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _day(v):
    t = _iso(v)
    return t[:10] if t else None


def artist_rows(artists):
    """Lidarr's /api/v1/artist to the report's rows, by name, at most MAX_ARTISTS. Names, counts and the MusicBrainz id only. Pure."""
    out = []
    for a in artists if isinstance(artists, list) else []:
        if not isinstance(a, dict):
            continue
        aid, name = _int(a.get('id'), 1), _text(a.get('artistName'), 200)
        if not aid or not name:
            continue
        st = a.get('statistics') if isinstance(a.get('statistics'), dict) else {}
        mb = str(a.get('foreignArtistId') or '').lower()
        status = a.get('status') if a.get('status') in STATUS else None
        out.append({'lidarr_id': aid, 'name': name, 'mbid': mb if MBID.match(mb) else None, 'status': status, 'monitored': a.get('monitored') is True,
                    'albums': _int(st.get('albumCount'), 0, 100000), 'tracks': _int(st.get('trackFileCount'), 0, 1000000),
                    'total_tracks': _int(st.get('totalTrackCount'), 0, 1000000), 'size_bytes': _int(st.get('sizeOnDisk')), 'added': _iso(a.get('added'))})
    out.sort(key=lambda r: (r['name'].lower(), r['lidarr_id']))
    return out[:MAX_ARTISTS]


def _records(page):
    return page.get('records') if isinstance(page, dict) and isinstance(page.get('records'), list) else None


def _artist_name(r):
    art = r.get('artist') if isinstance(r.get('artist'), dict) else {}
    return _text(art.get('artistName'), 200) or None


def wanted_rows(records):
    """Lidarr's missing albums to the report's rows, newest release first, at most MAX_WANTED. Pure."""
    out = []
    for r in records if isinstance(records, list) else []:
        if not isinstance(r, dict):
            continue
        alb, title = _int(r.get('id'), 1), _text(r.get('title'), 200)
        if not alb or not title:
            continue
        out.append({'album_id': alb, 'artist_id': _int(r.get('artistId'), 1), 'artist': _artist_name(r), 'title': title,
                    'type': _text(r.get('albumType'), 30) or None, 'release': _day(r.get('releaseDate'))})
    out.sort(key=lambda x: (x['release'] or '', x['album_id']), reverse=True)
    return out[:MAX_WANTED]


def queue_rows(records):
    """Lidarr's download queue to one row per album, percent done from size and sizeleft, at most MAX_QUEUE. No download client,
    indexer, path, quality or error text. Pure."""
    rows = {}
    for r in records if isinstance(records, list) else []:
        if not isinstance(r, dict):
            continue
        alb = _int(r.get('albumId'), 1)
        album = r.get('album') if isinstance(r.get('album'), dict) else {}
        title = _text(album.get('title'), 200)
        if not alb or not title:
            continue
        size, left = r.get('size'), r.get('sizeleft')
        pct = None
        if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0 and isinstance(left, (int, float)) and not isinstance(left, bool):
            pct = max(0, min(100, int(round((size - left) * 100.0 / size))))
        status = str(r.get('status') or '').lower()
        row = rows.get(alb)
        if row is None:   # a release in several downloads: one row, the least done
            rows[alb] = {'album_id': alb, 'artist_id': _int(r.get('artistId'), 1), 'artist': _artist_name(r), 'title': title,
                         'percent': pct, 'status': status if status in QUEUE_STATUS else None, 'eta': _iso(r.get('estimatedCompletionTime'))}
        elif pct is not None and (row['percent'] is None or pct < row['percent']):
            row['percent'] = pct
    out = sorted(rows.values(), key=lambda x: (x['artist'] or '', x['title']))
    return out[:MAX_QUEUE]


def run(cfg, now, http=None):
    """One pass from the add-on's loop. Returns the new state; its 'log' holds this pass's lines (no secret in any of them)."""
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    state['log'] = _pass(cfg, now, _Net(http or default_http), state)
    for line in state['log']:
        print('[home-collector] ' + line, flush=True)
    if state['log']:
        cfg['outcome'] = state['log'][-1]
        if any(('nothing sent' in x) or ('site answered' in x and not x.endswith(' 200')) for x in state['log']):
            cfg['failed'] = True
    return state


def _pass(cfg, now, http, state):
    site = site_base(cfg)
    base, key = str(cfg.get('lidarr_url') or '').strip().rstrip('/'), str(cfg.get('lidarr_key') or '').strip()
    if not site:
        return ['lidarr: no site link in the options']
    if not (base and key):
        return ['lidarr: Lidarr not set in the options']
    if not (base.startswith('http://') or base.startswith('https://')):
        return ['lidarr: lidarr_url must start with http:// or https://; nothing sent']
    if now - float(state.get('lidarr_at') or 0) < EVERY_S:
        return []
    state['lidarr_at'] = now   # stamped before the work, so a failing run also waits
    hdr = {'X-Api-Key': key}
    get = lambda path: _json(*http.request('GET', base + path, hdr, None))
    artists = get('/api/v1/artist')
    if not isinstance(artists, list):
        return ['lidarr: Lidarr did not answer the artist list; nothing sent']
    body = {'v': 1, 'artists': artist_rows(artists)}
    lines = []
    wanted = _records(get('/api/v1/wanted/missing?' + urllib.parse.urlencode({'page': 1, 'pageSize': MAX_WANTED, 'sortKey': 'releaseDate',
                                                                                 'sortDirection': 'descending', 'includeArtist': 'true', 'monitored': 'true'})))
    if wanted is not None:
        body['wanted'] = wanted_rows(wanted)
    else:
        lines.append('lidarr: wanted albums not read; not sent')
    queue = _records(get('/api/v1/queue?' + urllib.parse.urlencode({'page': 1, 'pageSize': MAX_QUEUE, 'includeArtist': 'true', 'includeAlbum': 'true'})))
    if queue is not None:
        body['queue'] = queue_rows(queue)
    else:
        lines.append('lidarr: the queue not read; not sent')
    status = http.request('POST', site + '/music/lidarr', {'Content-Type': 'application/json'}, json.dumps(body).encode('utf-8'))[0]
    lines.append('lidarr: sent %d artists, %s wanted albums, %s queued, site answered %d' % (
                 len(body['artists']), len(body['wanted']) if 'wanted' in body else 'no', len(body['queue']) if 'queue' in body else 'no', status))
    return lines
