#!/usr/bin/env python3
"""The Home collector's download queue job (add-on 1.0.25; roadmap R-0484 "Movies and TV Watch tab becomes the Now view", step
3.1, 11/8/26: "ship queue totals now", then a row per title). collector.py's JOBS entry 'queue' calls run() every minute
while push_url is set; it reads Sonarr and Radarr only when their options are set.

Read only. Each run asks, on the house network:
  Sonarr  GET <sonarr_url>/api/v3/queue?pageSize=50&includeSeries=true&includeEpisode=true
  Radarr  GET <radarr_url>/api/v3/queue?pageSize=50&includeMovie=true
and posts to theleases.org (src/download-queue.js) when the queue changed since the last post the site took, and at least every
FLOOR_S while anything is in it (QUIET_S while it is empty):
  POST <site>/queue  { v: 1, at, sonarr, radarr, items: [item] }
    sonarr, radarr  'ok', 'off' (its options are blank) or a short reason it did not answer ("HTTP 401", the exception's class name)
    item  app ('sonarr' or 'radarr'), title (the movie, or the show), year (a movie's), season and episode (an episode's numbers),
          pct (0 to 100, whole), size and left (bytes), eta (Unix seconds, or null), state (one of STATES)
At most MAX_ITEMS items, the ones closest to done first. NEVER sent: the release name, the indexer, the download client, a file or
folder path, an address or a key. An older site (404) or a refused post (400) waits BACKOFF_S.

Python 3 standard library only. The queue shape (records[] with size, sizeleft, status, trackedDownloadState,
trackedDownloadStatus, estimatedCompletionTime, series.title, episode.seasonNumber and episodeNumber, movie.title and year) is from
memory of the Sonarr and Radarr v3 APIs, NOT checked against a live server; every field is read as optional.
"""
import json
import re
from datetime import datetime, timezone

READ_TIMEOUT_S = 5   # each Sonarr or Radarr read (1.0.26, bug b-9b7cba8e): a slow app gives up quickly rather than 20 s
UA = 'lease-home-collector/queue-1'
MAX_ITEMS = 40
FLOOR_S = 600          # a busy queue posts at least this often even when nothing moved
QUIET_S = 3600         # an empty queue posts at least hourly, so the site knows the add-on still reads it
BACKOFF_S = 3600
STATES = ('downloading', 'queued', 'paused', 'importing', 'warning', 'failed')
TEXT_BAD = re.compile(r'[<>{}\x00-\x1f]')


def site_base(cfg):
    s = str(cfg.get('push_url') or '').rstrip('/')
    if s.endswith('/push'):
        s = s[:-5]
    return s if re.match(r'^https://theleases\.org/collector/[A-Za-z0-9_-]{20,64}$', s) else ''


def _d(v):
    return v if isinstance(v, dict) else {}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def _text(v, n=120):
    if not isinstance(v, str):
        return None
    t = ' '.join(TEXT_BAD.sub(' ', re.sub(r'<[^>]*>', '', v)).split())[:n]
    return t or None


def _eta(v):
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace('Z', '+00:00'))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return int(t.timestamp())


def state_of(r):
    """One of STATES from a queue record's status words. Pure."""
    st = str(r.get('status') or '').lower()
    td = str(r.get('trackedDownloadState') or '').lower()
    ts = str(r.get('trackedDownloadStatus') or '').lower()
    if st == 'failed' or td in ('failed', 'failedpending'):
        return 'failed'
    if st == 'warning' or ts in ('warning', 'error'):
        return 'warning'
    if td in ('importpending', 'importing', 'imported') or st == 'completed':
        return 'importing'
    if st == 'paused':
        return 'paused'
    if st in ('queued', 'delay'):
        return 'queued'
    return 'downloading'


def item_of(app, r):
    """A Sonarr or Radarr queue record as the site's item, or None. Pure."""
    r = _d(r)
    if app == 'sonarr':
        title = _text(_d(r.get('series')).get('title'))
        ep = _d(r.get('episode'))
        season, episode, year = _num(ep.get('seasonNumber')), _num(ep.get('episodeNumber')), None
    else:
        mv = _d(r.get('movie'))
        title, season, episode, year = _text(mv.get('title')), None, None, _num(mv.get('year'))
    if not title:
        return None
    size, left = _num(r.get('size')), _num(r.get('sizeleft'))
    pct = None
    if size:
        pct = max(0, min(100, int(round((size - min(left if left is not None else size, size)) * 100.0 / size))))
    return {'app': app, 'title': title, 'year': int(year) if year else None,
            'season': int(season) if season is not None else None, 'episode': int(episode) if episode is not None else None,
            'pct': pct, 'size': int(size) if size is not None else None, 'left': int(left) if left is not None else None,
            'eta': _eta(r.get('estimatedCompletionTime')), 'state': state_of(r)}


def items_of(sonarr, radarr):
    """Both queues' records to at most MAX_ITEMS items, the closest to done first. Pure."""
    out = []
    for app, recs in (('sonarr', sonarr), ('radarr', radarr)):
        for r in recs or []:
            it = item_of(app, r)
            if it:
                out.append(it)
    out.sort(key=lambda x: (-(x['pct'] if x['pct'] is not None else -1), x['title'], x['season'] or 0, x['episode'] or 0))
    return out[:MAX_ITEMS]


def reason(status, exc=None):
    if exc is not None:
        return type(exc).__name__[:40]
    return 'HTTP %d' % status


def read_queue(cfg, app, http):
    """(records or None, 'ok' | 'off' | reason). Never raises."""
    base, key = str(cfg.get(app + '_url') or '').rstrip('/'), str(cfg.get(app + '_key') or '')
    if not base or not key:
        return None, 'off'
    q = 'pageSize=50&includeSeries=true&includeEpisode=true' if app == 'sonarr' else 'pageSize=50&includeMovie=true'
    try:
        status, data = http('GET', base + '/api/v3/queue?' + q, {'X-Api-Key': key, 'User-Agent': UA}, None, READ_TIMEOUT_S)
    except (OSError, ValueError) as e:
        return None, reason(0, e)
    if status != 200:
        return None, reason(status)
    try:
        body = json.loads((data or b'null').decode('utf-8'))
    except ValueError as e:
        return None, reason(0, e)
    recs = body.get('records') if isinstance(body, dict) else body if isinstance(body, list) else None
    if not isinstance(recs, list):
        return None, 'not a queue'
    return recs, 'ok'


def run(cfg, now, http):
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    site = site_base(cfg)
    if not site:
        cfg['failed'], cfg['outcome'] = True, 'the site link is missing'
        return state
    if now < float(state.get('off_until') or 0):
        cfg['outcome'] = 'waiting until the site takes the download queue'
        return state
    son, son_said = read_queue(cfg, 'sonarr', http)
    rad, rad_said = read_queue(cfg, 'radarr', http)
    if son_said == 'off' and rad_said == 'off':
        cfg['outcome'] = 'off (no Sonarr or Radarr options)'
        return state
    items = items_of(son, rad)
    body = {'v': 1, 'at': int(now), 'sonarr': son_said, 'radarr': rad_said, 'items': items}
    sig = json.dumps([son_said, rad_said, items], sort_keys=True)
    floor = FLOOR_S if items else QUIET_S
    if sig == state.get('sent_sig') and now - float(state.get('sent_at') or 0) < floor:
        cfg['outcome'] = '%d in the queue, no change' % len(items)
        return state
    try:
        st, data = http('POST', site + '/queue', {'Content-Type': 'application/json', 'User-Agent': UA}, json.dumps(body, separators=(',', ':')).encode('utf-8'), 10)
    except (OSError, ValueError):
        st, data = 0, b''
    if st == 200:
        if len(items) != state.get('sent_n'):
            print('[home-collector] queue: %d in the queue (Sonarr %s, Radarr %s)' % (len(items), son_said, rad_said), flush=True)
        state['sent_sig'], state['sent_at'], state['sent_n'] = sig, now, len(items)
        cfg['outcome'] = '%d in the queue, posted' % len(items)
        return state
    if st in (400, 404):
        state['off_until'] = now + BACKOFF_S
        try:
            msg = str((json.loads((data or b'{}').decode('utf-8')) or {}).get('message', ''))[:160]
        except (ValueError, AttributeError):
            msg = ''
        cfg['failed'], cfg['outcome'] = True, ('the site does not take the download queue yet (404); trying again in an hour' if st == 404
                                               else 'the site refused the post (400): %s; trying again in an hour' % msg)
        return state
    cfg['failed'], cfg['outcome'] = True, 'the site did not answer (HTTP %d); trying again next run' % st
    return state
