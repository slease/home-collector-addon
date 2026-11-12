#!/usr/bin/env python3
"""The Home collector's Now showing job (roadmap R-0018 "Poster display", step 3.4, 10/31/26). collector.py's JOBS entry
'nowplaying' calls run() each poll while the `poster_player` option names a media_player (blank: off).

Read only. Each poll it asks Home Assistant for that one entity (GET http://supervisor/core/api/states/<entity> with the
Supervisor token) and, while it is playing or paused, posts what it plays, whatever the app (Plex, Netflix, the TV app):
  POST <site>/nowplaying  { v: 1, item: { title, sub, app, state, position_s, duration_s, poster } }   (item null once it stops)
The site answers { need: [poster id] }; the artwork then goes to <site>/nowplaying/img/<id> as its JPEG bytes. The artwork is read
through Home Assistant's own API (http://supervisor/core + entity_picture, a path on Home Assistant only, never another host),
JPEG only, at most 400 KB; its id is the sha256 of those bytes. Sent on a change, at least every 60 s while playing or paused,
and once with item null when it stops. NEVER sent: the entity id, a user, an address, an account, the picture's address.
Python 3 standard library only. The attribute names are Home Assistant's media_player ones (media_title, media_series_title,
media_season, media_episode, app_name, media_position, media_position_updated_at, media_duration, entity_picture), not
checked against a live Apple TV.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone

CORE = 'http://supervisor/core'
EVERY_S = 60           # resend at least this often while playing or paused
SEEK_S = 30            # a position this far off what was expected counts as a change (a seek)
MAX_IMG = 400 * 1024
UA = 'lease-home-collector/nowplaying-1'
ENTITY_RE = re.compile(r'^media_player\.[a-z0-9_]+$')
PIC_RE = re.compile(r'^/api/[A-Za-z0-9_./?=&%-]+$')


def supervisor_token():
    t = os.environ.get('SUPERVISOR_TOKEN')
    if t:
        return t
    try:
        with open('/run/s6/container_environment/SUPERVISOR_TOKEN') as f:
            return f.read().strip()
    except OSError:
        return None


def site_base(cfg):
    s = str(cfg.get('push_url') or '').rstrip('/')
    if s.endswith('/push'):
        s = s[:-5]
    return s if re.match(r'^https://theleases\.org/collector/[A-Za-z0-9_-]{20,64}$', s) else ''


def _text(v, n):
    if not isinstance(v, (str, int)) or isinstance(v, bool):
        return ''
    return ' '.join(re.sub(r'[\x00-\x1f\x7f]', ' ', str(v)).split())[:n]


def _secs(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0 or v > 24 * 3600:
        return None
    return round(float(v), 1)


def _when(s):
    try:
        d = datetime.fromisoformat(str(s).strip().replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def item_of(st, now):
    """Home Assistant's state for the player to the site's item, or None when it is not playing or paused. Pure."""
    if not isinstance(st, dict):
        return None
    state = str(st.get('state') or '').lower()
    if state not in ('playing', 'paused'):
        return None
    a = st.get('attributes') if isinstance(st.get('attributes'), dict) else {}
    series, title = _text(a.get('media_series_title'), 120), _text(a.get('media_title'), 120)
    app = _text(a.get('app_name'), 40) or None
    if series:
        s, e = a.get('media_season'), a.get('media_episode')
        ep = ('S%s E%s' % (_text(s, 4), _text(e, 5))) if _text(s, 4) and _text(e, 5) else ''
        sub = ', '.join(x for x in (ep, title if title != series else '') if x) or None
        title = series
    else:
        sub = None
    if not title:
        return None
    pos, dur = _secs(a.get('media_position')), _secs(a.get('media_duration'))
    if pos is not None and state == 'playing':
        at = _when(a.get('media_position_updated_at'))
        if at is not None and now > at:
            pos = round(pos + (now - at), 1)
    if pos is not None and dur:
        pos = min(pos, dur)
    return {'title': title, 'sub': sub, 'app': app, 'state': state, 'position_s': pos, 'duration_s': dur or None, 'poster': None}


def picture_path(st):
    """The entity_picture as a path on Home Assistant's own API, else None (an outside address is never fetched). Pure."""
    a = st.get('attributes') if isinstance(st, dict) and isinstance(st.get('attributes'), dict) else {}
    p = a.get('entity_picture')
    return p if isinstance(p, str) and PIC_RE.match(p) and '..' not in p and '//' not in p else None


def is_jpeg(b):
    return isinstance(b, (bytes, bytearray)) and len(b) > 4 and b[:3] == b'\xff\xd8\xff'


def fetch_art(http, tok, path):
    """(bytes, id) for a JPEG of at most MAX_IMG, else (None, None)."""
    try:
        status, data = http('GET', CORE + path, {'Authorization': 'Bearer ' + tok, 'User-Agent': UA, 'Accept': 'image/jpeg'}, None, 20)
    except (OSError, ValueError):
        return None, None
    if status != 200 or not is_jpeg(data) or len(data) > MAX_IMG:
        return None, None
    return bytes(data), hashlib.sha256(data).hexdigest()


def due(item, state, now):
    """Whether to post: a change of what or how, a seek, every EVERY_S while it plays or pauses, or a first stop. Pure."""
    if 'sig' not in state:
        return True
    if item is None:
        return state.get('sig') is not None
    sig = [item['title'], item['sub'], item['app'], item['state'], item['poster'], item['duration_s']]
    if sig != state.get('sig') or now - float(state.get('sent_at') or 0) >= EVERY_S:
        return True
    was, at = state.get('pos'), float(state.get('sent_at') or now)
    if item['position_s'] is None or was is None:
        return False
    expect = was + (now - at if item['state'] == 'playing' else 0)
    return abs(item['position_s'] - expect) > SEEK_S


def run(cfg, now, http):
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    ent, site, tok = str(cfg.get('poster_player') or '').strip().lower(), site_base(cfg), supervisor_token()
    if not ENTITY_RE.match(ent) or not site or not tok:
        if state.get('said') != 'setup':
            print('[home-collector] nowplaying: poster_player must be a media_player entity id (and push_url set)', flush=True)
        state['said'] = 'setup'
        return state
    try:
        status, data = http('GET', CORE + '/api/states/' + ent, {'Authorization': 'Bearer ' + tok, 'User-Agent': UA}, None, 20)
        st = json.loads((data or b'null').decode('utf-8')) if status == 200 else None
    except (OSError, ValueError):
        st, status = None, 0
    if st is None and status != 404:   # Home Assistant did not answer: say nothing, try again next poll
        return state
    item = item_of(st, now)
    art = None
    if item:
        path = picture_path(st)
        key = hashlib.sha256(path.encode('utf-8')).hexdigest() if path else None   # the picture's address never leaves or is kept
        if key != state.get('pic_key'):
            art, pid = fetch_art(http, tok, path) if path else (None, None)
            state['pic_key'], state['pic_id'] = key, pid
        item['poster'] = state.get('pic_id')
    if not due(item, state, now):
        return state
    hdr = {'User-Agent': UA, 'Content-Type': 'application/json'}
    try:
        code, ans = http('POST', site + '/nowplaying', hdr, json.dumps({'v': 1, 'item': item}, separators=(',', ':')).encode('utf-8'), 20)
        ans = json.loads((ans or b'{}').decode('utf-8')) if code == 200 else {}
    except (OSError, ValueError):
        code, ans = 0, {}
    if code != 200:
        if state.get('said') != 'refused %s' % code:
            print('[home-collector] nowplaying: the site answered %s' % code, flush=True)
        state['said'] = 'refused %s' % code
        return state
    state['said'] = None
    state['sig'] = None if item is None else [item['title'], item['sub'], item['app'], item['state'], item['poster'], item['duration_s']]
    state['pos'], state['sent_at'] = (item or {}).get('position_s'), now
    need = ans.get('need') if isinstance(ans, dict) else None
    if item and item['poster'] and isinstance(need, list) and item['poster'] in need:
        if art is None:
            path = picture_path(st)
            art, pid = fetch_art(http, tok, path) if path else (None, None)
            if pid != item['poster']:
                art = None
        if art:
            try:
                http('POST', site + '/nowplaying/img/' + item['poster'], {'User-Agent': UA, 'Content-Type': 'image/jpeg'}, art, 30)
            except (OSError, ValueError):
                pass
    return state
