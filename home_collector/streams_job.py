#!/usr/bin/env python3
"""The Home collector's streams job (roadmap R-0407 "Plex analytics", step 0.1; 10/29/26: no Tautulli, the add-on
samples Plex itself). collector.py's JOBS entry 'streams' calls run() every 15 seconds on the fast path (1.0.27, R-0484 step 3.6,
approved 11/9/26: "faster Streaming now"; every minute on the slow thread before).

Read only. Every 15 seconds it asks Plex for /status/sessions on the house network and folds each active stream into one record per
session, kept in this job's state, and posts them to theleases.org (src/plex.js) when a stream starts, stops, pauses or resumes
or its quality changes (the decision or the stream's resolution), at least every LIVE_EVERY_S while one is open, and otherwise every
POST_EVERY_S as before (1.0.27; every POST_EVERY_S whatever happened before). The fast path keeps the 5-second jobs' pace, so the
Plex read and the post each wait at most FAST_TIMEOUT_S. Counting is unchanged: playing_s adds the time between samples that
began playing, each gap at most PLAY_GAP_MAX_S, so a stream's played seconds come out the same, only measured more finely:
  POST <site>/plex/streams  { v: 1, sessions: [session], samples, failed }
    session  id (the Plex sessionKey and the minute it was first seen), account (Plex account id), user (the Plex account's
             name), kind (movie, episode, track, other), title (a movie with its year, or the show with S1 E2), product and
             platform (the player app and its system, such as Plex for Apple TV and tvOS: never the player's own name),
             local, decision (direct play, direct stream or transcode) with video and audio decisions, source and stream
             resolution, source bitrate and peak stream bitrate (kbps), state, progress (percent), first and last seen,
             active (seen in the last sample), playing_s (1.0.16: the seconds between samples that began with the stream
             playing; paused and buffering do not count, and a gap is counted at most PLAY_GAP_MAX_S)
    samples, failed  how many samples were taken since the last post and how many found Plex not answering
    fail_reason      (1.0.6, only when a sample failed) the last failure's short reason: "HTTP 401", the exception's class
                     name, or "not JSON, looks like XML or HTML"; never the address or the token. The log gets one line per
                     distinct failure, not one a minute.
NEVER sent: an IP or server address, a token, the player's machine identifier or its own name, a file path.
A session is posted while it plays and once more after it stops (active false), then forgotten here.
From 1.0.12 a post with no session and no failed sample is skipped when the last good post had no session either, but one still
goes at least every QUIET_EVERY_S (an hour); a failed sample always posts, so "Plex did not answer" shows as before.
Per-person play data is the owner's alone on the site, and never goes to an email, search, a log line or Today.

Python 3 standard library only. Plex's sessions shape (MediaContainer.Metadata[] with User, Player, Session, Media,
TranscodeSession) is from memory of its API, NOT checked against a live server; every field is read as optional.
"""
import json
import urllib.error
import urllib.request

POST_EVERY_S = 5 * 60
LIVE_EVERY_S = 60      # 1.0.27: while a stream is open, a post at least every minute (and at once on any change)
FAST_TIMEOUT_S = 5     # 1.0.27: the job runs on the fast path, so neither call may hold it long
PLAY_GAP_MAX_S = 180   # 1.0.16: samples are a minute apart; a longer gap (Plex not answering, the add-on restarting) counts at most this
MAX_SESSIONS = 60
UA = 'lease-home-collector/streams-1'
TEXT_OK = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ._+()-')


def default_http(method, url, headers=None, body=None, timeout=20):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        return e.code, e.read(4096)


def _json(status, data):
    if status < 200 or status >= 300:
        return None
    try:
        return json.loads((data or b'null').decode('utf-8'))
    except ValueError:
        return None


def why_not(status, data, exc=None):
    """A failed Plex answer's short reason (no address, no token): 'HTTP 401', the exception's class name, or not JSON."""
    if exc is not None:
        return type(exc).__name__[:40]
    if status < 200 or status >= 300:
        return 'HTTP %d' % status
    head = (data or b'').lstrip()[:1]
    return 'not JSON, looks like XML or HTML' if head == b'<' else 'not JSON'


def site_base(cfg):
    s = str(cfg.get('push_url') or '').rstrip('/')
    if s.endswith('/push'):
        s = s[:-5]
    return s if s.startswith('https://') and '/collector/' in s else ''


def _d(v):
    return v if isinstance(v, dict) else {}


def _int(v, lo=0, hi=10 ** 10):
    if isinstance(v, str) and v.isdigit():
        v = int(v)
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


def _text(v, n):
    return ' '.join(str(v).split())[:n] if isinstance(v, (str, int)) and not isinstance(v, bool) else ''


def _word(v, n=40):
    t = _text(v, n)
    return t if t and all(c in TEXT_OK for c in t) else None


def _res(v):
    """A resolution as Plex names it (1080, 4k, 720, sd) or a height in pixels, as short text, else None."""
    t = _text(v, 8).lower()
    if t.isdigit():
        return t + 'p' if t in ('480', '576', '720', '1080', '1440', '2160') else t
    return t if t in ('4k', 'sd', 'hd') else None


def _height_res(h):
    h = _int(h, 1, 5000)
    if not h:
        return None
    for lim, name in ((2160, '4k'), (1440, '1440p'), (1080, '1080p'), (720, '720p'), (576, '576p'), (480, '480p')):
        if h >= lim * 0.9:
            return name
    return 'sd'


def _decision(v):
    t = str(v or '').lower()
    return 'transcode' if t == 'transcode' else 'copy' if t == 'copy' else 'directplay' if t in ('directplay', '') else None


def one(m):
    """One Plex session to the fields the site keeps (no address, token, machine identifier or player name). Pure."""
    if not isinstance(m, dict):
        return None
    key = _text(m.get('sessionKey') or _d(m.get('Session')).get('id'), 40)
    if not key:
        return None
    user, player, sess = _d(m.get('User')), _d(m.get('Player')), _d(m.get('Session'))
    tc = _d(m.get('TranscodeSession'))
    media = m.get('Media')[0] if isinstance(m.get('Media'), list) and m.get('Media') and isinstance(m.get('Media')[0], dict) else {}
    kind = m.get('type') if m.get('type') in ('movie', 'episode', 'track') else 'other'
    if kind == 'episode':
        s, e = _int(m.get('parentIndex'), 0, 500), _int(m.get('index'), 0, 5000)
        title = _text(m.get('grandparentTitle'), 160) + (' S%d E%d' % (s, e) if s is not None and e is not None else '')
    elif kind == 'track':
        title = _text(m.get('grandparentTitle'), 100) + ' - ' + _text(m.get('title'), 100)
    else:
        y = _int(m.get('year'), 1880, 2100)
        title = _text(m.get('title'), 180) + (' (%d)' % y if y else '')
    vd = _decision(tc.get('videoDecision')) if tc else 'directplay'
    ad = _decision(tc.get('audioDecision')) if tc else 'directplay'
    overall = 'transcode' if 'transcode' in (vd, ad) else 'direct stream' if 'copy' in (vd, ad) else 'direct play'
    dur, off = _int(m.get('duration'), 1), _int(m.get('viewOffset'))
    local = player.get('local')
    loc = sess.get('location')
    state = player.get('state') if player.get('state') in ('playing', 'paused', 'buffering') else None
    return {'key': key, 'account': _int(user.get('id')), 'user': _text(user.get('title'), 40) or None, 'kind': kind,
            'title': title.strip() or None, 'product': _word(player.get('product')), 'platform': _word(player.get('platform')),
            'local': True if local in (True, 1, '1') or loc == 'lan' else False if local in (False, 0, '0') or loc == 'wan' else None,
            'decision': overall, 'video': vd, 'audio': ad,
            'source_res': _res(media.get('videoResolution')), 'stream_res': _height_res(tc.get('height')) if tc and vd == 'transcode' else _res(media.get('videoResolution')),
            'source_kbps': _int(media.get('bitrate'), 0, 1000000), 'kbps': _int(sess.get('bandwidth'), 0, 1000000),
            'state': state, 'progress': min(100, int(off * 100 / dur)) if dur and off is not None else None}


def signature(state):
    """What a post on change compares (1.0.27): each open stream's key, whether it is active, its state, decision and stream
    resolution. Pure."""
    return sorted([k, bool(v.get('active')), v.get('state') or '', v.get('decision') or '', v.get('stream_res') or ''] for k, v in (state.get('open') or {}).items())


def fold(state, sample, now):
    """Fold one sample (a list of one() records, or None when Plex did not answer) into state['open']. Pure but for state."""
    open_ = state.setdefault('open', {})
    state['samples'] = int(state.get('samples') or 0) + 1
    if sample is None:
        state['failed'] = int(state.get('failed') or 0) + 1
        return
    seen = set()
    for r in sample[:MAX_SESSIONS]:
        k = r['key']
        cur = open_.get(k)
        if cur is None or not cur.get('active'):
            if cur is not None:   # a key Plex used again after it stopped: the old one goes once more as stopped
                state.setdefault('closed', []).append(cur)
            cur = {'id': '%s-%d' % (k, int(now // 60)), 'first': int(now), 'peak_kbps': None, 'playing_s': 0}
            open_[k] = cur
        elif cur.get('state') == 'playing' and cur.get('last') is not None:
            # 1.0.16 (11/2/26): the time from the last sample to this one counts as played only if it began playing
            cur['playing_s'] = int(cur.get('playing_s') or 0) + max(0, min(int(now) - int(cur['last']), PLAY_GAP_MAX_S))
        seen.add(k)
        for f in ('account', 'user', 'kind', 'title', 'product', 'platform', 'local', 'decision', 'video', 'audio', 'source_res', 'stream_res', 'source_kbps', 'state', 'progress'):
            if r.get(f) is not None or f not in cur:
                cur[f] = r.get(f)
        if r.get('kbps') is not None:
            cur['peak_kbps'] = max(cur.get('peak_kbps') or 0, r['kbps'])
        if r.get('decision') == 'transcode':
            cur['transcoded'] = True
        cur['last'] = int(now)
        cur['active'] = True
    for k, cur in list(open_.items()):
        if k not in seen and cur.get('active'):
            cur['active'] = False


def payload(state):
    out = []
    for cur in list(state.get('closed') or []) + list((state.get('open') or {}).values()):
        out.append({'id': cur['id'], 'account': cur.get('account'), 'user': cur.get('user'), 'kind': cur.get('kind') or 'other', 'title': cur.get('title'),
                    'product': cur.get('product'), 'platform': cur.get('platform'), 'local': cur.get('local'), 'decision': cur.get('decision'),
                    'video': cur.get('video'), 'audio': cur.get('audio'), 'source_res': cur.get('source_res'), 'stream_res': cur.get('stream_res'),
                    'source_kbps': cur.get('source_kbps'), 'peak_kbps': cur.get('peak_kbps'), 'transcoded': bool(cur.get('transcoded')),
                    'state': cur.get('state'), 'progress': cur.get('progress'), 'first': cur['first'], 'last': cur.get('last', cur['first']),
                    'active': bool(cur.get('active')), 'playing_s': int(cur.get('playing_s') or 0)})
    body = {'v': 1, 'sessions': out[:MAX_SESSIONS * 2], 'samples': int(state.get('samples') or 0), 'failed': int(state.get('failed') or 0)}
    if body['failed'] and state.get('fail_reason'):
        body['fail_reason'] = state['fail_reason']
    return body


QUIET_EVERY_S = 60 * 60   # an idle post at least hourly (the site shows the last post's time; Plex down shows from a failure post)


def quiet(state, now):
    """True when this post can be skipped (add-on 1.0.12): no session open or stopped to send, no failed sample since the last
    post, the last good post also carried no session, and it went within QUIET_EVERY_S. Pure."""
    if state.get('closed') or state.get('open') or int(state.get('failed') or 0):
        return False
    return state.get('last_count') == 0 and now - float(state.get('sent_at') or 0) < QUIET_EVERY_S


def run(cfg, now, http=None):
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    http = http or default_http
    hdr = {'User-Agent': UA, 'Accept': 'application/json'}
    base, tok, site = str(cfg.get('plex_url') or '').rstrip('/'), str(cfg.get('plex_token') or ''), site_base(cfg)
    lines = []
    if not (base and tok and site):
        state['log'] = ['streams: Plex or the site link not set in the options']
        return state
    reason = None
    try:
        status, data = http('GET', base + '/status/sessions', dict(hdr, **{'X-Plex-Token': tok}), None, FAST_TIMEOUT_S)
        got = _json(status, data)
        if got is None:
            reason = why_not(status, data)
    except (OSError, ValueError) as e:
        got, reason = None, why_not(0, None, e)
    mc = got.get('MediaContainer') if isinstance(got, dict) else None
    rows = (mc.get('Metadata') or []) if isinstance(mc, dict) else None
    if rows is None and reason is None:
        reason = 'no MediaContainer'
    if reason and reason != state.get('fail_reason'):   # one line per distinct failure, not one a minute
        lines.append('streams: Plex /status/sessions answered %s' % reason)
    if reason:
        state['fail_reason'] = reason
    elif state.get('fail_reason') and not int(state.get('failed') or 0):
        state.pop('fail_reason', None)
    before = signature(state)
    fold(state, [r for r in (one(x) for x in rows) if r] if isinstance(rows, list) else None, now)
    since = now - float(state.get('posted_at') or 0)
    live = not reason and any(v.get('active') for v in (state.get('open') or {}).values())   # Plex not answering: the old pace
    changed = signature(state) != before and not reason
    due = since >= POST_EVERY_S or (changed and since >= 1) or (live and since >= LIVE_EVERY_S) or (state.get('closed') and since >= 1)
    if since >= POST_EVERY_S and quiet(state, now):
        # 1.0.12: nothing playing, nothing failed, and the last post said nothing was playing too: skip it, start the counts
        # again, and still post at least every QUIET_EVERY_S so the site's Streaming now keeps a fresh time
        state['posted_at'] = now
        state['samples'], state['failed'] = 0, 0
    elif due:
        state['posted_at'] = now
        body = payload(state)
        try:
            status, _ = http('POST', site + '/plex/streams', dict(hdr, **{'Content-Type': 'application/json'}), json.dumps(body).encode('utf-8'), FAST_TIMEOUT_S)
        except (OSError, ValueError):
            status = 0
        if status == 200:   # sent: stopped sessions are forgotten, the counts start again
            state['last_count'], state['sent_at'] = len(body['sessions']), now
            state['open'] = {k: v for k, v in (state.get('open') or {}).items() if v.get('active')}
            state['closed'], state['samples'], state['failed'] = [], 0, 0
            if not reason:
                state.pop('fail_reason', None)
        if status != 200 or since >= POST_EVERY_S:   # 1.0.27: a post a minute while something plays; the log keeps its old pace
            lines.append('streams: %d sessions sent, site answered %d' % (len(body['sessions']), status))
    state['log'] = lines
    for line in lines:
        print('[home-collector] ' + line, flush=True)
    return state
