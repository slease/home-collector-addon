#!/usr/bin/env python3
"""The Home collector's movie job (roadmap R-0378, 10/25/26; the add-on itself is R-0003: collector.py's JOBS entry
'movies' calls run() from its loop every 5 minutes, and this module keeps the library to at most hourly itself).

The site decides; the house carries it out. This job, on the Home Assistant box, talks to Radarr and Plex on the house network
and to theleases.org only (src/movies.js):
  every 5 minutes  GET  <site>/movies/actions, do each one, POST <site>/movies/done with the results
  every hour       POST <site>/movies/library: the Radarr library rows, the TMDb ids Plex says were played and when
                   (seen: [tmdb_id, Unix seconds], one per movie per day, for the Movies seen section, R-0395)
Only two actions are ever taken, and anything else the site hands out is refused here too:
  add     {tmdb_id}             Radarr lookup by TMDb id, then add: monitored, Minimum availability `released`, search on add,
                                the quality profile and root folder from the add-on's options (blank: Radarr's first ones)
  delete  {radarr_id, tmdb_id}  only when Radarr's own record for that id has the same TMDb id; deleteFiles and an import exclusion
  tv-delete {sonarr_id, tvdb_id} (1.0.35, R-0533 step 1.3; the owner's yes 11/14/26: the add-on may delete a series and its files in
                                Sonarr) only when Sonarr's own record for that id has the same TVDB id; the whole series with its
                                files (deleteFiles) and an import list exclusion. The site hands one out only at the end of a show's
                                Leaving countdown (src/show-leaving.js), never as a direct delete. The Sonarr key and address are
                                the TV job's own options (sonarr_url, sonarr_key); unset, the removal answers not ok and the site
                                tries again a day later. This is the one Sonarr call that is not a GET.
The Radarr key, the Plex token and the server addresses live only in the add-on's options and are never sent to the site.
A library row carries exactly radarr_id, tmdb_id, title, year, added, size_bytes, has_file, imdb, tmdb_rating, rt, plus cert
(Radarr's US certification, such as PG-13), runtime (minutes) and poster (the TMDb poster path, such as /abc.jpg, from the
poster image's remoteUrl on image.tmdb.org; the Movie night picker, R-0016, 10/28/26), and from 1.0.5 release (the digital,
else physical, release day, for the Plex page's Coming soon, R-0412) and the file's quality name and a remux flag (R-0378 step
1.3), and from 1.0.6 file_added (movieFile.dateAdded: when the file arrived, so a movie wanted for months that downloaded
last night counts as new on the site; bug 10/30/26), and from 1.0.23 monitored (Radarr's monitored flag, so an unmonitored movie
that still has its file joins the Movies page's Free up space, R-0378 step 1.7) and cinema (Radarr's inCinemas day, for the
poster's In theaters now, R-0018 step 5.4): never a file path, a Plex user, a player or an address.
From 1.0.6 the library post also carries plex: the movies Plex's movie sections added in the last PLEX_RECENT_DAYS days
(tmdb_id from the Guid list, title, year, plex_added from addedAt), read from the same section listing the played check uses,
so a movie Radarr does not have still shows as new and an upgrade (Plex keeps the item's addedAt) never does.
From 1.0.5 it also sends, at most hourly, POST <site>/plex/activity (src/plex.js): Plex TV plays read from the server-wide
history (show title, season, episode, viewedAt and the Plex account id, past a cursor kept in state; R-0412, owner only on the
site). Streams now playing are streams_job.py's (R-0407).
From 1.0.12 (11/1/26): the TV plays are read NEWEST first back to the cursor (R-0412 step 3.4: Plex may ignore the viewedAt>=
filter, and the oldest-first read then reread the same oldest 2,000 plays every hour); the library post sends only the rows
whose hash changed since the last good post, marked partial: true, with a full post at least once a day (the site drops a
removed movie only from a full one); and it carries upgrades: each movie whose file Radarr replaced in the last 30 days, from
GET /api/v3/history/since (movieFileDeleted with data.reason Upgrade paired with the nearest downloadFolderImported), as
tmdb_id, upgraded_at, quality_before and quality_after (Radarr quality names; R-0406 step 2.10). That history shape is from
memory, NOT checked against a live Radarr; every field is read as optional, and Radarr not answering leaves upgrades out.
From 1.0.13 (11/1/26): no history read asks Plex for a type (type=10 answered nothing on the owner's server, so type=1 and type=4
are not trusted either); rows are kept to movie or episode in code. The TV plays have their own 5-minute timer (tv_plays_pass,
TV_PLAYS_EVERY_S) and log only when plays were sent, plus an hourly "0 new" line. The played-movies read stays hourly with the
library post: the site judges from the whole played list (every movie anyone ever played, plus viewCount and lastViewedAt from
the library sections), not from new plays past a cursor, so it cannot be cut down to a cheap 5-minute read without a site change.
From 1.0.17 (R-0407 step 0.2): that same hourly history read also gives the movie plays, one row each (tmdb_id by ratingKey,
title, viewedAt, accountID; movie_play_rows), posted past their own cursor to POST <site>/plex/activity { v: 1, movies }
(movie_plays_pass), oldest first, 2,000 a post, so the first run sends the whole history. Never a player, address or token.

Python 3 standard library only. The Radarr (/api/v3/movie, /qualityprofile, /rootfolder, /movie/lookup/tmdb) and Plex
(/library/sections, /library/sections/<key>/all?includeGuids=1 with Guid[].id "tmdb://N", viewCount, lastViewedAt) shapes are from
memory of their APIs, NOT checked against a live server; every field is read as optional. Played means anyone on the
server: the server-wide play history (/status/sessions/history/all, every account, asked for no type since 1.0.13 and kept to movie rows, matched by ratingKey)
plus viewCount and lastViewedAt. If any part of that read fails, or the history runs past HIST_PAGES, no played list is sent and
the site judges nothing that run (fail safe: never prune on a missing played list).

Entry point: run(cfg, now, http=None) -> the new state dict (collector.py saves it to /data/jobs/movies.json). cfg is the add-on's
options (push_url, radarr_url, radarr_key, radarr_profile, radarr_root, plex_url, plex_token) plus cfg['state'], the state it
returned last time (the two stamps and the last log lines live in it). http(method, url, headers=None, body=None, timeout=20)
-> (status, bytes), collector.py's own; injectable for tests. Log lines are printed and kept in state['log'].
"""
import calendar
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

ACTIONS_EVERY_S = 5 * 60
LIBRARY_EVERY_S = 60 * 60
TV_PLAYS_EVERY_S = 5 * 60 - 10   # 1.0.13: TV plays on their own 5-minute timer (10 s short, so a run every 300 s never skips one)
QUIET_EVERY_S = 60 * 60          # the "0 new TV plays" and "not read" lines, at most hourly
TIMEOUT = 20
MAX_MOVIES = 4000
UA = 'lease-home-collector/movies-1'


def default_http(method, url, headers=None, body=None, timeout=TIMEOUT):
    """The real network when collector.py passes none: urllib; an HTTP error is a status, not an exception."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        return e.code, e.read(4096)


class _Net:
    """Wraps collector.py's http function so every call carries the JSON headers."""
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


def why_not(status, data):
    """A failed Plex answer's short reason (no address, no token): 'HTTP 401', or not JSON."""
    if status < 200 or status >= 300:
        return 'HTTP %d' % status
    return 'not JSON, looks like XML or HTML' if (data or b'').lstrip()[:1] == b'<' else 'not JSON'


def _plex_get(http, base, path, hdr, why):
    """GET a Plex path as JSON, or None; a failure's reason goes on why as '<path without its query> answered <reason>'."""
    status, data = http.request('GET', base + path, hdr, None)
    got = _json(status, data)
    if got is None and why is not None:
        why.append('%s answered %s' % (path.split('?', 1)[0], why_not(status, data)))
    return got


def site_base(cfg):
    s = str(cfg.get('push_url') or '').rstrip('/')
    if s.endswith('/push'):
        s = s[:-5]
    return s if s.startswith('https://') and '/collector/' in s else ''


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _rating(r, key):
    v = ((r or {}).get(key) or {}).get('value') if isinstance((r or {}).get(key), dict) else None
    return round(float(v), 1) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 10 and v > 0 else None


CERT_OK = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-+ ')
POSTER_OK = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-')


def _cert(v):
    """Radarr's certification as short upper-case text (PG-13, R, TV-14), else None."""
    t = ' '.join(str(v).split()).upper()[:12] if isinstance(v, str) else ''
    return t if t and all(c in CERT_OK for c in t) else None


def _poster(images):
    """The TMDb path of the poster (/abc123.jpg) from Radarr's images, only from an https image.tmdb.org remoteUrl, else None."""
    for im in images if isinstance(images, list) else []:
        if not isinstance(im, dict) or im.get('coverType') != 'poster':
            continue
        u = str(im.get('remoteUrl') or '')
        if not u.startswith('https://image.tmdb.org/t/p/'):
            continue
        name = u.rsplit('/', 1)[-1]
        stem, _, ext = name.rpartition('.')
        if ext in ('jpg', 'png') and 4 <= len(stem) <= 80 and all(c in POSTER_OK for c in stem):
            return '/' + name
    return None


def _day(v):
    """A Radarr date string to YYYY-MM-DD when it is a sane day, else None."""
    if isinstance(v, str) and len(v) >= 10 and v[:4].isdigit() and v[4] == '-' and v[7] == '-' and v[5:7].isdigit() and v[8:10].isdigit() and '1900' < v[:4] < '2101':
        return v[:10]
    return None


def _release(m):
    """The day a movie reaches home (Radarr's digitalRelease, else physicalRelease) as YYYY-MM-DD, else None. The Plex page's
    Coming soon (R-0412, add-on 1.0.5) reads it for movies Radarr tracks without a file."""
    for k in ('digitalRelease', 'physicalRelease'):
        v = m.get(k)
        if isinstance(v, str) and len(v) >= 10 and v[:4].isdigit() and v[4] == '-' and v[7] == '-' and v[5:7].isdigit() and v[8:10].isdigit() and '1900' < v[:4] < '2101':
            return v[:10]
    return None


QUALITY_OK = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .+-')


def _quality(m):
    """The file's quality name in Radarr (movieFile.quality.quality.name, such as Remux-2160p or Bluray-1080p), else None."""
    f = m.get('movieFile') if isinstance(m.get('movieFile'), dict) else {}
    q = f.get('quality') if isinstance(f.get('quality'), dict) else {}
    q = q.get('quality') if isinstance(q.get('quality'), dict) else {}
    t = ' '.join(str(q.get('name') or '').split())[:40] if isinstance(q.get('name'), str) else ''
    return t if t and all(c in QUALITY_OK for c in t) else None


def _remux(m):
    """True when the file's quality name says Remux (R-0378 step 1.3: remux candidates measured, nothing changed), else False."""
    t = _quality(m)
    return None if t is None else 'remux' in t.lower()


def _iso(v):
    """An ISO time from Radarr (2026-10-30T06:12:44Z) as given when it looks sane (1990 to 2100), else None."""
    if not isinstance(v, str) or len(v) < 19 or len(v) > 40 or not ('1990' <= v[:4] < '2101') or v[4] != '-' or v[10] != 'T':
        return None
    return v if all(c in '0123456789-:.TZ+' for c in v) else None


def _file_added(m):
    """When the movie's file arrived (movieFile.dateAdded), only with a file, else None (add-on 1.0.6)."""
    f = m.get('movieFile') if isinstance(m.get('movieFile'), dict) else {}
    return _iso(f.get('dateAdded')) if m.get('hasFile') is True else None


def library_rows(movies):
    """Radarr /api/v3/movie to the site's rows: the nineteen fields only (monitored and cinema from 1.0.23). A row with no Radarr id, TMDb id or title is left out."""
    out = []
    for m in movies if isinstance(movies, list) else []:
        if not isinstance(m, dict):
            continue
        rid, tid, title = _int(m.get('id')), _int(m.get('tmdbId')), ' '.join(str(m.get('title') or '').split())[:200]
        if not (rid and tid and title):
            continue
        r = m.get('ratings') if isinstance(m.get('ratings'), dict) else {}
        rt = (r.get('rottenTomatoes') or {}).get('value') if isinstance(r.get('rottenTomatoes'), dict) else None
        added = m.get('added') if isinstance(m.get('added'), str) and m.get('added', '')[:4] > '1900' else None
        year = m.get('year') if isinstance(m.get('year'), int) and 1880 <= m.get('year') <= 2100 else None
        size = m.get('sizeOnDisk') if isinstance(m.get('sizeOnDisk'), int) and m.get('sizeOnDisk') >= 0 else None
        out.append({'radarr_id': rid, 'tmdb_id': tid, 'title': title, 'year': year, 'added': added, 'size_bytes': size,
                    'has_file': m.get('hasFile') is True, 'imdb': _rating(r, 'imdb'), 'tmdb_rating': _rating(r, 'tmdb'),
                    'rt': int(rt) if isinstance(rt, (int, float)) and not isinstance(rt, bool) and 0 <= rt <= 100 else None,
                    'cert': _cert(m.get('certification')), 'poster': _poster(m.get('images')), 'release': _release(m),
                    'quality': _quality(m) if m.get('hasFile') is True else None, 'remux': _remux(m) if m.get('hasFile') is True else None,
                    'file_added': _file_added(m), 'monitored': m.get('monitored') if isinstance(m.get('monitored'), bool) else None,
                    'cinema': _day(m.get('inCinemas')),
                    'runtime': m.get('runtime') if isinstance(m.get('runtime'), int) and not isinstance(m.get('runtime'), bool) and 0 < m.get('runtime') <= 600 else None})
    return out[:MAX_MOVIES]


def played_ids(items):
    """Plex movie items to the TMDb ids someone has played (viewCount above 0 or a lastViewedAt), from Guid tmdb://N."""
    out = set()
    for m in items if isinstance(items, list) else []:
        if not isinstance(m, dict):
            continue
        vc, lv = m.get('viewCount'), m.get('lastViewedAt')
        if not ((isinstance(vc, int) and vc > 0) or (isinstance(lv, int) and lv > 0)):
            continue
        for g in m.get('Guid') or []:
            gid = str((g or {}).get('id') or '') if isinstance(g, dict) else ''
            if gid.startswith('tmdb://') and gid[7:].isdigit():
                out.add(int(gid[7:]))
    return sorted(out)


def check_action(a):
    """The action rule, here as on the site: an add with a TMDb id, a delete with a Radarr id and a TMDb id, or (1.0.35) a
    tv-delete with a Sonarr id and a TVDB id. Else None."""
    if not isinstance(a, dict) or not isinstance(a.get('id'), str):
        return None
    if a.get('kind') == 'add' and _int(a.get('tmdb_id')) and a['id'] == 'a-%d' % a['tmdb_id']:
        return {'id': a['id'], 'kind': 'add', 'tmdb_id': a['tmdb_id']}
    if a.get('kind') == 'delete' and _int(a.get('tmdb_id')) and _int(a.get('radarr_id')) and a['id'] == 'd-%d' % a['tmdb_id']:
        return {'id': a['id'], 'kind': 'delete', 'tmdb_id': a['tmdb_id'], 'radarr_id': a['radarr_id']}
    if a.get('kind') == 'tv-delete' and _int(a.get('sonarr_id')) and _int(a.get('tvdb_id')) and a['id'] == 'ds-%d' % a['sonarr_id']:
        return {'id': a['id'], 'kind': 'tv-delete', 'sonarr_id': a['sonarr_id'], 'tvdb_id': a['tvdb_id']}
    return None


class Sonarr:
    """The one Sonarr write (1.0.35): a whole series and its files, after its Leaving countdown on the site."""
    def __init__(self, cfg, http):
        self.base, self.key, self.http = str(cfg.get('sonarr_url') or '').rstrip('/'), str(cfg.get('sonarr_key') or ''), http

    def ok(self):
        return bool(self.base and self.key)

    def delete(self, sonarr_id, tvdb_id):
        if not self.ok():
            return {'ok': False, 'message': 'Sonarr not set in the options'}
        mine = _json(*self.http.request('GET', self.base + '/api/v3/series/%d' % sonarr_id, {'X-Api-Key': self.key}, None))
        if not isinstance(mine, dict):
            return {'ok': False, 'message': 'Sonarr has no show with that id'}
        if _int(mine.get('tvdbId')) != tvdb_id:
            return {'ok': False, 'message': 'not the same show: nothing removed'}
        status, _ = self.http.request('DELETE', self.base + '/api/v3/series/%d?deleteFiles=true&addImportListExclusion=true' % sonarr_id, {'X-Api-Key': self.key}, None)
        return {'ok': True} if 200 <= status < 300 else {'ok': False, 'message': 'Sonarr refused the removal: HTTP %d' % status}


class Radarr:
    def __init__(self, cfg, http):
        self.base, self.key, self.http = str(cfg.get('radarr_url') or '').rstrip('/'), str(cfg.get('radarr_key') or ''), http
        self.profile_name, self.root = str(cfg.get('radarr_profile') or '').strip(), str(cfg.get('radarr_root') or '').strip()
        self.used_profile = None

    def ok(self):
        return bool(self.base and self.key)

    def call(self, method, path, body=None):
        hdr = {'X-Api-Key': self.key}
        if body is not None:
            hdr['Content-Type'] = 'application/json'
        return self.http.request(method, self.base + path, hdr, None if body is None else json.dumps(body).encode('utf-8'))

    def get(self, path):
        return _json(*self.call('GET', path))

    def pick(self):
        """(profile id, root folder) from the options, else Radarr's first ones; the profile's name is reported to the site."""
        profs = self.get('/api/v3/qualityprofile') or []
        prof = None
        for p in profs if isinstance(profs, list) else []:
            if isinstance(p, dict) and _int(p.get('id')) and (not self.profile_name or str(p.get('name', '')).lower() == self.profile_name.lower()):
                prof = p
                break
        root = self.root
        if not root:
            rf = self.get('/api/v3/rootfolder') or []
            root = next((f.get('path') for f in rf if isinstance(f, dict) and isinstance(f.get('path'), str)), '') if isinstance(rf, list) else ''
        if prof:
            self.used_profile = ''.join(c for c in str(prof.get('name') or '') if c.isalnum() or c in ' ._+()-')[:60] or None
        return (prof['id'] if prof else None), root

    def add(self, tmdb_id):
        found = self.get('/api/v3/movie/lookup/tmdb?tmdbId=%d' % tmdb_id)
        if isinstance(found, list):
            found = found[0] if found else None
        if not isinstance(found, dict) or _int(found.get('tmdbId')) != tmdb_id:
            return {'ok': False, 'message': 'Radarr did not find that movie'}
        if _int(found.get('id')):
            return {'ok': True, 'message': 'already in Radarr', 'radarr_id': found['id']}
        prof, root = self.pick()
        if not prof or not root:
            return {'ok': False, 'message': 'no quality profile or root folder in Radarr'}
        body = dict(found, qualityProfileId=prof, rootFolderPath=root, monitored=True, minimumAvailability='released',
                    addOptions={'searchForMovie': True})
        status, data = self.call('POST', '/api/v3/movie', body)
        ans = _json(status, data)
        if isinstance(ans, dict) and _int(ans.get('id')):
            return {'ok': True, 'radarr_id': ans['id']}
        if status == 400 and b'already' in (data or b'').lower():
            return {'ok': True, 'message': 'already in Radarr'}
        return {'ok': False, 'message': 'Radarr refused the add: HTTP %d' % status}

    def delete(self, radarr_id, tmdb_id):
        mine = self.get('/api/v3/movie/%d' % radarr_id)
        if not isinstance(mine, dict):
            return {'ok': False, 'message': 'Radarr has no movie with that id'}
        if _int(mine.get('tmdbId')) != tmdb_id:
            return {'ok': False, 'message': 'not the same movie: nothing removed'}
        status, _ = self.call('DELETE', '/api/v3/movie/%d?deleteFiles=true&addImportExclusion=true' % radarr_id)
        return {'ok': 200 <= status < 300} if 200 <= status < 300 else {'ok': False, 'message': 'Radarr refused the removal: HTTP %d' % status}


HIST_PAGE = 500    # rows a page, of every type: from 1.0.13 no history read asks Plex for a type (type=10 answered nothing on the owner's server)
HIST_PAGES = 400   # 200,000 history rows of every type (music included); a longer history is not read in part (that could miss a play), it fails safe


def tmdb_of(m):
    for g in m.get('Guid') or [] if isinstance(m, dict) else []:
        gid = str((g or {}).get('id') or '') if isinstance(g, dict) else ''
        if gid.startswith('tmdb://') and gid[7:].isdigit():
            return int(gid[7:])
    return None


def history_played(rows, by_key):
    """History rows (any account) to TMDb ids, by the row's ratingKey in the movie sections. Other types (tracks, episodes:
    the read asks for no type since 1.0.13) are skipped."""
    out = set()
    for h in rows if isinstance(rows, list) else []:
        if not isinstance(h, dict) or h.get('type') not in (None, 'movie'):
            continue
        t = by_key.get(str(h.get('ratingKey') or ''))
        if t:
            out.add(t)
    return out


SEEN_MAX = 25000   # the site's cap (SEEN_MAX in src/movies-seen.js)


def seen_pairs(items, rows, by_key):
    """Watch times for the Movies seen section (R-0395): [tmdb_id, viewed_at] in whole Unix seconds, one per movie per UTC
    day, from the history rows (any account) and each movie's lastViewedAt. Only ids and times: never an account or a player."""
    got = {}
    def add(t, s):
        if t and isinstance(s, int) and not isinstance(s, bool) and s > 0:
            got.setdefault((t, s // 86400), s)
    for m in items if isinstance(items, list) else []:
        if isinstance(m, dict):
            add(tmdb_of(m), m.get('lastViewedAt'))
    for h in rows:
        if isinstance(h, dict) and h.get('type') in (None, 'movie'):
            add(by_key.get(str(h.get('ratingKey') or '')), h.get('viewedAt'))
    newest = sorted(([t, s] for (t, _), s in got.items()), key=lambda p: p[1])[-SEEN_MAX:]   # past the cap, the newest
    return sorted(newest)


PLEX_RECENT_DAYS = 14
PLEX_RECENT_MAX = 500   # the site's cap (LIMITS.plex in src/movies.js)


def plex_recent(items, now):
    """Plex movie items added in the last PLEX_RECENT_DAYS days: [{tmdb_id, title, year, plex_added}], newest first. An item
    with no TMDb Guid, title or addedAt is left out. Pure."""
    out, ids = [], set()
    for m in items if isinstance(items, list) else []:
        if not isinstance(m, dict):
            continue
        t, at = tmdb_of(m), m.get('addedAt')
        title = ' '.join(str(m.get('title') or '').split())[:200]
        if not (t and title and _int(at)) or t in ids or at > now + 86400 or now - at > PLEX_RECENT_DAYS * 86400:
            continue
        ids.add(t)
        year = m.get('year') if isinstance(m.get('year'), int) and not isinstance(m.get('year'), bool) and 1880 <= m.get('year') <= 2100 else None
        out.append({'tmdb_id': t, 'title': title, 'year': year, 'plex_added': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(at))})
    out.sort(key=lambda r: r['plex_added'], reverse=True)
    return out[:PLEX_RECENT_MAX]


def movie_play_rows(rows, by_key):
    """History rows to movie plays (1.0.17, R-0407 step 0.2): tmdb_id (by the row's ratingKey in the movie sections), title,
    viewed_at and account (accountID), once each. A row with no movie, time or account is skipped; never a player or address. Pure."""
    out, seen = [], set()
    for h in rows if isinstance(rows, list) else []:
        if not isinstance(h, dict) or h.get('type') not in (None, 'movie'):
            continue
        t, at, acc = by_key.get(str(h.get('ratingKey') or '')), h.get('viewedAt'), h.get('accountID')
        if not t or not _int(at):
            continue
        acc = acc if (_int(acc) or acc == 0) and not isinstance(acc, bool) else None
        k = (t, acc, at)
        if k in seen:
            continue
        seen.add(k)
        title = ' '.join(str(h.get('title') or '').split())[:200] or None
        out.append({'tmdb_id': t, 'title': title, 'viewed_at': at, 'account': acc})
    out.sort(key=lambda p: p['viewed_at'])
    return out


def plex_played(cfg, http, seen=None, recent=None, now=None, why=None, plays=None):
    """TMDb ids anyone on the server has played, or None when any part could not be read (the site then judges nothing).
    With a list as seen, it is filled with the watch times (seen_pairs) when the read was whole. With a list as recent, it is
    filled with plex_recent once every movie section has been read (even if the history read then fails). With a list as plays,
    it is filled with every movie play in the history (movie_play_rows, 1.0.17) when the read was whole."""
    base, tok = str(cfg.get('plex_url') or '').rstrip('/'), str(cfg.get('plex_token') or '')
    if not (base and tok):
        return None
    hdr = {'X-Plex-Token': tok}
    secs = _plex_get(http, base, '/library/sections', hdr, why)
    if not isinstance(secs, dict) or not isinstance((secs.get('MediaContainer') or {}).get('Directory'), list):
        return None
    items = []
    for d in secs['MediaContainer']['Directory']:
        if isinstance(d, dict) and d.get('type') == 'movie' and str(d.get('key') or '').isdigit():
            got = _plex_get(http, base, '/library/sections/%s/all?includeGuids=1' % d['key'], hdr, why)
            if not isinstance(got, dict):
                return None
            items += (got.get('MediaContainer') or {}).get('Metadata') or []
    if recent is not None:
        recent.extend(plex_recent(items, now if now is not None else time.time()))
        recent.append(None)   # marker: the sections were read whole (taken off by the caller)
    played = set(played_ids(items))
    by_key = {str(m.get('ratingKey')): tmdb_of(m) for m in items if isinstance(m, dict) and m.get('ratingKey') is not None and tmdb_of(m)}
    hist = []
    for page in range(HIST_PAGES + 1):
        if page == HIST_PAGES:
            if why is not None:
                why.append('/status/sessions/history/all ran past %d pages' % HIST_PAGES)
            return None   # more history than one run reads: fail safe rather than miss a play
        got = _plex_get(http, base, '/status/sessions/history/all?X-Plex-Container-Start=%d&X-Plex-Container-Size=%d' % (page * HIST_PAGE, HIST_PAGE), hdr, why)
        if not isinstance(got, dict) or not isinstance(got.get('MediaContainer'), dict):
            return None
        rows = got['MediaContainer'].get('Metadata') or []
        if not isinstance(rows, list):
            return None
        played |= history_played(rows, by_key)
        hist += [h for h in rows if isinstance(h, dict) and h.get('type') in (None, 'movie')]   # only movie rows are kept for seen_pairs
        if len(rows) < HIST_PAGE:
            break
    if seen is not None:
        seen.extend(seen_pairs(items, hist, by_key))
    if plays is not None:
        plays.extend(movie_play_rows(hist, by_key))
    return sorted(played)


PLAYS_PAGES = 100      # 50,000 history rows of every type a run, newest first; past that the oldest unread ones are skipped (logged)
PLAYS_FIRST_DAYS = 90  # a first run (no cursor) reads back this far
LATE_DAYS = 30         # 1.0.28: once an hour the TV plays are read again this far behind the cursor, for plays Plex dates late
LATE_EVERY_S = 3600
BACKFILL_V = 1         # 1.0.28: state['tv_backfill'] below this reads the whole TV history once (the first run read 90 days)
PAGE_PAUSE_S = 0.5     # 1.0.28: a short pause between history pages, so a long read does not press on Plex
_pause = time.sleep
PLAYS_POST = 2000      # the site takes at most 2,000 plays a post (PLAYS_MAX in src/plex.js)


def tv_play_rows(rows, cursor):
    """Plex history rows to TV plays: show (grandparentTitle), season, episode, viewed_at, account (accountID), at or past the
    cursor. Other types and rows with no show, numbers or time are skipped. Pure."""
    out = []
    for h in rows if isinstance(rows, list) else []:
        if not isinstance(h, dict) or h.get('type') != 'episode':
            continue
        show = ' '.join(str(h.get('grandparentTitle') or '').split())[:200]
        s, e, at, acc = h.get('parentIndex'), h.get('index'), h.get('viewedAt'), h.get('accountID')
        if not show or not _int(at) or at < cursor or not isinstance(s, int) or isinstance(s, bool) or not (0 <= s <= 500) or not _int(e) or e > 5000:
            continue
        out.append({'show': show, 'season': s, 'episode': e, 'viewed_at': at, 'account': acc if _int(acc) or acc == 0 else None})
    return out


def _times(rows):
    return [h.get('viewedAt') for h in rows if isinstance(h, dict) and _int(h.get('viewedAt'))]


def _newest(plays):
    """Once each (show, season, episode, account, time), newest first. Pure."""
    seen, out = set(), []
    for p in sorted(plays, key=lambda p: p['viewed_at'], reverse=True):
        k = (p['show'], p['season'], p['episode'], p['account'], p['viewed_at'])
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def read_tv_plays(cfg, http, cursor, now=None, note=None, info=None, floor=None, start=0):
    """TV plays at or past the floor (the cursor, else PLAYS_FIRST_DAYS back on a first run), newest first, or None when Plex
    could not be read. Add-on 1.0.12 (R-0412 step 3.4): read NEWEST first (sort=viewedAt:desc) and page back until a page holds
    nothing at or past the floor. Plex may ignore the viewedAt>= filter, and the old oldest-first read then reread the same
    oldest 2,000 plays every hour and never reached a recent one. The filter is still asked (it saves pages when Plex honors
    it). When a page comes back oldest first (Plex ignored the sort too) the stop rule cannot be trusted, so every page up to
    PLAYS_PAGES is read. note, a list, gets a short line for an ignored sort or a read cut at the page cap.
    Add-on 1.0.13: no type=4 in the ask (type=10 answered nothing on the owner's server, so a type filter is not trusted); every
    type is read and tv_play_rows keeps the episodes. The stop rule reads every row's time, so a page of tracks or movies never
    hides an episode. info, a dict, gets 'top' (the newest viewedAt of ANY row read, so the cursor can move past music and movies
    too) and 'rows' (how many rows were read, for the quiet line). floor, when given (1.0.28), is the time to read back to
    instead: 0 for the whole history, or LATE_DAYS behind the cursor for plays Plex recorded with an older time.
    1.0.28: start is the page to begin at (a history read resumed); PAGE_PAUSE_S between pages; info also gets 'partial'
    (True when a page after the first failed or the PLAYS_PAGES cap was reached, so the history was not read to its end),
    'page' (the page to resume at), 'oldest' (the oldest viewedAt of any row read) and 'sorted' (Plex kept newest first)."""
    base, tok = str(cfg.get('plex_url') or '').rstrip('/'), str(cfg.get('plex_token') or '')
    if not (base and tok):
        return None
    now = time.time() if now is None else now
    if floor is None:
        floor = cursor if cursor > 0 else max(0, int(now - PLAYS_FIRST_DAYS * 86400))
    info = {} if info is None else info
    info['partial'], info['sorted'] = False, True
    out, sorted_ok = [], True
    for page in range(start, start + PLAYS_PAGES + 1):
        if page == start + PLAYS_PAGES:
            info['partial'], info['page'] = True, page
            if note is not None:
                note.append('TV plays read stopped at %d pages; older unread plays were skipped' % PLAYS_PAGES)
            break
        if page > start and PAGE_PAUSE_S:
            _pause(PAGE_PAUSE_S)
        q = 'sort=viewedAt:desc&viewedAt%%3E=%d&X-Plex-Container-Start=%d&X-Plex-Container-Size=%d' % (floor, page * HIST_PAGE, HIST_PAGE)
        try:
            got = _json(*http.request('GET', base + '/status/sessions/history/all?' + q, {'X-Plex-Token': tok}, None))
        except (OSError, ValueError):
            if page == start:
                raise
            got = None
        rows = got['MediaContainer'].get('Metadata') or [] if isinstance(got, dict) and isinstance(got.get('MediaContainer'), dict) else None
        if not isinstance(rows, list):
            if page == start:
                return None
            info['partial'], info['page'] = True, page   # a later page failed: what was read is kept, the rest is read again
            break
        t = _times(rows)
        if sorted_ok and len(t) > 1 and t[0] < t[-1]:
            sorted_ok = info['sorted'] = False
            if note is not None:
                note.append('Plex ignored the newest-first sort on TV plays; every page read')
        out += tv_play_rows(rows, floor)
        info['rows'] = info.get('rows', 0) + len(rows)
        if t:
            info['top'] = max([info.get('top', 0)] + t)
            info['oldest'] = min([info['oldest']] + t) if 'oldest' in info else min(t)
        if len(rows) < HIST_PAGE:
            break
        if sorted_ok and t and min(t) < floor:   # newest first and this page reached past the floor: the rest is older still
            break
    return _newest(out)


UPGRADE_DAYS = 30
UPGRADE_MAX = 500           # the site's cap (LIMITS.upgrades in src/plex-upgrades.js)
UPGRADE_PAIR_S = 15 * 60    # an import this close to an Upgrade deletion of the same movie is the copy that replaced it


def _qname(h):
    """A history record's quality name (quality.quality.name, such as CAM or Bluray-1080p), else None."""
    q = h.get('quality') if isinstance(h.get('quality'), dict) else {}
    q = q.get('quality') if isinstance(q.get('quality'), dict) else {}
    t = ' '.join(str(q.get('name') or '').split())[:40] if isinstance(q.get('name'), str) else ''
    return t if t and all(c in QUALITY_OK for c in t) else None


def _secs(iso):
    """Unix seconds of a UTC ISO time (to the second), else None."""
    try:
        return calendar.timegm(time.strptime(iso[:19], '%Y-%m-%dT%H:%M:%S'))
    except (TypeError, ValueError):
        return None


def upgrade_rows(history, rows, now):
    """Radarr history to the movies whose file was replaced by a better one in the last UPGRADE_DAYS days (add-on 1.0.12, R-0406
    step 2.10): [{tmdb_id, upgraded_at, quality_before, quality_after}], newest first. An upgrade is a movieFileDeleted whose
    data.reason is Upgrade (its quality is the one before); the downloadFolderImported of the same movie nearest it, within
    UPGRADE_PAIR_S, is the one after and its time is upgraded_at; with no such import the deletion's time is used and the after
    quality is the library row's current file quality. The TMDb id is the record's movie.tmdbId, else the library row with that
    Radarr id. Every field is read as optional: the Radarr v3 history shape is from memory, NOT checked against a live server.
    Only ids, times and quality names: never a source title, release group, indexer, path or client. Pure."""
    tmdb_by = {r['radarr_id']: r['tmdb_id'] for r in rows or []}
    qual_by = {r['radarr_id']: r.get('quality') for r in rows or []}
    recs = []
    for h in history if isinstance(history, list) else []:
        if not isinstance(h, dict):
            continue
        at, mid = _iso(h.get('date')), _int(h.get('movieId'))
        sec = _secs(at) if at else None
        if not (mid and sec is not None) or now - sec > UPGRADE_DAYS * 86400 or sec > now + 86400:
            continue
        mv = h.get('movie') if isinstance(h.get('movie'), dict) else {}
        data = h.get('data') if isinstance(h.get('data'), dict) else {}
        recs.append({'ev': str(h.get('eventType') or ''), 'mid': mid, 'sec': sec, 'q': _qname(h),
                     'tmdb': _int(mv.get('tmdbId')) or tmdb_by.get(mid), 'up': str(data.get('reason') or '').lower() == 'upgrade'})
    out, seen = [], set()
    for d in recs:
        if d['ev'] != 'movieFileDeleted' or not d['up'] or not d['tmdb']:
            continue
        imps = [i for i in recs if i['ev'] == 'downloadFolderImported' and i['mid'] == d['mid'] and abs(i['sec'] - d['sec']) <= UPGRADE_PAIR_S]
        imp = min(imps, key=lambda i: abs(i['sec'] - d['sec'])) if imps else None
        when = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(imp['sec'] if imp else d['sec']))
        if (d['tmdb'], when) in seen:
            continue
        seen.add((d['tmdb'], when))
        out.append({'tmdb_id': d['tmdb'], 'upgraded_at': when, 'quality_before': d['q'],
                    'quality_after': (imp['q'] if imp and imp['q'] else None) or (None if imp else qual_by.get(d['mid']))})
    out.sort(key=lambda r: r['upgraded_at'], reverse=True)
    return out[:UPGRADE_MAX]


def read_upgrades(radarr, rows, now):
    """GET /api/v3/history/since for the last UPGRADE_DAYS days (includeMovie), to upgrade_rows, or None when Radarr did not
    answer (the post then leaves upgrades out and the site keeps what it had)."""
    since = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - UPGRADE_DAYS * 86400))
    hist = radarr.get('/api/v3/history/since?' + urllib.parse.urlencode({'date': since, 'includeMovie': 'true'}))
    if isinstance(hist, dict) and isinstance(hist.get('records'), list):   # a paged answer, should Radarr send one
        hist = hist['records']
    return upgrade_rows(hist, rows, now) if isinstance(hist, list) else None


FULL_EVERY_S = 24 * 3600   # a full library post at least once a day, so the site can drop a removed row


def row_hash(r):
    return hashlib.sha1(json.dumps(r, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()[:16]


def changed(rows, hashes, key):
    """(the rows whose hash is not the one kept from the last good post, every row's new hash by its key). Pure."""
    now_h = {str(r[key]): row_hash(r) for r in rows}
    return [r for r in rows if (hashes or {}).get(str(r[key])) != now_h[str(r[key])]], now_h


def _due(state, key, every, now):
    if now - float(state.get(key) or 0) < every:
        return False
    state[key] = now   # stamped before the work, so a failing run also waits
    return True


def tv_key(p):
    """A short hash of one TV play, kept for the plays at the cursor's own second (state['tv_at_cursor']). Pure."""
    return hashlib.sha1(json.dumps([p['show'], p['season'], p['episode'], p['account'], p['viewed_at']]).encode('utf-8')).hexdigest()[:12]


def tv_plays_pass(cfg, http, state, now, post):
    """The TV plays, every 5 minutes (1.0.13; hourly with the library post before): read newest first back to the cursor,
    post what is new, move the cursor to the newest row of any type the read saw. The floor is inclusive, so the plays at the
    cursor's own second are kept as hashes and not sent again. Log lines: one when plays were sent, else at most hourly
    "0 new TV plays (checked N history rows)" or "Plex TV plays not read". Returns the lines.
    1.0.28 (bug b-adca2a2c, 11/11/26): the cursor is the newest row of ANY type, so a play Plex dates earlier than that (a
    download watched offline and synced later, or a watched mark set by hand) was never read. Once an hour the read goes back
    LATE_DAYS behind the cursor instead, and the plays sent in that span are kept as hashes (state['tv_recent']) so only the
    late ones are sent. Once per BACKFILL_V the read goes back to the start of Plex's history (a first run read 90 days only);
    state['tv_backfill'] is set only when the read reached the end of the history and every post of it was taken. A read that
    stopped part way (a page failed, or the page cap) keeps state['tv_backfill_at'] = {page, upto, keys} and the next run
    resumes there, sending only plays older than upto (plus new ones past the cursor), so nothing is sent twice."""
    cursor = int(state.get('tv_plays_cursor') or 0)
    note, info = [], {}
    backfill = cursor > 0 and int(state.get('tv_backfill') or 0) < BACKFILL_V
    late = cursor > 0 and not backfill and now - float(state.get('tv_late_at') or 0) >= LATE_EVERY_S
    floor = 0 if backfill else max(0, cursor - LATE_DAYS * 86400) if late else None
    # a history read that stopped part way (1.0.28): resume a page before where it stopped (new plays push rows down the
    # newest-first pages); every play newer than the oldest row it reached and no newer than the cursor was sent then
    resume = state.get('tv_backfill_at') if backfill and isinstance(state.get('tv_backfill_at'), dict) else None
    start = max(0, int(resume.get('page') or 0) - 1) if resume else 0
    try:
        plays = read_tv_plays(cfg, http, cursor, now, note, info, floor, start)
    except (OSError, ValueError):
        plays = None
    lines = ['movies: ' + n for n in note]
    quiet = now - float(state.get('tv_quiet_at') or 0) >= QUIET_EVERY_S
    if plays is None:
        if quiet:
            state['tv_quiet_at'] = now
            lines.append('movies: Plex TV plays not read')
        return lines
    done = set(state.get('tv_at_cursor') or []) if cursor else set()
    recent = dict(state.get('tv_recent') or {}) if isinstance(state.get('tv_recent'), dict) else {}
    plays = [p for p in plays if not (p['viewed_at'] == cursor and tv_key(p) in done) and tv_key(p) not in recent]
    if resume and _int(resume.get('upto')):
        upto, edge_keys = resume['upto'], set(resume.get('keys') or [])
        plays = [p for p in plays if p['viewed_at'] > cursor or p['viewed_at'] < upto or (p['viewed_at'] == upto and tv_key(p) not in edge_keys)]
    status = 200
    for i in range(0, len(plays), PLAYS_POST):   # newest first; one post per 2,000 (the site's cap); none when nothing is new
        status, _ = post('/plex/activity', {'v': 1, 'plays': plays[i:i + PLAYS_POST]})
        if status != 200:
            break
    if status == 200:   # the site keeps each play once, so a play sent again is harmless
        top = max([cursor, info.get('top', 0)] + [p['viewed_at'] for p in plays])
        if top > cursor:
            state['tv_at_cursor'] = sorted(tv_key(p) for p in plays if p['viewed_at'] == top)
        else:
            state['tv_at_cursor'] = sorted(done | {tv_key(p) for p in plays})
        state['tv_plays_cursor'] = top
        edge = top - LATE_DAYS * 86400
        recent.update({tv_key(p): p['viewed_at'] for p in plays})
        state['tv_recent'] = {k: v for k, v in recent.items() if isinstance(v, int) and v >= edge}
        if backfill and info.get('partial'):   # not to the end: the marker waits, and the next run picks up where this one stopped
            sent_before = resume if resume and _int(resume.get('upto')) else None
            upto = info.get('oldest') if info.get('sorted') and _int(info.get('oldest')) else None
            if upto is not None and sent_before and sent_before['upto'] < upto:
                upto = None   # the rows came back newer than last time (pages moved): keep the earlier mark
            mark = {'page': info.get('page', 0)}
            if upto is not None:
                mark['upto'] = upto
                mark['keys'] = sorted({tv_key(p) for p in plays if p['viewed_at'] == upto} | (set(sent_before.get('keys') or []) if sent_before and sent_before['upto'] == upto else set()))
            elif sent_before:
                mark['upto'], mark['keys'] = sent_before['upto'], sent_before.get('keys') or []
            state['tv_backfill_at'] = mark
            lines.append('movies: TV history read stopped at page %d, will retry next run' % (info.get('page', 0) + 1))
        else:
            if late or backfill:   # a read from the start covers the late window too
                state['tv_late_at'] = now
            if backfill:
                state['tv_backfill'] = BACKFILL_V
                state.pop('tv_backfill_at', None)
                lines.append('movies: TV history read from the start, %d plays sent' % len(plays))
    if plays:
        lines.append('movies: %d TV plays sent%s, site answered %d' % (len(plays), ' (late or earlier plays)' if late or backfill else '', status))
    elif quiet:
        state['tv_quiet_at'] = now
        lines.append('movies: 0 new TV plays (checked %d history rows)' % info.get('rows', 0))
    return lines


def movie_key(p):
    """A short hash of one movie play, kept for the plays at the cursor's own second (state['movie_at_cursor']). Pure."""
    return hashlib.sha1(json.dumps([p['tmdb_id'], p['account'], p['viewed_at']]).encode('utf-8')).hexdigest()[:12]


def movie_plays_pass(plays, state, post):
    """1.0.17 (R-0407 step 0.2): post the movie plays at or past the cursor to /plex/activity as { v: 1, movies }, 2,000 a post,
    oldest first, so a first run sends the whole history over a few posts; the cursor moves only when the site took them. An
    older site answers 400 with 'Unknown field "movies"' (it knows no movies field): the cursor stays and one line says so
    until the site is updated. Any other 400 (the site refused something in that batch) is logged with the site's own short
    message and the cursor moves past the batch, so one bad row never stops the feed. Returns the log lines."""
    cursor = int(state.get('movie_plays_cursor') or 0)
    done = set(state.get('movie_at_cursor') or []) if cursor else set()
    todo = [p for p in plays if p['viewed_at'] > cursor or (p['viewed_at'] == cursor and movie_key(p) not in done)]
    if not todo:
        return []
    status, sent, lines = 200, 0, []
    for i in range(0, len(todo), PLAYS_POST):
        part = todo[i:i + PLAYS_POST]
        status, body = post('/plex/activity', {'v': 1, 'movies': part})
        if status == 400:
            msg = site_message(body)
            if 'unknown field "movies"' in msg.lower():   # an older site: wait for it, cursor where it was
                if state.get('movie_plays_old_site') != 1:
                    state['movie_plays_old_site'] = 1
                    lines.append('movies: the site does not take movie plays yet (update the site first); %d waiting' % (len(todo) - i))
                return lines
            lines.append('movies: the site refused %d movie plays: %s' % (len(part), msg or 'no reason given'))   # skipped, the feed goes on
        elif status != 200:
            break
        else:
            sent += len(part)
        top = part[-1]['viewed_at']
        state['movie_at_cursor'] = sorted(movie_key(p) for p in todo[:i + PLAYS_POST] if p['viewed_at'] == top)
        state['movie_plays_cursor'] = top
    state.pop('movie_plays_old_site', None)
    return lines + ['movies: %d movie plays sent, site answered %d' % (sent, status)]


def site_message(body):
    """The site's short reason from a refusal ({ ok: false, message }), plain and cut to 120 characters, else ''. Pure."""
    try:
        m = json.loads((body or b'{}').decode('utf-8')).get('message')
    except (ValueError, AttributeError):
        return ''
    return ' '.join(str(m or '').split())[:120] if isinstance(m, str) else ''


def run(cfg, now, http=None):
    """One pass from the add-on's loop. Returns the new state; its 'log' holds this pass's lines (no secret in any of them)."""
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    state['log'] = _pass(cfg, now, _Net(http or default_http), state)
    for line in state['log']:
        print('[home-collector] ' + line, flush=True)
    return state


def _pass(cfg, now, http, state):
    site = site_base(cfg)
    radarr, sonarr = Radarr(cfg, http), Sonarr(cfg, http)
    lines = []
    if not site:
        return ['movies: no site link in the options']
    post = lambda path, body: http.request('POST', site + path, {'Content-Type': 'application/json'}, json.dumps(body).encode('utf-8'))
    if not radarr.ok():   # 1.0.28 (bug b-adca2a2c): the TV plays come from Plex alone, so they no longer stop with Radarr unset
        lines = ['movies: Radarr not set in the options']
        if _due(state, 'activity_at', TV_PLAYS_EVERY_S, now):
            lines += tv_plays_pass(cfg, http, state, now, post)
        return lines
    if _due(state, 'movies_actions_at', ACTIONS_EVERY_S, now):
        ans = _json(*http.request('GET', site + '/movies/actions', {}, None)) or {}
        results = []
        for raw in ans.get('actions') or [] if isinstance(ans, dict) else []:
            a = check_action(raw)
            if not a:
                lines.append('movies: refused an action that is not an add, a delete or a show removal')
                continue
            try:
                if a['kind'] == 'tv-delete':
                    r = sonarr.delete(a['sonarr_id'], a['tvdb_id'])
                else:
                    r = radarr.add(a['tmdb_id']) if a['kind'] == 'add' else radarr.delete(a['radarr_id'], a['tmdb_id'])
            except (OSError, ValueError) as e:
                r = {'ok': False, 'message': '%s not reached: %s' % ('Sonarr' if a['kind'] == 'tv-delete' else 'Radarr', type(e).__name__)}
            results.append(dict({'id': a['id']}, **{k: v for k, v in r.items() if k in ('ok', 'message', 'radarr_id')}))
        if results:
            body = {'v': 1, 'results': results}
            if radarr.used_profile:
                body['profile'] = radarr.used_profile
            status, _ = post('/movies/done', body)
            lines.append('movies: %d actions done, %d ok, site answered %d' % (len(results), sum(1 for r in results if r['ok']), status))
    if _due(state, 'movies_library_at', LIBRARY_EVERY_S, now):
        rows = library_rows(radarr.get('/api/v3/movie'))
        seen, recent, why, mplays = [], [], [], []
        try:
            played = plex_played(cfg, http, seen, recent, now, why, mplays)
        except (OSError, ValueError) as e:
            played = None
            why.append('read failed: %s' % type(e).__name__)
        reason = why[0] if why else None
        if reason and reason != state.get('plex_fail'):   # one line per distinct failure, not one an hour
            lines.append('movies: Plex %s' % reason)
        if reason:
            state['plex_fail'] = reason
        else:
            state.pop('plex_fail', None)
        body = {'v': 1, 'movies': rows}
        if played is not None:
            body['played'] = played
            body['seen'] = seen
        if recent and recent[-1] is None:   # the sections were read whole: Plex's own recently added (1.0.6)
            body['plex'] = recent[:-1]
        try:
            ups = read_upgrades(radarr, rows, now)   # 1.0.12, R-0406 step 2.10
        except (OSError, ValueError):
            ups = None
        if ups is not None:
            body['upgrades'] = ups
        # 1.0.12: only the rows that changed since the last good post, with a full post once a day (the site drops a removed
        # row only from a full one) and whenever no hashes are kept (a first run, or a post that failed)
        full = not state.get('movies_hashes') or now - float(state.get('movies_full_at') or 0) >= FULL_EVERY_S
        diff, hashes = changed(rows, state.get('movies_hashes'), 'radarr_id')
        if not full:
            body['movies'], body['partial'] = diff, True
        status, ans = post('/movies/library', body)
        if status == 409 and not full:   # the site has no library to merge into: send it whole at once
            body.pop('partial', None)
            body['movies'], full = rows, True
            status, ans = post('/movies/library', body)
        if status == 400 and ('partial' in body or 'upgrades' in body):   # a site before 1.0.12: the post as 1.0.11 sent it
            body = {k: v for k, v in body.items() if k not in ('partial', 'upgrades')}
            body['movies'], full = rows, True
            status, ans = post('/movies/library', body)
        if status == 200:
            state['movies_hashes'] = hashes
            if full:
                state['movies_full_at'] = now
        else:
            state.pop('movies_hashes', None)   # the next post is whole
        lines.append('movies: library sent, %s, %s%s, site answered %d' % (
                     ('%d movies' % len(rows)) if full else ('%d of %d movies changed' % (len(body['movies']), len(rows))),
                     ('%d played' % len(played)) if played is not None else 'no played list (Plex not read, nothing judged)',
                     (', %d upgrades' % len(ups)) if ups is not None and 'upgrades' in body else '', status))
        if played is not None:   # 1.0.17: the movie plays past their cursor, from the same history read (R-0407 step 0.2)
            lines += movie_plays_pass(mplays, state, post)
    if _due(state, 'activity_at', TV_PLAYS_EVERY_S, now):   # 1.0.13: every 5 minutes, no longer with the hourly library
        lines += tv_plays_pass(cfg, http, state, now, post)
    return lines
