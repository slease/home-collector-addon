#!/usr/bin/env python3
"""The Home collector's Plex music library job (add-on 1.0.20; roadmap R-0451 "Plex music library", step 1.1, 11/5/26).
collector.py's JOBS entry 'library' calls run() every 5 minutes while push_url, plex_url and plex_token are set.

Read only. Plex answers only inside the house and only with the token, so the site cannot ask it. This job:
  1. at most every CHECK_S asks Plex for its library sections (GET /library/sections) and keeps the music ones (type artist; on
     the owner's server section 7, "Music", read live 11/5/26). A full read starts when a section's contentChangedAt (else
     updatedAt) moved since the last finished read, at most every CHANGE_GAP_S, and at least every DAILY_S.
  2. reads each music section's artists (type 8), albums (type 9) and tracks (type 10), PAGE rows a request
     (GET /library/sections/<key>/all?type=T&X-Plex-Container-Start=S&X-Plex-Container-Size=PAGE), at most PAGES_PER_RUN pages and
     RUN_BUDGET_S a run with PLEX_GAP_S between requests, so the house's Plex and the main loop are never held for long; a read
     of a few thousand tracks takes 3 or 4 runs. Where it is lives in the job's state, so a restart carries on.
  3. posts each page as one part to the site (src/music-library.js):
       POST <site>/library { v: 1, read, part, kind, rows, sections? (part 0), last?, totals? (the final part) }
     The site checks the shape, upserts the rows, and on the final part replaces the library (rows this read did not send are
     removed and counted). A 409 (out of order, or rows missing) starts the read over; a 404 (an older site) or a refusal (400,
     413) waits an hour; no answer keeps the place and tries again next run.
Facts from the owner's server (11/5/26): albums carry no leafCount, so tracks are counted on the site; a missing viewCount is 0.

Sent, per row, and nothing else (never the token, Plex's address, a file path, a part or a stream):
  artists  key, name, sort, genres, added, plays, last
  albums   key, artist_key, title, artist, year, genres, added, plays, last
  tracks   key, album_key, artist_key, title, artist, disc, n, ms, added, plays, last, format, kbps
  key ids are Plex ratingKeys (digits only); names plain text cut to 200 characters; genres the first 5 Genre tags; added and last
  Unix seconds (addedAt, lastViewedAt); plays viewCount (missing: 0); format Media[0].audioCodec; kbps Media[0].bitrate.
Option: music_library (default true; false turns the job off). Python 3 standard library only.
"""
import json
import re
import time

UA = 'lease-home-collector/library-1'
PAGE = 500
PAGES_PER_RUN = 4
RUN_BUDGET_S = 120
PLEX_GAP_S = 0.5
CHECK_S = 900            # how often the sections are asked whether anything changed
CHANGE_GAP_S = 1800      # a change starts a read at most this often (Plex changes a section many times while it adds an album)
DAILY_S = 86400          # a full read at least this often, so play counts and removals stay current
BACKOFF_S = 3600         # an older site (404) or a refused part (400, 413)
KINDS = (('artists', 8), ('albums', 9), ('tracks', 10))
KEY_RE = re.compile(r'^[0-9]{1,12}$')
FORMAT_RE = re.compile(r'^[a-z0-9]{1,12}$')
MAX_TIME = 4102444800


def site_base(cfg):
    s = str(cfg.get('push_url') or '').rstrip('/')
    if s.endswith('/push'):
        s = s[:-5]
    return s if re.match(r'^https://theleases\.org/collector/[A-Za-z0-9_-]{20,64}$', s) else ''


def _text(v, n=200):
    return ' '.join(str(v or '').replace('\x00', ' ').split())[:n]


def _key(v):
    s = str(v if v is not None else '').strip()
    return s if KEY_RE.match(s) else None


def _int(v, lo, hi):
    if isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _genres(m):
    out = []
    for g in m.get('Genre') if isinstance(m.get('Genre'), list) else []:
        t = _text(g.get('tag') if isinstance(g, dict) else '', 40)
        if t and t not in out:
            out.append(t)
    return out[:5]


def _common(m):
    o = {'added': _int(m.get('addedAt'), 0, MAX_TIME), 'plays': _int(m.get('viewCount'), 0, 10000000) or 0, 'last': _int(m.get('lastViewedAt'), 0, MAX_TIME)}
    return {k: v for k, v in o.items() if v is not None}


def shape(kind, m):
    """One Plex Metadata row as the site's row, or None when it has no usable id. Only the named fields: no file, part or path. Pure."""
    if not isinstance(m, dict):
        return None
    key = _key(m.get('ratingKey'))
    if not key:
        return None
    if kind == 'artists':
        r = {'key': key, 'name': _text(m.get('title')) or 'Unknown artist'}
        sort = _text(m.get('titleSort'))
        if sort and sort != r['name']:
            r['sort'] = sort
        g = _genres(m)
        if g:
            r['genres'] = g
    elif kind == 'albums':
        r = {'key': key, 'title': _text(m.get('title')) or 'Untitled'}
        for f, src in (('artist_key', 'parentRatingKey'),):
            k = _key(m.get(src))
            if k:
                r[f] = k
        a = _text(m.get('parentTitle'))
        if a:
            r['artist'] = a
        y = _int(m.get('year'), 1, 3000)
        if y:
            r['year'] = y
        g = _genres(m)
        if g:
            r['genres'] = g
    else:
        r = {'key': key, 'title': _text(m.get('title')) or 'Untitled'}
        for f, src in (('album_key', 'parentRatingKey'), ('artist_key', 'grandparentRatingKey')):
            k = _key(m.get(src))
            if k:
                r[f] = k
        a = _text(m.get('originalTitle') or m.get('grandparentTitle'))
        if a:
            r['artist'] = a
        for f, src, hi in (('disc', 'parentIndex', 999), ('n', 'index', 9999), ('ms', 'duration', 86400000)):
            v = _int(m.get(src), 0, hi)
            if v is not None:
                r[f] = v
        media = m.get('Media')[0] if isinstance(m.get('Media'), list) and m.get('Media') and isinstance(m.get('Media')[0], dict) else {}
        fmt = str(media.get('audioCodec') or '').strip().lower()
        if FORMAT_RE.match(fmt):
            r['format'] = fmt
        kb = _int(media.get('bitrate'), 1, 100000)
        if kb:
            r['kbps'] = kb
    r.update(_common(m))
    return r


def _get(http, purl, ptok, path):
    """(status, parsed JSON or None). The token in the header only."""
    try:
        status, data = http('GET', purl + path, {'X-Plex-Token': ptok, 'Accept': 'application/json', 'User-Agent': UA}, None, 30)
    except (OSError, ValueError):
        return 0, None
    if status != 200:
        return status, None
    try:
        return status, json.loads((data or b'null').decode('utf-8'))
    except ValueError:
        return status, None


def music_sections(box):
    """[(key, title, changed)] of the music sections in a /library/sections answer. Pure."""
    mc = box.get('MediaContainer') if isinstance(box, dict) else None
    dirs = mc.get('Directory') if isinstance(mc, dict) else None
    out = []
    for d in dirs if isinstance(dirs, list) else []:
        if not isinstance(d, dict) or d.get('type') != 'artist':
            continue
        k = _key(d.get('key'))
        if not k:
            continue
        ch = _int(d.get('contentChangedAt'), 0, 10 ** 12) or _int(d.get('updatedAt'), 0, MAX_TIME) or 0
        out.append((k, _text(d.get('title'), 60) or 'Music', ch))
    return sorted(out)[:10]


def _post(http, site, body):
    try:
        status, data = http('POST', site + '/library', {'Content-Type': 'application/json', 'User-Agent': UA}, json.dumps(body, separators=(',', ':')).encode('utf-8'), 60)
    except (OSError, ValueError):
        return 0, {}
    try:
        ans = json.loads((data or b'{}').decode('utf-8'))
    except ValueError:
        ans = {}
    return status, ans if isinstance(ans, dict) else {}


def say(line):
    print('[home-collector] library: ' + line, flush=True)


def run(cfg, now, http, clock=time.monotonic, sleep=time.sleep):
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    if cfg.get('music_library') is False:
        state.pop('read', None)
        cfg['outcome'] = 'off (music_library is false)'
        return state
    purl, ptok, site = str(cfg.get('plex_url') or '').rstrip('/'), str(cfg.get('plex_token') or ''), site_base(cfg)
    if not (purl and ptok and site):
        cfg['outcome'] = 'nothing sent: push_url is not the site\'s collector link'
        return state
    if now < float(state.get('off_until') or 0):
        cfg['outcome'] = 'waiting until the site takes the library again'
        return state
    rd = state.get('read') if isinstance(state.get('read'), dict) else None
    if rd is None:
        if now - float(state.get('checked_at') or 0) < CHECK_S:
            cfg['outcome'] = 'nothing to do; %s' % last_line(state)
            return state
        state['checked_at'] = now
        st, box = _get(http, purl, ptok, '/library/sections')
        if box is None:
            cfg['failed'], cfg['outcome'] = True, 'Plex did not answer the library sections (HTTP %d); trying again in 15 minutes' % st
            return state
        secs = music_sections(box)
        if not secs:
            cfg['outcome'] = 'no music section in Plex'
            return state
        sig = ','.join('%s:%s' % (k, ch) for k, _, ch in secs)
        last = float(state.get('done_at') or 0)
        why = ('first read' if not last else 'starting over' if state.get('restart') else 'daily read' if now - last >= DAILY_S
               else 'Plex changed the library' if sig != state.get('sig') and now - last >= CHANGE_GAP_S else None)
        if not why:
            cfg['outcome'] = 'nothing changed; %s' % last_line(state)
            return state
        rd = {'id': str(int(now)), 'why': why, 'sig': sig, 'sections': [[k, t] for k, t, _ in secs], 'kind': 0, 'sec': 0, 'start': 0, 'part': 0,
              'seen': {k: [] for k, _ in KINDS}}
        state.pop('restart', None)
        say('reading the music library (%s: %s)' % (why, ', '.join(t for _, t in rd['sections'])))
    t0, pages, posted = clock(), 0, 0
    while pages < PAGES_PER_RUN and clock() - t0 < RUN_BUDGET_S:
        kind, typ = KINDS[rd['kind']]
        sk = rd['sections'][rd['sec']][0]
        if pages:
            sleep(PLEX_GAP_S)
        st, box = _get(http, purl, ptok, '/library/sections/%s/all?type=%d&X-Plex-Container-Start=%d&X-Plex-Container-Size=%d' % (sk, typ, rd['start'], PAGE))
        pages += 1
        mc = box.get('MediaContainer') if isinstance(box, dict) else None
        if not isinstance(mc, dict):
            state['read'] = rd
            cfg['failed'], cfg['outcome'] = True, 'Plex did not answer the %s (HTTP %d); the read carries on next run' % (kind, st)
            return state
        meta = mc.get('Metadata') if isinstance(mc.get('Metadata'), list) else []
        rows, seen = [], set(rd['seen'][kind])
        for m in meta:
            r = shape(kind, m)
            if r and r['key'] not in seen:
                seen.add(r['key'])
                rows.append(r)
        total = _int(mc.get('totalSize'), 0, 10 ** 8)
        end_of_section = len(meta) < PAGE or (total is not None and rd['start'] + len(meta) >= total)
        final = end_of_section and rd['sec'] == len(rd['sections']) - 1 and rd['kind'] == len(KINDS) - 1
        body = {'v': 1, 'read': rd['id'], 'part': rd['part'], 'kind': kind, 'rows': rows}
        if rd['part'] == 0:
            body['sections'] = [t for _, t in rd['sections']]
        if final:
            totals = {k: len(rd['seen'][k]) for k, _ in KINDS}
            totals[kind] = len(seen)
            body['last'], body['totals'] = True, totals
        status, ans = _post(http, site, body)
        if status == 200:
            rd['seen'][kind] = sorted(seen)
            rd['part'] += 1
            posted += len(rows)
            if final:
                state.pop('read', None)
                state.update(done_at=now, sig=rd['sig'], counts=body['totals'], removed=ans.get('removed') if isinstance(ans.get('removed'), dict) else {})
                rm = state['removed']
                gone = ', '.join('%d %s' % (int(rm.get(k) or 0), k) for k, _ in KINDS if int(rm.get(k) or 0))
                cfg['outcome'] = 'read done (%s): %s%s' % (rd['why'], ', '.join('%d %s' % (body['totals'][k], k) for k, _ in KINDS), '; removed ' + gone if gone else '')
                return state
            if end_of_section:
                rd['start'], rd['sec'] = 0, rd['sec'] + 1
                if rd['sec'] >= len(rd['sections']):
                    rd['sec'], rd['kind'] = 0, rd['kind'] + 1
            else:
                rd['start'] += len(meta)
            continue
        msg = str(ans.get('message', ''))[:200]
        if status == 409:   # out of order or rows missing on the site: start over (a new read id) next run
            state.pop('read', None)
            state['checked_at'], state['restart'] = 0, True
            cfg['failed'], cfg['outcome'] = True, 'the site asked to start the read over (409): %s' % msg
            return state
        if status in (400, 404, 413):
            state.pop('read', None)
            state['off_until'], state['restart'], state['checked_at'] = now + BACKOFF_S, True, 0
            cfg['failed'], cfg['outcome'] = True, ('the site does not take the library yet (404); trying again in an hour' if status == 404
                                                   else 'the site refused a part (HTTP %d): %s; trying again in an hour' % (status, msg))
            return state
        state['read'] = rd
        cfg['failed'], cfg['outcome'] = True, 'the site did not answer (HTTP %d); the read carries on next run' % status
        return state
    state['read'] = rd
    cfg['outcome'] = 'reading: %d rows sent this run, part %d next (%s, %s)' % (posted, rd['part'], KINDS[rd['kind']][0], rd['sections'][rd['sec']][1])
    return state


def last_line(state):
    c = state.get('counts') if isinstance(state.get('counts'), dict) else None
    if not c or not state.get('done_at'):
        return 'no read finished yet'
    return 'last read %d artists, %d albums, %d tracks' % (int(c.get('artists') or 0), int(c.get('albums') or 0), int(c.get('tracks') or 0))
