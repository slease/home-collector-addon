#!/usr/bin/env python3
"""The Home collector's Plex album covers job (add-on 1.0.18; roadmap R-0447 "Plex album covers on Listening, carried by the Home
collector add-on", approved 11/3/26). collector.py's JOBS entry 'covers' calls run() every 5 minutes while push_url,
plex_url, plex_token and plex_account_id are set (blank sends nothing).

Plex serves an album's cover only on the house network and only with the Plex token, so the Listening page cannot ask for it. This
job reads Plex's play history the way the plays job does (every type, that ONE account's track rows, newest first past its own
cursor; a first run reads the whole history, so every album ever played is found) and notes each album the first time it turns up:
its album key (the row's parentRatingKey, or since 1.0.20 the id in its parentThumb path, since Plex's history rows have no
parentRatingKey), title (parentTitle), the play artists seen on it (originalTitle, else grandparentTitle,
as the plays carry them) and its cover path (parentThumb). Then, at most PER_PASS albums a run (about 12 every 5 minutes, so 4,000
albums take a day or so and the site is never flooded), it asks Plex's photo transcoder for two JPEGs of that cover:
  master   at most 1500 px on the long side (width=height=1500, minSize=0 so it fits inside the box, upscale=0 so a smaller cover
           stays at Plex's own size)
  display  at most 300 px on the long side, for the page
with the token in the X-Plex-Token header only, never in an address that is logged or sent. They go to the site once per album:
  POST <site>/covers  { v: 1, key, album, artists, master: base64, display: base64 }   (src/listening-covers.js checkCover)
A play artist seen later on an album already sent goes as a names-only post (no images). What was sent is remembered (state['sent'],
the key and short hashes of its artists), so an album is never sent twice. Plex answering 404, a cover that is not a JPEG or is too
large, or the site refusing it (400 or 413) marks the album as having none for NONE_RETRY_S (30 days); any other failure waits an
hour, then doubles, up to a week (state['fail']). A site that does not know the route yet (404) or does not answer stops the run;
the albums wait for the next one. NEVER sent: the token, Plex's address, a player, a device, a user or the cover's path.

The Cover Art Archive fallback (R-0447 step 1.2, folded into 1.0.18 before it was installed): when Plex has no cover for an album
(no cover path, or the transcoder's answer is one of the 30-day cases above), the job asks Plex for that album's MusicBrainz id
(GET /library/metadata/<key>?includeGuids=1, the token in the header as always; a Guid like mbid://<uuid>, or the old MusicBrainz
agent's guid). With one, it asks the free Cover Art Archive (coverartarchive.org, no key; it redirects to archive.org) for the
front image, the release group first, then the release by the same id:
  master   https://coverartarchive.org/release-group/<mbid>/front-1200   (1200 px on the long side, or the original when smaller)
  display  https://coverartarchive.org/release-group/<mbid>/front-250
as they are (no resizing: the add-on is standard library only, and both sizes sit inside the site's caps; the same JPEG checks as
Plex's answer apply), and posts them with source: 'caa'. Those requests carry only a polite User-Agent naming this add-on and the
site (CAA_UA, no email) and never the token, Plex's address or anything of the house's. Paced: one Cover Art Archive request a
second at most (CAA_GAP_S) and at most CAA_PER_PASS albums a run; an album past that waits for the next run; a 429 or 5xx (or no
answer) stops the archive for the rest of the run and the album waits an hour, then longer. No MusicBrainz id, or the archive
answering 404 for every id, keeps the letter tile and waits 30 days like any album with no cover. The browser never asks it.
The whole library (1.0.27, R-0447 step 5.1, 11/9/26: "we are also missing a ton of album covers"): the play history finds
only albums someone has played, so Site Status read 158 covers for 299 albums on 11/9/26. Once a day (LIB_EVERY_S) the job also
walks the Plex music library itself: GET /library/sections (the music sections, type artist), then each one's albums
(GET /library/sections/<key>/all?type=9, LIB_PAGE a page, at most LIB_PAGES_PER_RUN pages a run, resuming where it stopped), and
queues every album not sent yet with its title, its album artist (parentTitle) and its cover path (thumb), exactly as a played one.
The same pacing, checks and fallback apply, so a library of 300 albums is covered in a few hours. Read only; nothing new is sent.
Python 3 standard library only. The transcoder's parameters are Plex's as commonly documented, not checked against the owner's server.
"""
import base64
import hashlib
import json
import re
import time
import urllib.parse

HIST_PAGE = 500          # history rows a page, of every type (as the plays job since 1.0.13)
HIST_PAGES = 100         # 50,000 rows at most a run
PER_PASS = 12            # albums sent at most each run (every 5 minutes)
PASS_BUDGET_S = 150      # and the run stops starting new albums after this long
MASTER_PX, DISPLAY_PX = 1500, 300
MASTER_MAX, DISPLAY_MAX = 1536 * 1024, 160 * 1024   # the site's caps (COVER_LIMITS in src/listening-covers.js)
NONE_RETRY_S = 30 * 86400
FAIL_FIRST_S, FAIL_MAX_S = 3600, 7 * 86400
ARTISTS_MAX = 50
OLD_SITE_EVERY_S = 3600  # "the site does not take covers yet", at most hourly
UA = 'lease-home-collector/covers-1'
CAA_BASE = 'https://coverartarchive.org'
CAA_UA = 'lease-home-collector/1.0.22 ( https://theleases.org/ )'   # MusicBrainz's asked-for form: app/version ( contact ), a URL, no email
CAA_GAP_S = 1.0          # one Cover Art Archive request a second at most
CAA_PER_PASS = 4         # albums looked up there at most each run
CAA_MASTER, CAA_DISPLAY = 1200, 250
MBID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
KEY_RE = re.compile(r'^[0-9]{1,12}$')
LIB_EVERY_S = 86400      # the whole music library is walked once a day (1.0.27, R-0447 5.1)
LIB_PAGE = 500
LIB_PAGES_PER_RUN = 4
ROWS_V = 2               # 1.0.20: the album id may come from parentThumb; state from an older version reads the history again
THUMB_RE = re.compile(r'^/library/metadata/([0-9]{1,12})/thumb(/[0-9]{1,12})?$')


def site_base(cfg):
    s = str(cfg.get('push_url') or '').rstrip('/')
    if s.endswith('/push'):
        s = s[:-5]
    return s if re.match(r'^https://theleases\.org/collector/[A-Za-z0-9_-]{20,64}$', s) else ''


def _text(v, n=200):
    return ' '.join(str(v or '').split())[:n]


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


def name_hash(name):
    """A short hash of a play artist, lower case: what state['sent'] keeps per album. Pure."""
    return hashlib.sha1(_text(name).lower().encode('utf-8')).hexdigest()[:10]


def history_url(purl, account_id, after, start):
    q = 'sort=viewedAt:desc&accountID=%s&viewedAt%%3E=%d&X-Plex-Container-Start=%d&X-Plex-Container-Size=%d' % (
        urllib.parse.quote(str(account_id)), after, start, HIST_PAGE)
    return purl + '/status/sessions/history/all?' + q


def album_rows(container, account_id):
    """That account's track rows to albums: [(key, album, artist, thumb)], thumb '' when Plex gave none or an odd path. Pure."""
    out, want = [], str(account_id or '').strip()
    for m in _rows(container):
        if not isinstance(m, dict) or m.get('type') != 'track' or str(m.get('accountID', '')).strip() != want:
            continue
        thumb = str(m.get('parentThumb') or '').strip()
        thumb = thumb if THUMB_RE.match(thumb) else ''
        key = album_key(m, thumb)
        album = _text(m.get('parentTitle'))
        artist = _text(m.get('originalTitle') or m.get('grandparentTitle'))
        if not key or not album or not artist:
            continue
        out.append((key, album, artist, thumb))
    return out


def album_key(m, thumb):
    """Plex's album id for a play row: parentRatingKey when the row has one, else the id inside its cover path
    (/library/metadata/<id>/thumb/...). Plex's play history rows carry no parentRatingKey on the owner's server, so 1.0.18 and
    1.0.19 skipped every row and sent no cover (R-0447 step 4.1, fixed in 1.0.20). '' when neither gives one. Pure."""
    key = str(m.get('parentRatingKey') or '').strip()
    if KEY_RE.match(key):
        return key
    t = THUMB_RE.match(thumb or '')
    return t.group(1) if t else ''


def read_albums(purl, ptok, acct, after, http):
    """(albums seen at or past the cursor as {key: [album, [artists], thumb]}, the newest viewedAt of any row), or None when Plex
    did not answer. Newest first, back until a page reaches past the cursor (every page when Plex ignores the sort)."""
    found, top, sorted_ok = {}, after, True
    for page in range(HIST_PAGES):
        try:
            status, data = http('GET', history_url(purl, acct, after, page * HIST_PAGE), {'X-Plex-Token': ptok, 'Accept': 'application/json', 'User-Agent': UA}, None, 30)
        except (OSError, ValueError):
            status, data = 0, b''
        if status != 200:
            print('[home-collector] covers: Plex answered the play history with HTTP %d' % status, flush=True)
            return None
        try:
            box = json.loads((data or b'null').decode('utf-8'))
        except ValueError:
            print('[home-collector] covers: Plex\'s play history was not JSON', flush=True)
            return None
        rows = _rows(box)
        t = [x for x in (_at(m) for m in rows) if x is not None]
        if sorted_ok and len(t) > 1 and t[0] < t[-1]:
            sorted_ok = False
        for key, album, artist, thumb in album_rows(box, acct):
            a = found.setdefault(key, [album, [], thumb])
            if artist not in a[1] and len(a[1]) < ARTISTS_MAX:
                a[1].append(artist)
            if thumb and not a[2]:
                a[2] = thumb
        top = max([top] + t)
        if len(rows) < HIST_PAGE:
            break
        if sorted_ok and t and min(t) < after:
            break
    return found, top


def music_sections(box):
    """The keys of the music sections (type artist) in a /library/sections answer, at most 10. Pure."""
    mc = box.get('MediaContainer') if isinstance(box, dict) else None
    dirs = mc.get('Directory') if isinstance(mc, dict) else None
    out = []
    for d in dirs if isinstance(dirs, list) else []:
        k = str(d.get('key') or '').strip() if isinstance(d, dict) and d.get('type') == 'artist' else ''
        if KEY_RE.match(k) and k not in out:
            out.append(k)
    return sorted(out)[:10]


def library_albums(box):
    """One page of /library/sections/<key>/all?type=9 to [(key, album, artist, thumb)], thumb '' when Plex gave none or an odd
    path. The artist is the album artist (parentTitle). Pure."""
    out = []
    for m in _rows(box):
        if not isinstance(m, dict) or m.get('type') not in ('album', None):
            continue
        key = str(m.get('ratingKey') or '').strip()
        thumb = str(m.get('thumb') or '').strip()
        thumb = thumb if THUMB_RE.match(thumb) else ''
        album, artist = _text(m.get('title')), _text(m.get('parentTitle'))
        if KEY_RE.match(key) and album and artist:
            out.append((key, album, artist, thumb))
    return out


def _get_json(http, purl, ptok, path):
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


def walk_library(state, purl, ptok, now, http):
    """(albums found this run as {key: [album, [artist], thumb]}, pages read). Once a day it starts a walk of every music
    section's albums, LIB_PAGES_PER_RUN pages a run, kept in state['lib'] = {at, secs, sec, start}; Plex not answering leaves the
    walk where it stopped for the next run. Never fails the job: the play history is still read as before."""
    lib = dict(state.get('lib') or {})
    found, pages = {}, 0
    if not lib.get('secs') and now - float(lib.get('at') or 0) < LIB_EVERY_S:
        return found, pages
    if not lib.get('secs'):
        st, box = _get_json(http, purl, ptok, '/library/sections')
        secs = music_sections(box) if st == 200 else []
        if not secs:
            if st == 200:
                lib['at'] = now   # no music section: ask again tomorrow
            state['lib'] = lib
            return found, pages
        lib.update({'secs': secs, 'sec': 0, 'start': 0})
    while pages < LIB_PAGES_PER_RUN and lib.get('secs'):
        sk = lib['secs'][int(lib.get('sec') or 0)]
        st, box = _get_json(http, purl, ptok, '/library/sections/%s/all?type=9&X-Plex-Container-Start=%d&X-Plex-Container-Size=%d' % (sk, int(lib.get('start') or 0), LIB_PAGE))
        if st != 200 or box is None:
            break
        pages += 1
        rows = library_albums(box)
        for key, album, artist, thumb in rows:
            found.setdefault(key, [album, [artist], thumb])
        if len(_rows(box)) < LIB_PAGE:
            lib['sec'], lib['start'] = int(lib.get('sec') or 0) + 1, 0
            if lib['sec'] >= len(lib['secs']):
                lib = {'at': now, 'albums': int(lib.get('albums') or 0) + len(rows)}
                break
        else:
            lib['start'] = int(lib.get('start') or 0) + LIB_PAGE
        lib['albums'] = int(lib.get('albums') or 0) + len(rows)
    state['lib'] = lib
    return found, pages


def transcode_url(purl, thumb, px):
    """Plex's photo transcoder for one cover, fitted inside px by px and never upscaled. No token in it. Pure."""
    q = urllib.parse.urlencode([('width', px), ('height', px), ('minSize', 0), ('upscale', 0), ('url', thumb)])
    return purl + '/photo/:/transcode?' + q


def jpeg_size(b):
    """(width, height) from a JPEG's first frame header, or None. Pure."""
    if not isinstance(b, (bytes, bytearray)) or len(b) < 4 or b[0] != 0xff or b[1] != 0xd8:
        return None
    i = 2
    while i + 4 <= len(b):
        if b[i] != 0xff:
            return None
        m = b[i + 1]
        if m == 0xff:
            i += 1
            continue
        if m in (0xd9, 0xda):
            return None
        if 0xd0 <= m <= 0xd7 or m == 0x01:
            i += 2
            continue
        ln = (b[i + 2] << 8) | b[i + 3]
        if ln < 2 or i + 2 + ln > len(b):
            return None
        if 0xc0 <= m <= 0xcf and m not in (0xc4, 0xc8, 0xcc):
            if ln < 7:
                return None
            h, w = (b[i + 5] << 8) | b[i + 6], (b[i + 7] << 8) | b[i + 8]
            return (w, h) if w and h else None
        i += 2 + ln
    return None


def fetch_cover(http, purl, ptok, thumb, px, cap):
    """('ok', bytes) for a JPEG at most cap bytes and px on the long side; ('none', why) when Plex has no usable cover; ('fail', why)
    when Plex did not answer (try again later)."""
    try:
        status, data = http('GET', transcode_url(purl, thumb, px), {'X-Plex-Token': ptok, 'Accept': 'image/jpeg', 'User-Agent': UA}, None, 30)
    except (OSError, ValueError) as e:
        return 'fail', type(e).__name__
    if status in (400, 404):
        return 'none', 'Plex has no cover (HTTP %d)' % status
    if status != 200:
        return 'fail', 'HTTP %d' % status
    data = bytes(data or b'')
    if data[:3] != b'\xff\xd8\xff':
        return 'none', 'not a JPEG'
    if len(data) > cap:
        return 'none', 'too large'
    size = jpeg_size(data)
    if not size:
        return 'none', 'unreadable JPEG'
    if max(size) > px:
        return 'none', 'larger than asked'
    return 'ok', data


def album_mbids(container):
    """The MusicBrainz ids Plex keeps for one album (/library/metadata/<key>?includeGuids=1): its Guid entries 'mbid://<uuid>' and
    the old MusicBrainz agent's guid 'com.plexapp.agents.musicbrainz://<uuid>[/...][?...]', lower case, each once, two at most. Pure."""
    out = []
    for m in _rows(container)[:1]:
        if not isinstance(m, dict):
            continue
        ids = [g.get('id') for g in m['Guid'] if isinstance(g, dict)] if isinstance(m.get('Guid'), list) else []
        ids.append(m.get('guid'))
        for g in ids:
            g = str(g or '').strip().lower()
            for pre in ('mbid://', 'com.plexapp.agents.musicbrainz://'):
                if g.startswith(pre):
                    u = re.split(r'[/?#]', g[len(pre):], 1)[0]
                    if MBID_RE.match(u) and u not in out:
                        out.append(u)
    return out[:2]


def read_mbids(http, purl, ptok, key):
    """('ok', [mbids]) for an album (none when Plex does not know it), or ('fail', why) when Plex did not answer."""
    try:
        status, data = http('GET', purl + '/library/metadata/' + key + '?includeGuids=1', {'X-Plex-Token': ptok, 'Accept': 'application/json', 'User-Agent': UA}, None, 30)
    except (OSError, ValueError) as e:
        return 'fail', type(e).__name__
    if status in (400, 404):
        return 'ok', []
    if status != 200:
        return 'fail', 'HTTP %d' % status
    try:
        return 'ok', album_mbids(json.loads((data or b'null').decode('utf-8')))
    except ValueError:
        return 'ok', []


def caa_url(kind, mbid, px):
    """The Cover Art Archive's front image of a release group or release at one of its sizes. Pure."""
    return '%s/%s/%s/front-%d' % (CAA_BASE, kind, mbid, px)


class Pace:
    """At most one Cover Art Archive request every CAA_GAP_S seconds, and a stop for the rest of the run after a refusal."""
    def __init__(self, clock, sleep):
        self.clock, self.sleep, self.last, self.off, self.albums = clock, sleep, None, False, 0

    def wait(self):
        if self.last is not None:
            gap = CAA_GAP_S - (self.clock() - self.last)
            if gap > 0:
                self.sleep(gap)
        self.last = self.clock()


def caa_get(http, pace, url, px, cap):
    """One image from the archive, checked like Plex's: ('ok', bytes), ('none', why) or ('fail', why). Only the polite
    User-Agent goes with it: no token, nothing of the house's."""
    pace.wait()
    try:
        status, data = http('GET', url, {'User-Agent': CAA_UA, 'Accept': 'image/jpeg'}, None, 30)
    except (OSError, ValueError) as e:
        pace.off = True
        return 'fail', type(e).__name__
    if status in (400, 404):
        return 'none', 'the Cover Art Archive has no front image (HTTP %d)' % status
    if status != 200:
        if status == 429 or status >= 500:
            pace.off = True
        return 'fail', 'the Cover Art Archive answered HTTP %d' % status
    data = bytes(data or b'')
    if data[:3] != b'\xff\xd8\xff':
        return 'none', 'not a JPEG'
    if len(data) > cap:
        return 'none', 'too large'
    size = jpeg_size(data)
    if not size:
        return 'none', 'unreadable JPEG'
    if max(size) > px:
        return 'none', 'larger than the site takes'
    return 'ok', data


def caa_cover(http, pace, mbids):
    """('ok', {master, display}) from the Cover Art Archive, ('none', why) when it has no usable front image for any of the ids,
    or ('fail', why) when it did not answer (try again later)."""
    for mbid in mbids:
        for kind in ('release-group', 'release'):
            res, master = caa_get(http, pace, caa_url(kind, mbid, CAA_MASTER), MASTER_PX, MASTER_MAX)
            if res == 'fail':
                return res, master
            if res != 'ok':
                continue
            res, display = caa_get(http, pace, caa_url(kind, mbid, CAA_DISPLAY), DISPLAY_PX, DISPLAY_MAX)
            if res == 'fail':
                return res, display
            if res != 'ok':
                continue
            dm, dd = jpeg_size(master), jpeg_size(display)
            if dd[0] > dm[0] or dd[1] > dm[1]:
                continue
            return 'ok', {'master': master, 'display': display}
    return 'none', 'no front image in the Cover Art Archive'


def _post(http, site, body):
    try:
        status, data = http('POST', site + '/covers', {'Content-Type': 'application/json', 'User-Agent': UA}, json.dumps(body, separators=(',', ':')).encode('utf-8'), 60)
    except (OSError, ValueError):
        return 0, {}
    try:
        ans = json.loads((data or b'{}').decode('utf-8'))
    except ValueError:
        ans = {}
    return status, ans if isinstance(ans, dict) else {}


def run(cfg, now, http, clock=time.monotonic, sleep=time.sleep):
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    purl, ptok = str(cfg.get('plex_url') or '').rstrip('/'), str(cfg.get('plex_token') or '')
    acct, site = str(cfg.get('plex_account_id') or '').strip(), site_base(cfg)
    if not (purl and ptok and acct and site):
        cfg['outcome'] = 'nothing sent: push_url is not the site\'s collector link'
        return state
    sent = dict(state.get('sent') or {})        # key -> [artist hashes]
    pending = dict(state.get('pending') or {})  # key -> [album, [artists], thumb]
    names = dict(state.get('names') or {})      # key -> [album, [artists]] new artists for an album already sent
    none = dict(state.get('none') or {})        # key -> retry after (unix seconds)
    fail = dict(state.get('fail') or {})        # key -> [tries, retry after]
    if state.get('rows_v') != ROWS_V:
        # 1.0.18 and 1.0.19 moved the cursor past every play while taking none of them (no parentRatingKey), so the first 1.0.20 run
        # reads the whole history again; albums already sent stay sent.
        state['cursor'], state['rows_v'] = 0, ROWS_V
    try:
        after = int(state.get('cursor') or 0)
    except (TypeError, ValueError):
        after = 0
    got = read_albums(purl, ptok, acct, after, http)
    if got is not None:
        found, top = got
        for key, (album, artists, thumb) in found.items():
            if key in sent:
                new = [a for a in artists if name_hash(a) not in sent[key]]
                if new:
                    n = names.setdefault(key, [album, []])
                    n[1] = (n[1] + [a for a in new if a not in n[1]])[:ARTISTS_MAX]
                continue
            p = pending.setdefault(key, [album, [], thumb])
            p[1] = (p[1] + [a for a in artists if a not in p[1]])[:ARTISTS_MAX]
            if thumb and not p[2]:
                p[2] = thumb
        if top > after:
            state['cursor'] = top
    # the whole music library, once a day (1.0.27): albums nobody has played are queued like played ones
    lib_found, lib_pages = walk_library(state, purl, ptok, now, http)
    for key, (album, artists, thumb) in lib_found.items():
        if key in sent:
            continue
        p = pending.setdefault(key, [album, [], thumb])
        p[1] = (p[1] + [a for a in artists if a not in p[1]])[:ARTISTS_MAX]
        if thumb and not p[2]:
            p[2] = thumb
    t0, done, empty, stop, from_caa = clock(), 0, 0, None, 0
    pace = Pace(clock, sleep)
    for key in list(pending):
        if done >= PER_PASS or clock() - t0 > PASS_BUDGET_S:
            break
        if float(none.get(key) or 0) > now or (key in fail and float(fail[key][1]) > now):
            continue
        album, artists, thumb = pending[key]
        imgs = {} if thumb else ('none', 'no cover path')
        if thumb:
            for which, px, cap in (('master', MASTER_PX, MASTER_MAX), ('display', DISPLAY_PX, DISPLAY_MAX)):
                res, val = fetch_cover(http, purl, ptok, thumb, px, cap)
                if res != 'ok':
                    imgs = (res, val)
                    break
                imgs[which] = val
        source = 'plex'
        if isinstance(imgs, tuple) and imgs[0] == 'none':
            # Plex has no art for it: the Cover Art Archive by the album's MusicBrainz id, if Plex keeps one (R-0447 1.2)
            if pace.off or pace.albums >= CAA_PER_PASS:
                continue   # the archive's share of this run is used up: the album waits for the next run
            res, mb = read_mbids(http, purl, ptok, key)
            if res != 'ok':
                imgs = (res, mb)
            elif mb:
                pace.albums += 1
                res, val = caa_cover(http, pace, mb)
                imgs, source = (val, 'caa') if res == 'ok' else ((res, val), 'plex')
        if isinstance(imgs, tuple):
            if imgs[0] == 'none':
                none[key] = now + NONE_RETRY_S
                empty += 1
            else:
                tries = int(fail.get(key, [0, 0])[0]) + 1
                fail[key] = [tries, now + min(FAIL_FIRST_S * 2 ** (tries - 1), FAIL_MAX_S)]
            continue
        body = {'v': 1, 'key': key, 'album': album, 'artists': artists,
                'master': base64.b64encode(imgs['master']).decode('ascii'), 'display': base64.b64encode(imgs['display']).decode('ascii')}
        if source == 'caa':
            body['source'] = 'caa'
        status, ans = _post(http, site, body)
        if status == 200:
            from_caa += source == 'caa'
            sent[key] = sorted({name_hash(a) for a in artists})
            pending.pop(key, None)
            fail.pop(key, None)
            none.pop(key, None)
            done += 1
        elif status in (400, 413):
            print('[home-collector] covers: the site refused one album\'s cover (HTTP %d): %s' % (status, str(ans.get('message', ''))[:200]), flush=True)
            none[key] = now + NONE_RETRY_S
        else:
            stop = status
            break
    if stop is None:
        for key in list(names):
            if key not in sent:
                names.pop(key, None)
                continue
            album, artists = names[key]
            status, ans = _post(http, site, {'v': 1, 'key': key, 'album': album, 'artists': artists})
            if status == 200 or status in (400, 413):
                if status == 200:
                    sent[key] = sorted(set(sent[key]) | {name_hash(a) for a in artists})
                names.pop(key, None)
            else:
                stop = status
                break
    if stop == 404:
        if now - float(state.get('old_site_at') or 0) >= OLD_SITE_EVERY_S:
            state['old_site_at'] = now
            print('[home-collector] covers: the site does not take covers yet; trying again in 5 minutes', flush=True)
    elif stop is not None:
        print('[home-collector] covers: the site did not take the cover (HTTP %d); trying again in 5 minutes' % stop, flush=True)
    left = sum(1 for k in pending if not (float(none.get(k) or 0) > now))
    if done or empty:
        print('[home-collector] covers: %d sent, %d with no cover in Plex, %d waiting%s' % (
            done, empty, left, ', %d from the Cover Art Archive' % from_caa if from_caa else ''), flush=True)
    cfg['failed'] = got is None or (stop is not None and stop != 404)
    cfg['outcome'] = '%s%d sent, %d waiting, %d albums sent so far%s' % (
        'Plex did not answer the play history; ' if got is None else '', done, left, len(sent),
        '; the site did not take them (HTTP %d)' % stop if stop is not None else '')
    for k in [k for k, v in none.items() if float(v) <= now and k not in pending]:
        none.pop(k, None)
    state.update({'sent': sent, 'pending': pending, 'names': names, 'none': none, 'fail': fail})
    return state
