#!/usr/bin/env python3
"""The Home collector's books job (1.0.41, roadmap R-0568 step 0.2, 11/17/26 "Entertainment a Books section"; the spec
from Home Assistant's own test of Bookshelf, 11/17/26). collector.py's JOBS entry 'books' calls run() every 5 minutes.

Bookshelf is a Readarr fork on the media miner, on the Readarr v1 API. This job, on the Home Assistant box, talks to Bookshelf on
the house network and to theleases.org only (src/bookshelf.js):
  every hour      POST <site>/books/report  { v: 1, books?, authors?, queue?, wanted?, calendar?, history? }
                  read only, from GET /api/v1/book, /author, /queue, /wanted/missing, /calendar and /history. A part Bookshelf
                  did not answer is left out (the site keeps what it had for it); a failed book read sends nothing at all.
  every 5 minutes GET  <site>/books/actions, do each add, POST <site>/books/done with the results
The only write is an add, and only for the owner (the site hands one out only for a book the owner's own search found):
  add {foreign_id, term}  Bookshelf's lookup finds the book again by its id (the same foreignBookId, or nothing is added), then
                          POST /api/v1/book, monitored, with the quality profile, metadata profile and root folder from the
                          options (bookshelf_profile, bookshelf_metadata_profile, bookshelf_root), then POST /api/v1/command
                          BookSearch for it. A book Bookshelf already has is answered "already in Bookshelf". Nothing ever deletes.
Search (lookup(), called by house_link.py when the site asks over the house link): GET /api/v1/book/lookup?term=... with a short
timeout (LOOKUP_TIMEOUT), at most LOOKUP_MAX books. A slow, failed or empty answer is not_answering, which the site words plainly.

What leaves this machine: names, titles, series, years, page counts, ratings, genres, https cover addresses, whether a file is
there and its size, the queue's percent done and status word, and the history's event kind and time. Never a file path, a
release name, a download client, an indexer name, the Bookshelf key or its address: none of those are read into a row.

Python 3 standard library only. The Readarr v1 shapes (book: id, title, foreignBookId, authorId, author.authorName, seriesTitle,
releaseDate, pageCount, ratings.value, genres, images[].coverType/remoteUrl, monitored, statistics.bookFileCount/sizeOnDisk,
added; author: id, authorName, sortName, foreignAuthorId, status, monitored, statistics.bookCount/bookFileCount; queue and history
as paged { records }; lookup terms "readarr:<foreignBookId>"; the add's author block with qualityProfileId, metadataProfileId,
rootFolderPath and addOptions; the BookSearch command) are from memory of Readarr's API, NOT checked against a live Bookshelf.
Every field is read as optional.

Entry point: run(cfg, now, http=None) -> the new state dict (collector.py saves it to /data/jobs/books.json).
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPORT_EVERY_S = 60 * 60
ACTIONS_EVERY_S = 5 * 60 - 10
TIMEOUT = 30
LOOKUP_TIMEOUT = 10      # seconds: Home Assistant's test of Bookshelf's search came back empty after 30 s (11/17/26)
LOOKUP_MAX = 20
MAX_BOOKS = 10000
MAX_AUTHORS = 4000
MAX_LIST = 300
CALENDAR_DAYS = 90
HISTORY_DAYS = 30
UA = 'lease-home-collector/books-1'
QUEUE_STATES = ('downloading', 'queued', 'paused', 'importing', 'completed', 'warning', 'failed', 'delay')
HISTORY_KINDS = {'grabbed': 'grabbed', 'bookfileimported': 'imported', 'downloadimported': 'imported', 'trackfileimported': 'imported',
                 'downloadfailed': 'failed', 'bookimportincomplete': 'failed', 'bookfiledeleted': 'deleted',
                 'bookfilerenamed': 'renamed', 'bookfileretagged': 'retagged', 'downloadignored': 'ignored'}
TEXT_MAX = 200
PRIVATE_TLDS = ('local', 'lan', 'home', 'internal', 'arpa')   # a name only the house network knows
TAG_RE = re.compile(r'<[^<>]{0,200}>')
FOREIGN_OK = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-')
GENRE_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 &'.,-")


def default_http(method, url, headers=None, body=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(32 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        return e.code, e.read(4096)


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


def _int(v, lo=1, hi=10 ** 12):
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


def _opt_int(v, default):
    try:
        n = int(str(v).strip()) if str(v or '').strip() else default
    except ValueError:
        return default
    return n if n > 0 else default


def _text(v, n=TEXT_MAX):
    """Plain text: control characters and markup out, one space between words, cut. Pure."""
    if not isinstance(v, str):
        return ''
    v = TAG_RE.sub(' ', v)
    t = ''.join(' ' if (ord(c) < 32 or 127 <= ord(c) < 160 or c in '<>\u2028\u2029' or 0x202a <= ord(c) <= 0x202e or 0x2066 <= ord(c) <= 0x2069) else c for c in v)
    return ' '.join(t.split())[:n]


def _foreign(v):
    s = str(v) if isinstance(v, (str, int)) and not isinstance(v, bool) else ''
    return s if 1 <= len(s) <= 64 and all(c in FOREIGN_OK for c in s) else None


def _iso(v):
    if not isinstance(v, str) or len(v) < 10:
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    if d.year < 1000 or d.year > 2199:
        return None
    return d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _day(v):
    t = _iso(v)
    return t[:10] if t else None


def _rating(r):
    v = r.get('value') if isinstance(r, dict) else None
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
        return None
    v = v / 2.0 if v > 5 else float(v)   # a 10-point rating halved
    return round(v, 2) if v <= 5 else None


def _https(u):
    """An https address on a public name, with no user, port or key-like query, else None (never Bookshelf's own /MediaCover copy)."""
    if not isinstance(u, str) or len(u) > 400 or not u.startswith('https://'):
        return None
    p = urllib.parse.urlsplit(u)
    h = (p.hostname or '').lower()
    if p.username or p.password or p.port or not h or '.' not in h or h.replace('.', '').isdigit():
        return None
    if h.rsplit('.', 1)[-1] in PRIVATE_TLDS or any(w in p.query.lower() for w in ('key', 'token', 'auth', 'sig')):
        return None
    return u


def _cover(b):
    for im in b.get('images') or [] if isinstance(b.get('images'), list) else []:
        if isinstance(im, dict) and im.get('coverType') in ('cover', 'poster'):
            u = _https(im.get('remoteUrl'))
            if u:
                return u
    return _https(b.get('remoteCover'))


def _genres(v):
    out = []
    for g in v if isinstance(v, list) else []:
        t = _text(g, 40)
        if t and all(c in GENRE_OK for c in t) and t not in out:
            out.append(t)
        if len(out) >= 12:
            break
    return out


def _author_of(b, names):
    a = b.get('author') if isinstance(b.get('author'), dict) else {}
    return _text(a.get('authorName')) or names.get(_int(b.get('authorId'))) or _text(b.get('authorTitle')) or ''


def book_rows(items, names):
    """Bookshelf /api/v1/book to the site's rows (the sixteen fields only). A book with no id or title is left out. Pure."""
    out, seen = [], set()
    for b in items if isinstance(items, list) else []:
        if not isinstance(b, dict):
            continue
        bid, title = _int(b.get('id')), _text(b.get('title'))
        if not bid or not title or bid in seen:
            continue
        seen.add(bid)
        st = b.get('statistics') if isinstance(b.get('statistics'), dict) else {}
        rel = _day(b.get('releaseDate'))
        r = {'book_id': bid, 'title': title}
        fid = _foreign(b.get('foreignBookId'))
        if fid:
            r['foreign_id'] = fid
        if _int(b.get('authorId')):
            r['author_id'] = b['authorId']
        a = _author_of(b, names)
        if a:
            r['author'] = a
        s = _text(b.get('seriesTitle'))
        if s:
            r['series'] = s
        if rel:
            r['release'] = rel
            r['year'] = int(rel[:4])
        if _int(b.get('pageCount'), 0, 99999) is not None:
            r['pages'] = b['pageCount']
        rt = _rating(b.get('ratings'))
        if rt is not None:
            r['rating'] = rt
        c = _cover(b)
        if c:
            r['cover'] = c
        g = _genres(b.get('genres'))
        if g:
            r['genres'] = g
        r['monitored'] = b.get('monitored') is True
        r['has_file'] = (_int(st.get('bookFileCount'), 0) or 0) > 0
        if _int(st.get('sizeOnDisk'), 0, 10 ** 13) is not None:
            r['size_bytes'] = st['sizeOnDisk']
        ad = _iso(b.get('added'))
        if ad:
            r['added'] = ad
        out.append(r)
        if len(out) >= MAX_BOOKS:
            break
    return out


def author_rows(items):
    """Bookshelf /api/v1/author to the site's rows. Pure."""
    out, seen = [], set()
    for a in items if isinstance(items, list) else []:
        if not isinstance(a, dict):
            continue
        aid, name = _int(a.get('id')), _text(a.get('authorName'))
        if not aid or not name or aid in seen:
            continue
        seen.add(aid)
        st = a.get('statistics') if isinstance(a.get('statistics'), dict) else {}
        r = {'author_id': aid, 'name': name, 'monitored': a.get('monitored') is True}
        fid = _foreign(a.get('foreignAuthorId'))
        if fid:
            r['foreign_id'] = fid
        sn = _text(a.get('sortName'))
        if sn:
            r['sort_name'] = sn
        if a.get('status') in ('continuing', 'ended'):
            r['status'] = a['status']
        for k, src in (('books', 'bookCount'), ('books_have', 'bookFileCount')):
            if _int(st.get(src), 0, 99999) is not None:
                r[k] = st[src]
        im = next((_https(i.get('remoteUrl')) for i in (a.get('images') or []) if isinstance(i, dict) and i.get('coverType') in ('poster', 'headshot') and _https(i.get('remoteUrl'))), None) if isinstance(a.get('images'), list) else None
        if im:
            r['image'] = im
        out.append(r)
        if len(out) >= MAX_AUTHORS:
            break
    return out


def _records(ans):
    if isinstance(ans, dict) and isinstance(ans.get('records'), list):
        return ans['records']
    return ans if isinstance(ans, list) else None


def _title_of(r, books):
    b = r.get('book') if isinstance(r.get('book'), dict) else {}
    bid = _int(r.get('bookId')) or _int(b.get('id'))
    have = books.get(bid) or {}
    return bid, have.get('title') or _text(b.get('title')), have.get('author') or _text(((b.get('author') or {}) if isinstance(b.get('author'), dict) else {}).get('authorName'))


def queue_rows(ans, books):
    """The download queue: the book (never the release's name), its status word and percent done, the expected finish. Pure."""
    recs = _records(ans)
    if recs is None:
        return None
    out = []
    for r in recs:
        if not isinstance(r, dict):
            continue
        bid, title, author = _title_of(r, books)
        if not title:
            continue
        row = {'title': title}
        if bid:
            row['book_id'] = bid
        if author:
            row['author'] = author
        st = str(r.get('status') or '').lower()
        tds = str(r.get('trackedDownloadState') or '').lower()
        if tds in ('importpending', 'importing'):
            st = 'importing'
        elif str(r.get('trackedDownloadStatus') or '').lower() == 'warning':
            st = 'warning'
        if st in QUEUE_STATES:
            row['state'] = st
        size, left = r.get('size'), r.get('sizeleft')
        if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0 and isinstance(left, (int, float)) and not isinstance(left, bool):
            row['percent'] = max(0, min(100, int(round(100 * (size - left) / size))))
        eta = _iso(r.get('estimatedCompletionTime'))
        if eta:
            row['day'] = eta
        out.append(row)
        if len(out) >= MAX_LIST:
            break
    return out


def book_list(ans, books):
    """wanted/missing or the calendar: the book, its author and release day. Pure."""
    recs = _records(ans)
    if recs is None:
        return None
    out = []
    for b in recs:
        if not isinstance(b, dict):
            continue
        bid = _int(b.get('id'))
        have = books.get(bid) or {}
        title = have.get('title') or _text(b.get('title'))
        if not title:
            continue
        row = {'title': title}
        if bid:
            row['book_id'] = bid
        a = have.get('author') or _author_of(b, {})
        if a:
            row['author'] = a
        d = _day(b.get('releaseDate'))
        if d:
            row['day'] = d
        out.append(row)
        if len(out) >= MAX_LIST:
            break
    return out


def history_rows(ans, books, since_iso):
    """History since since_iso: the book, the event's kind (a fixed word) and time; never the release, indexer or client. Pure."""
    recs = _records(ans)
    if recs is None:
        return None
    out = []
    for r in recs:
        if not isinstance(r, dict):
            continue
        at = _iso(r.get('date'))
        if not at or at < since_iso:
            continue
        bid, title, author = _title_of(r, books)
        if not title:
            continue
        row = {'title': title, 'day': at, 'state': HISTORY_KINDS.get(str(r.get('eventType') or '').lower(), 'other')}
        if bid:
            row['book_id'] = bid
        if author:
            row['author'] = author
        out.append(row)
        if len(out) >= MAX_LIST:
            break
    return out


class Bookshelf:
    def __init__(self, cfg, http):
        self.base, self.key, self.http = str(cfg.get('bookshelf_url') or '').strip().rstrip('/'), str(cfg.get('bookshelf_key') or '').strip(), http
        self.profile = _opt_int(cfg.get('bookshelf_profile'), 1)
        self.meta = _opt_int(cfg.get('bookshelf_metadata_profile'), 1)
        self.root = str(cfg.get('bookshelf_root') or '').strip() or '/books'

    def ok(self):
        return bool(self.base and self.key)

    def call(self, method, path, body=None, timeout=TIMEOUT):
        hdr = {'X-Api-Key': self.key, 'User-Agent': UA, 'Accept': 'application/json'}
        if body is not None:
            hdr['Content-Type'] = 'application/json'
        data = None if body is None else json.dumps(body).encode('utf-8')
        return self.http(method, self.base + path, hdr, data, timeout=timeout)

    def get(self, path, timeout=TIMEOUT):
        return _json(*self.call('GET', path, timeout=timeout))

    def lookup_raw(self, term, timeout=LOOKUP_TIMEOUT):
        return self.get('/api/v1/book/lookup?' + urllib.parse.urlencode({'term': term}), timeout=timeout)

    def add(self, foreign_id, term):
        """The one write: find the book again by its id, add it with the options' profiles and root, then search for it."""
        found = None
        for t in ('readarr:' + foreign_id, term):
            if not t:
                continue
            got = self.lookup_raw(t, timeout=TIMEOUT)
            for b in got if isinstance(got, list) else []:
                if isinstance(b, dict) and _foreign(b.get('foreignBookId')) == foreign_id:
                    found = b
                    break
            if found:
                break
        if not found:
            return {'ok': False, 'message': 'Bookshelf did not find that book again'}
        if _int(found.get('id')):
            return {'ok': True, 'message': 'already in Bookshelf', 'book_id': found['id']}
        author = dict(found.get('author')) if isinstance(found.get('author'), dict) else {}
        author.update(qualityProfileId=self.profile, metadataProfileId=self.meta, rootFolderPath=self.root, monitored=True,
                      monitorNewItems='none', addOptions={'monitor': 'none', 'searchForMissingBooks': False})
        body = dict(found, author=author, monitored=True, addOptions={'addType': 'manual', 'searchForNewBook': False})
        eds = body.get('editions')
        if isinstance(eds, list) and eds and not any(isinstance(e, dict) and e.get('monitored') for e in eds):
            body['editions'] = [dict(e, monitored=(i == 0)) if isinstance(e, dict) else e for i, e in enumerate(eds)]
        status, data = self.call('POST', '/api/v1/book', body)
        ans = _json(status, data)
        if not (isinstance(ans, dict) and _int(ans.get('id'))):
            if status == 400 and b'already' in (data or b'').lower():
                return {'ok': True, 'message': 'already in Bookshelf'}
            return {'ok': False, 'message': 'Bookshelf refused the add: HTTP %d' % status}
        bid = ans['id']
        s2, _ = self.call('POST', '/api/v1/command', {'name': 'BookSearch', 'bookIds': [bid]})
        if 200 <= s2 < 300:
            return {'ok': True, 'book_id': bid}
        return {'ok': True, 'book_id': bid, 'message': 'added; the search did not start (HTTP %d)' % s2}


def lookup_rows(items):
    """Bookshelf's lookup to the house link's rows (src/house-link.js checkBooks): at most LOOKUP_MAX. Pure."""
    out, seen = [], set()
    for b in items if isinstance(items, list) else []:
        if not isinstance(b, dict):
            continue
        fid, title = _foreign(b.get('foreignBookId')), _text(b.get('title'), 120)
        if not fid or not title or fid in seen:
            continue
        seen.add(fid)
        r = {'foreign_id': fid, 'title': title}
        a = _author_of(b, {})
        if a:
            r['author'] = a[:120]
        d = _day(b.get('releaseDate'))
        if d:
            r['year'] = int(d[:4])
        s = _text(b.get('seriesTitle'), 120)
        if s:
            r['series'] = s
        if _int(b.get('pageCount'), 0, 99999) is not None:
            r['pages'] = b['pageCount']
        rt = _rating(b.get('ratings'))
        if rt is not None:
            r['rating'] = rt
        c = _cover(b)
        if c:
            r['cover'] = c
        if _int(b.get('id')):
            r['in_library'] = True
        out.append(r)
        if len(out) >= LOOKUP_MAX:
            break
    return out


def lookup(cfg, term, http=None):
    """Bookshelf's search for the house link: ('ok', rows) or ('no_bookshelf' | 'not_answering', None). Never raises."""
    bs = Bookshelf(cfg, http or default_http)
    if not bs.ok():
        return 'no_bookshelf', None
    try:
        rows = lookup_rows(bs.lookup_raw(term))
    except Exception:   # a timeout, a refused connection: the same plain answer
        return 'not_answering', None
    return ('ok', rows) if rows else ('not_answering', None)


def check_action(a):
    if not isinstance(a, dict) or a.get('kind') != 'add' or not isinstance(a.get('id'), str):
        return None
    if not a['id'].startswith('ba-') or not a['id'][3:].isdigit() or len(a['id']) > 23:
        return None
    fid = _foreign(a.get('foreign_id'))
    if not fid:
        return None
    term = _text(a.get('term'), 100)
    return {'id': a['id'], 'foreign_id': fid, 'term': term}


def _due(state, key, every, now):
    return now - float(state.get(key) or 0) >= every


def run(cfg, now, http=None):
    """One pass from the add-on's loop. Returns the new state; its 'log' holds this pass's lines (no secret in any of them)."""
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    state['log'] = _pass(cfg, now, http or default_http, state)
    for line in state['log']:
        print('[home-collector] ' + line, flush=True)
    return state


def _pass(cfg, now, http, state):
    site, bs = site_base(cfg), Bookshelf(cfg, http)
    if not site:
        return ['books: no site link in the options']
    if not bs.ok():
        return ['books: Bookshelf not set in the options']
    site_hdr = {'Content-Type': 'application/json', 'User-Agent': UA, 'Accept': 'application/json'}
    post = lambda path, body: http('POST', site + path, site_hdr, json.dumps(body, separators=(',', ':')).encode('utf-8'))
    lines, added = [], False
    if _due(state, 'books_actions_at', ACTIONS_EVERY_S, now):
        state['books_actions_at'] = now
        ans = _json(*http('GET', site + '/books/actions', {'User-Agent': UA, 'Accept': 'application/json'}, None)) or {}
        results = []
        for raw in ans.get('actions') or [] if isinstance(ans, dict) else []:
            a = check_action(raw)
            if not a:
                lines.append('books: refused an action that is not an add')
                continue
            try:
                r = bs.add(a['foreign_id'], a['term'])
            except (OSError, ValueError) as e:
                r = {'ok': False, 'message': 'Bookshelf not reached: %s' % type(e).__name__}
            added = added or (r.get('ok') and 'book_id' in r)
            results.append(dict({'id': a['id']}, **{k: v for k, v in r.items() if k in ('ok', 'message', 'book_id')}))
        if results:
            status, _ = post('/books/done', {'v': 1, 'results': results})
            lines.append('books: %d adds done, %d ok, site answered %d' % (len(results), sum(1 for r in results if r['ok']), status))
    if added or _due(state, 'books_report_at', REPORT_EVERY_S, now):
        state['books_report_at'] = now   # stamped before the work, so a failing run also waits
        lines += report_pass(bs, now, post, cfg)
    return lines


def report_pass(bs, now, post, cfg=None):
    try:
        raw_books = bs.get('/api/v1/book')
    except (OSError, ValueError):
        raw_books = None
    if not isinstance(raw_books, list):
        if cfg is not None:
            cfg['failed'] = True
        return ['books: Bookshelf did not answer the book list; nothing sent']
    body, missed = {'v': 1}, []

    def safe(path):
        try:
            return bs.get(path)
        except (OSError, ValueError):
            return None
    raw_authors = safe('/api/v1/author')
    names = {}
    if isinstance(raw_authors, list):
        body['authors'] = author_rows(raw_authors)
        names = {a['author_id']: a['name'] for a in body['authors']}
    else:
        missed.append('authors')
    body['books'] = book_rows(raw_books, names)
    books = {b['book_id']: b for b in body['books']}
    stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    parts = (
        ('queue', lambda: queue_rows(safe('/api/v1/queue?' + urllib.parse.urlencode({'page': 1, 'pageSize': MAX_LIST, 'includeBook': 'true'})), books)),
        ('wanted', lambda: book_list(safe('/api/v1/wanted/missing?' + urllib.parse.urlencode({'page': 1, 'pageSize': MAX_LIST, 'sortKey': 'releaseDate', 'sortDirection': 'descending', 'includeAuthor': 'true'})), books)),
        ('calendar', lambda: book_list(safe('/api/v1/calendar?' + urllib.parse.urlencode({'start': stamp(now), 'end': stamp(now + CALENDAR_DAYS * 86400), 'unmonitored': 'false', 'includeAuthor': 'true'})), books)),
        ('history', lambda: history_rows(safe('/api/v1/history?' + urllib.parse.urlencode({'page': 1, 'pageSize': MAX_LIST, 'sortKey': 'date', 'sortDirection': 'descending', 'includeBook': 'true'})), books, stamp(now - HISTORY_DAYS * 86400))),
    )
    for name, read in parts:
        rows = read()
        if rows is None:
            missed.append(name)
        else:
            body[name] = rows
    status, data = post('/books/report', body)
    if not 200 <= status < 300 and cfg is not None:
        cfg['failed'] = True
    if cfg is not None:
        cfg['outcome'] = '%d books, %d authors' % (len(body['books']), len(body.get('authors') or []))
    return ['books: report sent, %d books, %d authors%s, site answered %d' % (
        len(body['books']), len(body.get('authors') or []), (', not read: ' + ', '.join(missed)) if missed else '', status)]
