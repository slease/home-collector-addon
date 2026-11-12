#!/usr/bin/env python3
"""The Home collector's Plex music plays job (roadmap R-0018 step 3.5, 10/31/26; moved here from the poster Pi's collector,
tools/poster-collector.py send_plays, which it followed). collector.py's JOBS entry 'plays' calls run() every 5 minutes (every 15
before add-on 1.0.13) while plex_url, plex_token and plex_account_id are set (blank plex_account_id sends nothing).

Read only. It reads Plex's play history on the house network (/status/sessions/history/all) for that ONE account and posts only
track, artist, album, duration_ms and played_at to <site>/plays (src/plex-plays.js checkPlays), 200 a post. The cursor moves
only after the site took the plays. NEVER sent: a player, device, user, address or token.

Add-on 1.0.13 (bug found live 11/1/26: not one Plex play had ever reached the Listening log): the history is no longer asked
for type=10. On the owner's server type=10 makes Plex answer an empty MediaContainer, while the same ask without it gives the owner's
music as type "track" rows mixed with movie and episode rows. So every type is read and plex_plays keeps only that account's
tracks. accountID is still asked (the same ask without type answered his rows, and Plex's own web history filters by it) and
is checked again in code, so a server that ignores it only costs rows. With videos in the same list, the read goes NEWEST first
(sort=viewedAt:desc) and pages back until a page reaches past the cursor, as the movie job's TV plays do since 1.0.12, and the
cursor moves to the newest row of ANY type: a page of movies and episodes then neither hides a track nor gets read again
every run. Plex may ignore the viewedAt>= filter (it is still asked; the stop rule does not need it) or the sort (then every
page up to HIST_PAGES is read and the log says so). The floor is inclusive: the plays at the cursor's own second are kept as
short hashes in state['at_cursor'] so they are not sent again, while a track Plex records at that same second later still goes.
A first run (no cursor) reads the whole history, up to HIST_PAGE x HIST_PAGES rows. A run with nothing new logs
"plays: 0 new (checked N history rows)" at most once an hour, so a silent job can be told from a stuck one.
"""
import datetime as dt
import hashlib
import json
import re
import urllib.parse

HIST_PAGE = 500        # rows a page, of every type (movies and episodes ride along since 1.0.13)
HIST_PAGES = 100       # 50,000 rows at most a run: a first run reads the whole history, a later one stops past the cursor
PLAYS_POST = 200       # the site's cap (PLAYS_LIMITS.plays in src/plex-plays.js)
QUIET_EVERY_S = 3600   # the "0 new" line, at most hourly
UA = 'lease-home-collector/plays-1'


def site_base(cfg):
    s = str(cfg.get('push_url') or '').rstrip('/')
    if s.endswith('/push'):
        s = s[:-5]
    return s if re.match(r'^https://theleases\.org/collector/[A-Za-z0-9_-]{20,64}$', s) else ''


def _text(v, n):
    return ' '.join(str(v or '').split())[:n]


def _iso(at):
    return dt.datetime.fromtimestamp(at, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if at else ''


def _rows(container):
    mc = (container or {}).get('MediaContainer') if isinstance(container, dict) else None
    rows = mc.get('Metadata') if isinstance(mc, dict) else None
    return rows if isinstance(rows, list) else []


def _at(m):
    v = m.get('viewedAt') if isinstance(m, dict) else None
    if isinstance(v, bool):
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def play_key(p):
    """A short hash of one play (track, artist, time): what state['at_cursor'] keeps. Pure."""
    return hashlib.sha1(('%s|%s|%s' % (p['track'], p['artist'], p['played_at'])).encode('utf-8')).hexdigest()[:12]


def plex_plays(container, account_id, after=0):
    """Plex play history (JSON) to Listening log plays, and the newest viewedAt among them. The poster collector's reading: a
    track row of that accountID at or past `after` (inclusive since 1.0.13; run() drops the ones already sent at that second),
    with a title and an artist (originalTitle, else grandparentTitle). Movie, episode and other rows, and other accounts'
    tracks, are skipped. Pure."""
    out, newest = [], after
    want = str(account_id or '').strip()
    if not want:
        return out, newest
    for m in _rows(container):
        if not isinstance(m, dict) or m.get('type') != 'track' or str(m.get('accountID', '')).strip() != want:
            continue
        at = _at(m)
        if at is None or at < after:
            continue
        track = _text(m.get('title'), 200)
        artist = _text(m.get('originalTitle') or m.get('grandparentTitle'), 200)
        if not track or not artist:
            continue
        play = {'track': track, 'artist': artist, 'played_at': _iso(at)}
        album = _text(m.get('parentTitle'), 200)
        if album:
            play['album'] = album
        dur = m.get('duration')
        if isinstance(dur, int) and not isinstance(dur, bool) and 0 < dur <= 6 * 3600 * 1000:
            play['duration_ms'] = dur
        out.append(play)
        newest = max(newest, at)
    return out, newest


def history_url(purl, account_id, after, start):
    """Every type (no type=10 since 1.0.13), that account, newest first, at or past the cursor, one page. Pure."""
    q = 'sort=viewedAt:desc&accountID=%s&viewedAt%%3E=%d&X-Plex-Container-Start=%d&X-Plex-Container-Size=%d' % (
        urllib.parse.quote(str(account_id)), after, start, HIST_PAGE)
    return purl + '/status/sessions/history/all?' + q


def read_history(purl, ptok, acct, after, http, note):
    """(the account's plays at or past `after`, oldest first and once each; the newest viewedAt of ANY row read, else `after`;
    how many rows were read), or None when Plex did not answer. Newest first, back until a page reaches past the cursor; a page
    that comes back oldest first means Plex ignored the sort, and then every page is read. note, a list, gets a line for an
    ignored sort or a read cut at the page cap."""
    plays, top, n, sorted_ok = {}, after, 0, True
    for page in range(HIST_PAGES + 1):
        if page == HIST_PAGES:
            note.append('plays: the history read stopped at %d rows; older plays were not read' % n)
            break
        status, data = http('GET', history_url(purl, acct, after, page * HIST_PAGE), {'X-Plex-Token': ptok, 'Accept': 'application/json', 'User-Agent': UA}, None, 30)
        if status != 200:
            print('[home-collector] plays: Plex answered the play history with HTTP %d' % status, flush=True)
            return None
        try:
            box = json.loads((data or b'null').decode('utf-8'))
        except ValueError:
            print('[home-collector] plays: Plex\'s play history was not JSON', flush=True)
            return None
        rows = _rows(box)
        n += len(rows)
        t = [x for x in (_at(m) for m in rows) if x is not None]
        if sorted_ok and len(t) > 1 and t[0] < t[-1]:
            sorted_ok = False
            note.append('plays: Plex ignored the newest-first sort; every page read')
        for p in plex_plays(box, acct, after)[0]:
            plays[play_key(p)] = p   # a play seen twice (new rows shift the pages while they are read) counts once
        top = max([top] + t)
        if len(rows) < HIST_PAGE:
            break
        if sorted_ok and t and min(t) < after:   # newest first and this page reached past the cursor: the rest is older still
            break
    return sorted(plays.values(), key=lambda p: p['played_at']), top, n


def run(cfg, now, http):
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    purl, ptok = str(cfg.get('plex_url') or '').rstrip('/'), str(cfg.get('plex_token') or '')
    acct, site = str(cfg.get('plex_account_id') or '').strip(), site_base(cfg)
    if not (purl and ptok and acct and site):
        cfg['outcome'] = 'nothing sent: push_url is not the site\'s collector link'
        return state
    try:
        after = int(state.get('cursor') or 0)
    except (TypeError, ValueError):
        after = 0
    note = []
    got = read_history(purl, ptok, acct, after, http, note)
    if got is None:
        cfg['failed'], cfg['outcome'] = True, 'Plex did not answer the play history; trying again in 5 minutes'
        return state
    for line in note:
        print('[home-collector] ' + line, flush=True)
    found, top, rows = got
    sent_here = set(state.get('at_cursor') or []) if after else set()
    plays = [p for p in found if not (p['played_at'] == _iso(after) and play_key(p) in sent_here)]
    added = dup = dropped = 0
    for i in range(0, len(plays), PLAYS_POST):
        body = json.dumps({'v': 1, 'plays': plays[i:i + PLAYS_POST]}, separators=(',', ':')).encode('utf-8')
        status, data = http('POST', site + '/plays', {'Content-Type': 'application/json', 'User-Agent': UA}, body, 30)
        try:
            ans = json.loads((data or b'{}').decode('utf-8'))
        except ValueError:
            ans = {}
        if not isinstance(ans, dict):
            ans = {}
        if status != 200:   # the cursor stays, so the same plays go again next run (the site keeps each once)
            print('[home-collector] plays: the site refused the plays (HTTP %d): %s' % (status, str(ans.get('message', ''))[:200]), flush=True)
            cfg['failed'], cfg['outcome'] = True, 'the site did not take the plays (HTTP %d); they go again next run' % status
            return state
        added += int(ans.get('added') or 0)
        dup += int(ans.get('duplicate') or 0)
        dropped += int(ans.get('dropped') or 0)
    if top > after:   # the newest row of any type, so the movies and episodes just read are not read again
        state['cursor'] = top
        state['at_cursor'] = sorted({play_key(p) for p in found if p['played_at'] == _iso(top)})
    elif plays:
        state['at_cursor'] = sorted(sent_here | {play_key(p) for p in plays if p['played_at'] == _iso(after)})
    if plays:
        print('[home-collector] plays: %d sent, %d new, %d already there%s' % (len(plays), added, dup, (', %d dropped by the site' % dropped) if dropped else ''), flush=True)
    elif now - float(state.get('quiet_at') or 0) >= QUIET_EVERY_S:
        state['quiet_at'] = now
        print('[home-collector] plays: 0 new (checked %d history rows)' % rows, flush=True)
    cfg['outcome'] = '%d sent, %d new (checked %d history rows)' % (len(plays), added, rows)
    return state
