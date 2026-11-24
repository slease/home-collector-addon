"""
The house link (1.0.32, roadmap R-0517, bug b-14a7baf8; state frames 1.0.33, R-0517 4.1, bug b-07533798): one outbound WebSocket to theleases.org, so the owner's Play on a Sonos
reaches Home Assistant in the house at once, with no port opened into it.

Why: Spotify's Web API cannot start or check a Sonos (11/12/26: it read an album playing while a Sonos speaker sat paused).
Home Assistant's Sonos integration can, on the LAN, and its media_player state says what the speaker really does.

What it does, and nothing more:
- Opens wss://theleases.org/collector/<token>/socket (the push link's token; the site checks it in constant time), sends a hello
  with this add-on's version and the speakers in the sonos_speakers option (entity ids and names), and pings every PING_S seconds.
- Takes only the site's fixed commands: play, pause, resume, next, volume, join and unjoin (1.0.33, R-0517 4.2), each on one
  speaker from sonos_speakers; a join's members are 1 to MEMBERS_MAX other speakers from that list, no duplicates. A play carries
  Spotify album or track URIs (spotify:album:ID, spotify:track:ID, up to 30 tracks) or a Plex ratingKey (digits); volume 0 to 100.
  check_command refuses anything else, and nothing from the site is ever run, evaluated or passed to any other Home Assistant
  action: the only calls are media_player.play_media, media_pause, media_play, media_next_track, volume_set, join (group_members
  from the list) and unjoin, on the listed entity,
  through the Supervisor's proxy to Core (homeassistant_api, which the add-on already had for reading states).
- A Plex play reads the album's section, artist and title from this machine's own Plex (plex_url, plex_token) by its key and sends
  Home Assistant's documented plex://{"library_name", "artist_name", "album_name"} form; the site never sends those words. Since
  1.0.38 (R-0581 1.2) the key may be a single track's, sent in the track form with "track_name", so one track plays.
- A Play of many tracks starts the first, answers, then adds the rest to the queue on a thread of its own; a command that comes
  while that runs is answered busy.
- Answers each command with Home Assistant's media_player state after the call (read back for up to READ_S seconds for a play,
  resume or pause) and, on failure, one fixed code (WHY), never free text.
- Says what each listed speaker is doing (1.0.33): it reads the listed media_player entities through the same Supervisor proxy
  every STATE_S seconds (and at once after a command) and sends one fixed `state` frame holding only the speakers that changed:
  { t: 'state', v: 1, speakers: [{ id, state, title, artist, album, volume, source }] }, state from STATES, the three texts
  stripped of control characters and markup and cut to TEXT_MAX, volume 0 to 100, source spotify, plex or other (worked out
  here from the content id, which is never sent), group the listed speakers Home Assistant groups it with (its group_members,
  leader first; any speaker not in sonos_speakers is dropped). A speaker goes in at most one frame every GAP_S seconds; the whole set goes
  once right after the hello. The site keeps only the latest, so the owner's pages can show Pause, Play, Next and Volume after
  a Play that Spotify cannot see.
- Runs on its own thread. A failure, a drop or a slow Home Assistant never touches the push loop. It reconnects with backoff
  (BACKOFF, with jitter), and with sonos_speakers empty it never connects.

- Guest light scenes (1.0.39, R-0035 1.3): the guest_scenes option lists at most SCENES_MAX Home Assistant
  scene entities with the name the site shows. The hello sends each one's place in that list (1 to SCENES_MAX), its name and
  whether it is the All off scene, never the entity id. The site's only scene command is { t: 'cmd', id, kind: 'scene', scene: N },
  and the only call it makes is scene.turn_on on the Nth listed scene. The All off scene (off: true; interior lights
  only) runs only when every presence entity in the entities option reads away, read from Home Assistant at that moment: anyone
  home is refused (someone_home), and a presence entity that is unknown, or none listed at all, is refused too (presence_unknown).
  The link connects when sonos_speakers or guest_scenes names something.
- Family looks and real light state (1.0.42, R-0588 1.1, approved 11/23/26): the family_scenes option lists at most FAMILY_MAX
  Home Assistant script entities (the house's own looks, Low light and Daytime) with the name the site shows. The hello sends
  family: [{ n, name }], never an entity id; the one command is { t: 'cmd', id, kind: 'family', scene: N }, and the only call
  is script.turn_on on the Nth listed script. The site offers them to the owner and household only. Every LIT_S seconds (and
  soon after a scene or family command) the add-on reads Home Assistant's states once and sends a `lit` frame when it changed:
  { t: 'lit', v: 1, scenes: [{ n, lit }] }, for each guest scene but All off, lit true when any light or switch that scene
  sets is on, false when none is, null when its lights cannot be read. Only that word per scene leaves the house: no light
  names, no entity ids.

Python's standard library only (a small RFC 6455 client: text frames, masking, ping, pong and close).
"""
import base64
import hashlib
import json
import os
import random
import re
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.request

CORE = 'http://supervisor/core/api'
HOST = 'theleases.org'
PUSH_RE = re.compile(r'^https://theleases\.org/collector/([A-Za-z0-9_-]{20,64})/push$')
SPEAKER_RE = re.compile(r'^media_player\.[a-z0-9_]{1,80}$')
SPOTIFY_RE = re.compile(r'^spotify:(album|track):[A-Za-z0-9]{10,40}$')
PLEX_KEY_RE = re.compile(r'^\d{1,12}$')
ID_RE = re.compile(r'^c-[0-9a-f]{16}$')
KINDS = ('play', 'pause', 'resume', 'next', 'volume', 'join', 'unjoin', 'scene', 'family')
SCENE_RE = re.compile(r'^scene\.[a-z0-9_]{1,80}$')
PRESENCE_RE = re.compile(r'^(person|device_tracker|binary_sensor)\.[a-z0-9_]{1,80}$')
SCENES_MAX = 6         # guest_scenes (1.0.39, R-0035)
SCRIPT_RE = re.compile(r'^script\.[a-z0-9_]{1,80}$')
FAMILY_MAX = 4         # family_scenes (1.0.42, R-0588 1.1)
LIT_S = 15             # read the guest scenes' lights this often for lit frames (1.0.42)
LIT_KICK_S = 3         # and this long after a scene or family command, once Home Assistant has moved them
LIGHT_RE = re.compile(r'^(light|switch)\.[a-z0-9_]{1,80}$')
SERVICE = {'pause': 'media_pause', 'resume': 'media_play', 'next': 'media_next_track', 'volume': 'volume_set', 'unjoin': 'unjoin'}
SERVICES = ('play_media', 'media_pause', 'media_play', 'media_next_track', 'volume_set', 'join', 'unjoin')
STATES = ('playing', 'paused', 'idle', 'off', 'buffering', 'on', 'standby', 'unavailable', 'unknown')
WHY = ('not_allowed', 'bad_command', 'ha_refused', 'ha_unreachable', 'no_plex', 'plex_not_found', 'not_started', 'busy', 'failed',
       'someone_home', 'presence_unknown')
SPEAKERS_MAX, URIS_MAX, NAME_MAX, FRAME_MAX = 12, 30, 40, 16 * 1024
MEMBERS_MAX = 9        # a join's members (1.0.33, R-0517 4.2)
TEXT_MAX = 120         # a state frame's title, artist and album
STATE_S = 5            # read the listed speakers this often for state frames
GAP_S = 2              # and never put one speaker in two frames less than this apart
SOURCES = ('spotify', 'plex', 'other')
BOOK_TERM_MAX = 100    # the site's book search (1.0.41, R-0568 0.2)
BOOK_WHY = ('no_bookshelf', 'not_answering', 'bad_command', 'busy')
# C0 and C1 controls, markup, the line and paragraph separators and the bidi controls (gate on #834)
CTRL_RE = re.compile('[\x00-\x1f\x7f-\x9f<>\u2028\u2029\u202a-\u202e\u2066-\u2069]+')
STATE_FRAME_MAX = 15000   # UTF-16 units per state frame: the site drops a message over 16 KB, measured as JavaScript counts
PING_S = 45            # the site answers "pong" without waking (Durable Object auto-response)
DEAD_S = 150           # nothing heard for this long: the socket is gone, reconnect
READ_S = 8             # a play, resume or pause: read the state back for this long, until it is what was asked
BACKOFF = (2, 5, 10, 30, 60, 120, 300)
STEADY_S = 120         # a socket that stayed up this long starts the backoff over
VERSION = None         # set by start() from collector.VERSION


def socket_url(push_url):
    """wss://theleases.org/collector/<token>/socket from the push link, or None when the link is not theleases.org's. Pure."""
    m = PUSH_RE.fullmatch(str(push_url or '').strip())
    return ('/collector/%s/socket' % m.group(1)) if m else None


def speakers_from(opts):
    """The sonos_speakers option, checked: [(entity_id, name)], at most SPEAKERS_MAX, or raises ValueError. Pure."""
    out, seen = [], set()
    for s in opts.get('sonos_speakers') or []:
        eid = str((s or {}).get('entity') or '').strip().lower()
        if not SPEAKER_RE.fullmatch(eid):
            raise ValueError('sonos_speakers: "%s" must be a media_player entity id' % eid[:60])
        if eid in seen:
            continue
        seen.add(eid)
        name = re.sub(r'[\x00-\x1f<>]+', ' ', str((s or {}).get('name') or '')).strip()[:NAME_MAX] or eid.split('.', 1)[1].replace('_', ' ').title()
        out.append((eid, name))
    if len(out) > SPEAKERS_MAX:
        raise ValueError('sonos_speakers: at most %d speakers' % SPEAKERS_MAX)
    return out


def scenes_from(opts):
    """The guest_scenes option, checked: [(entity_id, name, off)], at most SCENES_MAX, at most one All off, or raises ValueError. Pure."""
    out, seen = [], set()
    for s in opts.get('guest_scenes') or []:
        eid = str((s or {}).get('entity') or '').strip().lower()
        if not SCENE_RE.fullmatch(eid):
            raise ValueError('guest_scenes: "%s" must be a scene entity id' % eid[:60])
        if eid in seen:
            continue
        seen.add(eid)
        name = re.sub(r'[\x00-\x1f<>]+', ' ', str((s or {}).get('name') or '')).strip()[:NAME_MAX] or eid.split('.', 1)[1].replace('_', ' ').title()
        out.append((eid, name, (s or {}).get('all_off') is True))
    if len(out) > SCENES_MAX:
        raise ValueError('guest_scenes: at most %d scenes' % SCENES_MAX)
    if sum(1 for x in out if x[2]) > 1:
        raise ValueError('guest_scenes: only one scene can be the All off scene')
    return out


def family_from(opts):
    """The family_scenes option (1.0.42), checked: [(entity_id, name)], script entities only, at most FAMILY_MAX, or raises
    ValueError. Kept apart from guest_scenes: its own list, its own numbers, its own call (script.turn_on). Pure."""
    out, seen = [], set()
    for s in opts.get('family_scenes') or []:
        eid = str((s or {}).get('entity') or '').strip().lower()
        if not SCRIPT_RE.fullmatch(eid):
            raise ValueError('family_scenes: "%s" must be a script entity id' % eid[:60])
        if eid in seen:
            continue
        seen.add(eid)
        name = re.sub(r'[\x00-\x1f<>]+', ' ', str((s or {}).get('name') or '')).strip()[:NAME_MAX] or eid.split('.', 1)[1].replace('_', ' ').title()
        out.append((eid, name))
    if len(out) > FAMILY_MAX:
        raise ValueError('family_scenes: at most %d scripts' % FAMILY_MAX)
    return out


def presence_from(opts):
    """The presence entities in the entities option (kind presence), the ones All off checks. Pure."""
    out = []
    for s in opts.get('entities') or []:
        eid = str((s or {}).get('entity') or '').strip().lower()
        if (s or {}).get('kind') == 'presence' and PRESENCE_RE.fullmatch(eid) and eid not in out:
            out.append(eid)
    return out


def hello(speakers, version, scenes=(), books=False, family=()):
    m = {'t': 'hello', 'v': 1, 'addon': version, 'speakers': [{'id': e, 'name': n} for e, n in speakers]}
    if scenes:
        m['scenes'] = [{'n': i + 1, 'name': n, 'off': off} for i, (_, n, off) in enumerate(scenes)]
    if family:   # 1.0.42 (R-0588 1.1): the family looks, by place and name, never the script's entity id
        m['family'] = [{'n': i + 1, 'name': n} for i, (_, n) in enumerate(family)]
    if books:   # 1.0.41 (R-0568 0.2): Bookshelf is set up, so the site may ask its book search over this link
        m['books'] = True
    return m


def books_set(opts):
    """Whether the books job's Bookshelf options are both set (1.0.41). Pure."""
    return bool(str(opts.get('bookshelf_url') or '').strip() and str(opts.get('bookshelf_key') or '').strip())


def check_books(m):
    """The site's book search (1.0.41): { t: 'books', id, term } with nothing else, term 2 to BOOK_TERM_MAX characters of
    plain text. Returns (id, term) or (id or None, None). Pure."""
    cid = m.get('id') if isinstance(m, dict) and isinstance(m.get('id'), str) and ID_RE.fullmatch(m.get('id')) else None
    if not isinstance(m, dict) or set(m.keys()) != {'t', 'id', 'term'} or m.get('t') != 'books' or not cid:
        return cid, None
    t = m.get('term')
    if not isinstance(t, str) or not 2 <= len(t) <= BOOK_TERM_MAX or CTRL_RE.search(t):
        return cid, None
    return cid, t


def run_books(cid, term, cfg, http, mod=None):
    """One book search through books_job.lookup, as the answer frame. Never raises."""
    try:
        m = mod or _books_module()
        why, rows = m.lookup(cfg, term, http)
    except Exception:
        why, rows = 'not_answering', None
    if why == 'ok':
        return {'t': 'books', 'id': cid, 'ok': True, 'books': rows}
    return {'t': 'books', 'id': cid, 'ok': False, 'why': why if why in BOOK_WHY else 'not_answering'}


def _books_module():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'books_job.py')
    spec = importlib.util.spec_from_file_location('books_job', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def check_command(m, allowed, scenes=0, family=0):
    """The site's command, checked against the fixed shapes and the speaker allowlist. Returns (cmd, None) or (None, why). Pure."""
    if not isinstance(m, dict) or m.get('t') != 'cmd':
        return None, 'bad_command'
    keys = set(m.keys())
    if not keys <= {'t', 'id', 'kind', 'speaker', 'media', 'volume', 'members', 'scene'}:
        return None, 'bad_command'
    cid, kind, spk = m.get('id'), m.get('kind'), m.get('speaker')
    if not isinstance(cid, str) or not ID_RE.fullmatch(cid) or kind not in KINDS:
        return None, 'bad_command'
    if kind in ('scene', 'family'):   # a guest light scene by its place in guest_scenes, or (1.0.42) a family look by its place in
        n = m.get('scene')            # family_scenes (scenes, family: how many there are); nothing else rides along
        if keys != {'t', 'id', 'kind', 'scene'} or not isinstance(n, int) or isinstance(n, bool):
            return None, 'bad_command'
        if not 1 <= n <= (scenes if kind == 'scene' else family):
            return None, 'not_allowed'
        return {'id': cid, 'kind': kind, 'scene': n}, None
    if 'scene' in keys:
        return None, 'bad_command'
    if not isinstance(spk, str) or not SPEAKER_RE.fullmatch(spk) or spk not in allowed:
        return None, 'not_allowed'
    cmd = {'id': cid, 'kind': kind, 'speaker': spk}
    if kind == 'play':
        md = m.get('media')
        if not isinstance(md, dict) or len(md) != 1:
            return None, 'bad_command'
        if 'spotify' in md:
            u = md['spotify']
            if not isinstance(u, list) or not 1 <= len(u) <= URIS_MAX or not all(isinstance(x, str) and SPOTIFY_RE.fullmatch(x) for x in u):
                return None, 'bad_command'
            if len(u) > 1 and not all(x.startswith('spotify:track:') for x in u):
                return None, 'bad_command'
            cmd['spotify'] = list(u)
        elif 'plex' in md:
            if not isinstance(md['plex'], str) or not PLEX_KEY_RE.fullmatch(md['plex']):
                return None, 'bad_command'
            cmd['plex'] = md['plex']
        else:
            return None, 'bad_command'
    elif 'media' in keys:
        return None, 'bad_command'
    if kind == 'volume':
        v = m.get('volume')
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 100:
            return None, 'bad_command'
        cmd['volume'] = v
    elif 'volume' in keys:
        return None, 'bad_command'
    if kind == 'join':
        mem = m.get('members')
        if not isinstance(mem, list) or not 1 <= len(mem) <= MEMBERS_MAX:
            return None, 'bad_command'
        if not all(isinstance(x, str) and SPEAKER_RE.fullmatch(x) for x in mem) or len(set(mem)) != len(mem) or spk in mem:
            return None, 'bad_command'
        if not all(x in allowed for x in mem):
            return None, 'not_allowed'
        cmd['members'] = list(mem)
    elif 'members' in keys:
        return None, 'bad_command'
    return cmd, None


def plex_media_id(meta):
    """Home Assistant's plex:// id for one Plex album's or (1.0.38, R-0581 1.2) one track's metadata, or None. Pure.
    An album: librarySectionTitle, parentTitle (the artist), title. A track: librarySectionTitle, grandparentTitle (the album
    artist), parentTitle (the album), title (the track), sent as Home Assistant's documented track form with track_name."""
    if not isinstance(meta, dict) or meta.get('type') not in ('album', 'track'):
        return None
    lib = meta.get('librarySectionTitle')
    if meta.get('type') == 'album':
        artist, album, track = meta.get('parentTitle'), meta.get('title'), None
    else:
        artist, album, track = meta.get('grandparentTitle'), meta.get('parentTitle'), meta.get('title')
    if not all(isinstance(x, str) and x.strip() for x in (lib, artist, album)) or (meta.get('type') == 'track' and not (isinstance(track, str) and track.strip())):
        return None
    d = {'library_name': lib, 'artist_name': artist, 'album_name': album}
    if track is not None:
        d['track_name'] = track
    return 'plex://' + json.dumps(d, separators=(', ', ': '))


def plex_album(cfg, key, http):
    """(media_content_id, None) for a Plex album, or (1.0.38) a single track, by its ratingKey from this machine's Plex, or (None, why)."""
    purl, ptok = str(cfg.get('plex_url') or '').rstrip('/'), str(cfg.get('plex_token') or '')
    if not purl or not ptok:
        return None, 'no_plex'
    try:
        status, data = http('GET', purl + '/library/metadata/' + key, {'X-Plex-Token': ptok, 'Accept': 'application/json'}, None, 15)
    except Exception:
        return None, 'plex_not_found'
    if status != 200:
        return None, 'plex_not_found'
    try:
        md = (json.loads(data.decode('utf-8')).get('MediaContainer') or {}).get('Metadata') or []
    except (ValueError, AttributeError):
        return None, 'plex_not_found'
    mid = plex_media_id(md[0] if md else None)
    return (mid, None) if mid else (None, 'plex_not_found')


def calls_for(cmd, plex_id=None):
    """The Home Assistant actions a checked command makes, in order: [(service, data)]. Pure."""
    e = cmd['speaker']
    if cmd['kind'] == 'play':
        if 'spotify' in cmd:
            u = cmd['spotify']
            return [('play_media', {'entity_id': e, 'media_content_type': 'music', 'media_content_id': x, 'enqueue': 'replace' if i == 0 else 'add'}) for i, x in enumerate(u)]
        return [('play_media', {'entity_id': e, 'media_content_type': 'music', 'media_content_id': plex_id, 'enqueue': 'replace'})]
    if cmd['kind'] == 'volume':
        return [('volume_set', {'entity_id': e, 'volume_level': round(cmd['volume'] / 100.0, 2)})]
    if cmd['kind'] == 'join':
        return [('join', {'entity_id': e, 'group_members': list(cmd['members'])})]
    return [(SERVICE[cmd['kind']], {'entity_id': e})]


def split_calls(calls):
    """(now, later): a Play of many tracks starts the first and answers; the rest are added to the queue after the answer, so a
    30-track Play never outlasts the site's answer time (gate finding on #828). Anything else is all now. Pure."""
    if len(calls) > 1 and all(c[0] == 'play_media' for c in calls):
        return calls[:1], calls[1:]
    return calls, []


def enqueue_rest(later, tok, http, log, name):
    """Add the rest of a Play's tracks to the queue, in order (enqueue add); stops at the first refusal. Never raises."""
    done = 0
    for service, data in later:
        if ha_call(tok, service, data, http):
            log('house link: queued %d of %d more tracks on %s, then Home Assistant refused one' % (done, len(later), name))
            return done
        done += 1
    log('house link: queued %d more track%s on %s' % (done, '' if done == 1 else 's', name))
    return done


def want_state(kind):
    return {'play': ('playing',), 'resume': ('playing',), 'pause': ('paused', 'idle')}.get(kind)


def ha_call(tok, service, data, http):
    """One media_player action through the Supervisor proxy. Returns None or a WHY code. Only media_player.* ever."""
    if service not in SERVICES:
        return 'bad_command'
    try:
        status, _ = http('POST', CORE + '/services/media_player/' + service, {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'},
                         json.dumps(data).encode('utf-8'), 30)
    except Exception:
        return 'ha_unreachable'
    return None if 200 <= status < 300 else 'ha_refused'


def ha_state(tok, entity, http):
    """(state, volume 0 to 100 or None) of one media_player, or ('unknown', None)."""
    try:
        status, data = http('GET', CORE + '/states/' + entity, {'Authorization': 'Bearer ' + tok}, None, 10)
        if status != 200:
            return 'unknown', None
        j = json.loads(data.decode('utf-8'))
    except Exception:
        return 'unknown', None
    st = j.get('state') if isinstance(j, dict) else None
    st = st if st in STATES else 'unknown'
    v = (j.get('attributes') or {}).get('volume_level') if isinstance(j, dict) else None
    vol = int(round(float(v) * 100)) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 else None
    return st, vol


def run_command(cmd, cfg, tok, http, sleep=time.sleep, clock=time.monotonic, later=None):
    """Do one checked command and read the speaker back. Returns the result frame. Never raises. later: a list that gets the
    calls left for after the answer (the rest of a many-track Play, split_calls); with none given they are made here."""
    out = {'t': 'result', 'id': cmd['id'], 'ok': False, 'state': 'unknown'}
    try:
        plex_id = None
        if cmd['kind'] == 'play' and 'plex' in cmd:
            plex_id, why = plex_album(cfg, cmd['plex'], http)
            if why:
                out['why'] = why
                return out
        now, rest = split_calls(calls_for(cmd, plex_id))
        if later is None:
            now = now + rest
        for service, data in now:
            why = ha_call(tok, service, data, http)
            if why:
                out['why'] = why
                out['state'] = ha_state(tok, cmd['speaker'], http)[0]
                return out
        want = want_state(cmd['kind'])
        end = clock() + (READ_S if want else 0)
        st, vol = ha_state(tok, cmd['speaker'], http)
        while want and st not in want and clock() < end:
            sleep(0.5)
            st, vol = ha_state(tok, cmd['speaker'], http)
        out['state'] = st
        if vol is not None:
            out['volume'] = vol
        if want and st not in want:
            out['why'] = 'not_started' if cmd['kind'] != 'pause' else 'failed'
            return out
        out['ok'] = True
        if later is not None:
            later.extend(rest)
        return out
    except Exception:
        out['why'] = 'failed'
        return out


def presence_now(tok, entity, http):
    """'home', 'away' or 'unknown' for one presence entity, read from Home Assistant now (the collector's own mapping)."""
    try:
        status, data = http('GET', CORE + '/states/' + entity, {'Authorization': 'Bearer ' + tok}, None, 10)
        if status != 200:
            return 'unknown'
        j = json.loads(data.decode('utf-8'))
    except Exception:
        return 'unknown'
    s = str((j or {}).get('state') if isinstance(j, dict) else '').strip().lower()
    if s in ('unavailable', 'unknown', 'none', ''):
        return 'unknown'
    return 'home' if s in ('home', 'on') else 'away'


def run_scene(cmd, scenes, presence, tok, http):
    """Turn on one checked guest scene. All off only with every presence entity away (none listed, or any unknown: refused).
    Returns the result frame. Never raises. The only call is scene.turn_on on a listed scene."""
    out = {'t': 'result', 'id': cmd['id'], 'ok': False, 'state': 'unknown'}
    try:
        n = cmd.get('scene')
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(scenes):
            out['why'] = 'bad_command'   # never scenes[-1]: a number outside the list presses nothing
            return out
        eid, _, off = scenes[n - 1]
        if off:
            seen = [presence_now(tok, e, http) for e in presence]
            if 'home' in seen:
                out['why'] = 'someone_home'
                return out
            if not seen or 'unknown' in seen:
                out['why'] = 'presence_unknown'
                return out
        try:
            status, _ = http('POST', CORE + '/services/scene/turn_on', {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'},
                             json.dumps({'entity_id': eid}).encode('utf-8'), 30)
        except Exception:
            out['why'] = 'ha_unreachable'
            return out
        if not 200 <= status < 300:
            out['why'] = 'ha_refused'
            return out
        out['ok'], out['state'] = True, 'on'
        return out
    except Exception:
        out['why'] = 'failed'
        return out


def run_family(cmd, family, tok, http):
    """Run one checked family look (1.0.42): script.turn_on on the Nth listed script, nothing else. Returns the result frame.
    Never raises."""
    out = {'t': 'result', 'id': cmd['id'], 'ok': False, 'state': 'unknown'}
    try:
        n = cmd.get('scene')
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(family):
            out['why'] = 'bad_command'
            return out
        eid = family[n - 1][0]
        try:
            status, _ = http('POST', CORE + '/services/script/turn_on', {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'},
                             json.dumps({'entity_id': eid}).encode('utf-8'), 30)
        except Exception:
            out['why'] = 'ha_unreachable'
            return out
        if not 200 <= status < 300:
            out['why'] = 'ha_refused'
            return out
        out['ok'], out['state'] = True, 'on'
        return out
    except Exception:
        out['why'] = 'failed'
        return out


# ---- lit frames (1.0.42, R-0588 1.1)

def lit_frame(scenes, states):
    """The lit frame for the guest scenes from Home Assistant's /states list: for each scene but All off, whether any light or
    switch it sets is on (true), none is (false), or it cannot be told (null: the scene or its lights not read). Only n and
    that word go out. Pure."""
    by = {}
    for st in states if isinstance(states, list) else []:
        if isinstance(st, dict) and isinstance(st.get('entity_id'), str):
            by[st['entity_id']] = st
    out = []
    for i, (eid, _, off) in enumerate(scenes):
        if off:
            continue
        sc = by.get(eid) or {}
        members = (sc.get('attributes') or {}).get('entity_id') if isinstance(sc.get('attributes'), dict) else None
        lights = [x for x in members if isinstance(x, str) and LIGHT_RE.fullmatch(x)] if isinstance(members, list) else []
        seen = [str((by.get(x) or {}).get('state') or '').lower() for x in lights]
        seen = [x for x in seen if x in ('on', 'off')]
        out.append({'n': i + 1, 'lit': (('on' in seen) if seen else None)})
    return {'t': 'lit', 'v': 1, 'scenes': out}


def read_states(tok, http):
    """Home Assistant's whole /states list, one read. Raises on failure."""
    status, data = http('GET', CORE + '/states', {'Authorization': 'Bearer ' + tok}, None, 20)
    if status != 200:
        raise RuntimeError('states read: HTTP %s' % status)
    return json.loads(data.decode('utf-8'))


def watch_lit(w, scenes, tok, http, stop, kick, log, every=LIT_S):
    """The lit thread (1.0.42): a frame at once, then every `every` seconds (or LIT_KICK_S after kick, a command) a fresh read,
    sent only when it changed. A failed send ends it (the reader reconnects). Never raises."""
    last, said, first = None, False, True
    while first or not stop.is_set():
        if not first:
            if kick.wait(every):
                kick.clear()
                stop.wait(LIT_KICK_S)
            if stop.is_set():
                return
        first = False
        try:
            f = lit_frame(scenes, read_states(tok, http))
        except Exception as e:
            if not said:
                log('house link: reading the lights failed (%s); trying again' % (str(e)[:80] or type(e).__name__))
                said = True
            continue
        if f == last:
            continue
        try:
            w.send_text(json.dumps(f, separators=(',', ':')))
        except Exception:
            return
        last = f


# ---- state frames (1.0.33)

def clean_text(v):
    """A short plain string: control characters and markup out, spaces collapsed, cut to TEXT_MAX. Pure."""
    if not isinstance(v, str):
        return ''
    return re.sub(r'\s+', ' ', CTRL_RE.sub(' ', v)).strip()[:TEXT_MAX]


def source_of(attrs):
    """spotify, plex or other, from the content id and app name Home Assistant reports; only the word is ever sent. Pure."""
    cid = str(attrs.get('media_content_id') or '').lower()
    app = str(attrs.get('app_name') or attrs.get('source') or '').lower()
    if 'spotify' in cid or 'spotify' in app:
        return 'spotify'
    if 'plex' in cid or 'plex' in app:
        return 'plex'
    return 'other'


def media_from(entity, j, allowed=None):
    """One speaker's state entry from Home Assistant's /states answer, in the fixed shape, or None when it is not one. allowed:
    the listed speakers, the only ones a group may name (Home Assistant's group_members, leader first, others dropped). Pure."""
    if not isinstance(j, dict):
        return None
    st = j.get('state')
    a = j.get('attributes') if isinstance(j.get('attributes'), dict) else {}
    v = a.get('volume_level')
    vol = int(round(float(v) * 100)) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 else None
    out = {'id': entity, 'state': st if st in STATES else 'unknown', 'title': clean_text(a.get('media_title')),
           'artist': clean_text(a.get('media_artist')), 'album': clean_text(a.get('media_album_name')), 'source': source_of(a)}
    if vol is not None:
        out['volume'] = vol
    gm = a.get('group_members')
    if isinstance(gm, list) and allowed is not None:
        g = []
        for x in gm:
            if isinstance(x, str) and SPEAKER_RE.fullmatch(x) and x in allowed and x not in g:
                g.append(x)
        out['group'] = g[:SPEAKERS_MAX] if len(g) > 1 else []
    return out


def ha_media(tok, entity, http, allowed=None):
    """One listed speaker's state entry through the Supervisor proxy, or None when it could not be read."""
    if not SPEAKER_RE.fullmatch(entity):
        return None
    try:
        status, data = http('GET', CORE + '/states/' + entity, {'Authorization': 'Bearer ' + tok}, None, 10)
        if status != 200:
            return None
        return media_from(entity, json.loads(data.decode('utf-8')), allowed)
    except Exception:
        return None


class Watcher:
    """What each listed speaker said last, so only changes are sent, and none more often than GAP_S per speaker. An entry counts as
    sent only once the frame holding it went out (mark), so a frame that failed is tried again on the next tick."""

    def __init__(self, speakers, tok, http, clock=time.monotonic):
        self.ids = [e for e, _ in speakers]
        self.tok, self.http, self.clock = tok, http, clock
        self.last, self.sent_at = {}, {}

    def tick(self):
        """Read every listed speaker; return the state frames for those that changed (and may go now), each under
        STATE_FRAME_MAX as the site measures it: [] when nothing changed."""
        now, out = self.clock(), []
        for e in self.ids:
            m = ha_media(self.tok, e, self.http, set(self.ids))
            if m is None or m == self.last.get(e):
                continue
            if now - self.sent_at.get(e, -GAP_S) < GAP_S:
                continue   # changed again too soon: it goes on a later tick, still unsent
            out.append(m)
        return frames_of(out)

    def mark(self, frame):
        """The frame went out: its speakers count as sent."""
        now = self.clock()
        for m in frame['speakers']:
            self.last[m['id']], self.sent_at[m['id']] = m, now


def encoded(frame):
    return json.dumps(frame, separators=(',', ':'), ensure_ascii=False)


def units(s):
    """The length JavaScript gives a string (UTF-16 code units), which is what the site's 16 KB cap counts. Pure."""
    return len(s.encode('utf-16-le')) // 2


def frames_of(entries, limit=STATE_FRAME_MAX):
    """Speaker entries packed in order into as few state frames as fit under limit (UTF-16 units), one entry at least each. Pure."""
    frames, cur = [], []
    for m in entries:
        if cur and units(encoded({'t': 'state', 'v': 1, 'speakers': cur + [m]})) > limit:
            frames.append({'t': 'state', 'v': 1, 'speakers': cur})
            cur = []
        cur.append(m)
    if cur:
        frames.append({'t': 'state', 'v': 1, 'speakers': cur})
    return frames


def send_state(w, frame):
    w.send_text(encoded(frame))


def watch(w, watcher, stop, kick, log, every=STATE_S):
    """The state thread: the full set at once (off the reader thread, so a slow Home Assistant never holds the pings), then a tick
    every `every` seconds or at once when kick is set (after a command), until stop. A failed send means the socket is gone: the
    thread ends and the reader reconnects. Anything else is logged once and the thread carries on. Never raises."""
    said, first = False, True
    while first or not stop.is_set():   # the first set goes even if the reader stopped at once; a closed socket just fails it
        if not first:
            kick.wait(every)
            kick.clear()
            if stop.is_set():
                return
        first = False
        try:
            frames = watcher.tick()
        except Exception as e:
            if not said:
                log('house link: reading the speakers failed (%s); trying again' % (str(e)[:80] or type(e).__name__))
                said = True
            continue
        for f in frames:
            try:
                send_state(w, f)
            except Exception as e:   # a dropped socket: the reader sees it too and reconnects
                log('house link: a state frame did not go (%s)' % (str(e)[:80] or type(e).__name__))
                return
            watcher.mark(f)


# ---- a small RFC 6455 client

class Closed(Exception):
    pass


def ws_key():
    return base64.b64encode(os.urandom(16)).decode('ascii')


# RFC 6455's fixed GUID, public, in two pieces so the public mirror's token scan (tools/addon-mirror.mjs) does not read it as a secret
WS_GUID = '258EAFA5-E914' + '-47DA-95CA-C5AB0DC85B11'


def ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode('ascii')).digest()).decode('ascii')


def frame(opcode, payload, mask=None):
    """One client frame: FIN set, masked (a client must mask). Pure apart from the random mask when none is given."""
    b = payload if isinstance(payload, bytes) else payload.encode('utf-8')
    mask = mask or os.urandom(4)
    n = len(b)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack('!H', n)
    else:
        head += bytes([0x80 | 127]) + struct.pack('!Q', n)
    return head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(b))


class WS:
    def __init__(self, sock):
        self.sock, self.buf, self.lock = sock, b'', threading.Lock()
        self.msg, self.kind = b'', None   # a fragmented message so far: kept across a ping and across an idle wait

    def _read(self, n, mid=True):
        """n bytes. Bytes are taken off the buffer only once all n are there, so a timeout leaves it as it was; but once a
        frame's header has been taken (mid), a timeout would leave the reader inside a frame, so it is a drop (Closed)."""
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                if mid:
                    raise Closed('timed out in the middle of a frame')
                raise
            if not chunk:
                raise Closed('the site closed the socket')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, opcode, payload):
        with self.lock:
            self.sock.sendall(frame(opcode, payload))

    def send_text(self, s):
        self.send(1, s)

    def recv(self):
        """The next text message (str), or None for a control frame handled here. Raises Closed, or socket.timeout only between
        frames (idle). A server's frames are never masked; a control frame is 125 bytes or less and never fragmented."""
        while True:
            h = self._read(2, mid=False)
            fin, op, masked, n = h[0] & 0x80, h[0] & 0x0f, h[1] & 0x80, h[1] & 0x7f
            if h[0] & 0x70:
                raise Closed('a frame with reserved bits set')
            if masked:
                raise Closed('a masked frame from the site')
            if op >= 8 and (n > 125 or not fin):
                raise Closed('a control frame over 125 bytes or fragmented')
            if n == 126:
                n = struct.unpack('!H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('!Q', self._read(8))[0]
            if n > FRAME_MAX:
                raise Closed('a frame over the limit')
            data = self._read(n) if n else b''
            if op == 8:
                try:
                    self.send(8, data[:2])
                except OSError:
                    pass
                raise Closed('closed by the site (%s)' % (struct.unpack('!H', data[:2])[0] if len(data) >= 2 else 'no code'))
            if op == 9:
                self.send(10, data)
                return None
            if op == 10:
                return None
            if op in (1, 2):
                if self.kind is not None:
                    raise Closed('a new message inside a fragmented one')
                self.kind = op
            elif op == 0:
                if self.kind is None:
                    raise Closed('a continuation with no message')
            else:
                raise Closed('an unknown frame')
            self.msg += data
            if len(self.msg) > FRAME_MAX:
                raise Closed('a message over the limit')
            if fin:
                msg, kind = self.msg, self.kind
                self.msg, self.kind = b'', None
                return msg.decode('utf-8', 'replace') if kind == 1 else None

    def close(self):
        try:
            self.send(8, struct.pack('!H', 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def connect(path, timeout=20, host=HOST):
    """Open wss://theleases.org<path>. Returns a WS or raises. Only ever theleases.org on 443."""
    if not re.fullmatch(r'/collector/[A-Za-z0-9_-]{20,64}/socket', path):
        raise ValueError('refusing to connect anywhere but the collector socket')
    raw = socket.create_connection((host, 443), timeout=timeout)
    s = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    key = ws_key()
    req = ('GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
           'Sec-WebSocket-Version: 13\r\nUser-Agent: lease-home-collector/1\r\n\r\n') % (path, host, key)
    s.sendall(req.encode('ascii'))
    head = b''
    while b'\r\n\r\n' not in head:
        chunk = s.recv(4096)
        if not chunk:
            raise Closed('the site closed during the handshake')
        head += chunk
        if len(head) > 16384:
            raise Closed('a handshake over the limit')
    top, rest = head.split(b'\r\n\r\n', 1)
    lines = top.decode('latin-1').split('\r\n')
    code = lines[0].split(' ')[1] if len(lines[0].split(' ')) > 1 else ''
    if code != '101':
        s.close()
        raise Closed('the site answered %s' % (code or 'nothing'))
    hdr = {k.strip().lower(): v.strip() for k, v in (ln.split(':', 1) for ln in lines[1:] if ':' in ln)}
    if hdr.get('sec-websocket-accept') != ws_accept(key):
        s.close()
        raise Closed('the handshake did not check out')
    w = WS(s)
    w.buf = rest
    return w


# ---- the thread

def backoff(n, rnd=random.random):
    """Seconds to wait before reconnect try n (0 first), with up to a fifth of jitter. Pure but for rnd."""
    b = BACKOFF[min(n, len(BACKOFF) - 1)]
    return b + b * 0.2 * rnd()


def serve(w, speakers, cfg, tok, http, log, clock=time.monotonic, state_every=STATE_S, scenes=(), presence=(), family=(), lit_every=LIT_S):
    """One connected socket: hello and the full state set, then commands and pings until it drops. Raises when it does. The
    state frames after the first go from their own thread (watch), which stops with the socket."""
    allowed = {e for e, _ in speakers}
    w.send_text(json.dumps(hello(speakers, VERSION, scenes, books_set(cfg), family), separators=(',', ':')))
    watcher, stop, kick, lkick = Watcher(speakers, tok, http), threading.Event(), threading.Event(), threading.Event()
    th = threading.Thread(target=watch, args=(w, watcher, stop, kick, log, state_every), name='house-link-state', daemon=True)
    th.start()
    if any(not off for _, _, off in scenes):   # 1.0.42: what each guest scene's lights are doing, on its own thread
        threading.Thread(target=watch_lit, args=(w, scenes, tok, http, stop, lkick, log, lit_every), name='house-link-lit', daemon=True).start()
    try:
        _serve(w, speakers, cfg, tok, http, log, clock, allowed, kick, scenes, presence, family, lkick)
    finally:
        stop.set()
        kick.set()
        lkick.set()


def _serve(w, speakers, cfg, tok, http, log, clock, allowed, kick, scenes=(), presence=(), family=(), lkick=None):
    w.sock.settimeout(PING_S)
    heard, pinged = clock(), clock()
    queuing = {'t': None}   # the thread adding a Play's other tracks; a command while it runs is answered busy
    searching = {'t': None}   # 1.0.41: a book search runs on its own thread, one at a time, so a Play never waits behind Bookshelf
    while True:
        if clock() - pinged >= PING_S:
            w.send_text('ping')
            pinged = clock()
        try:
            m = w.recv()
        except socket.timeout:
            if clock() - heard > DEAD_S:
                raise Closed('nothing heard for %d s' % DEAD_S)
            continue
        heard = clock()
        if m is None or m == 'pong':
            continue
        try:
            j = json.loads(m)
        except (ValueError, RecursionError):   # junk, or nesting deep enough to stop the parser: dropped
            continue
        if isinstance(j, dict) and j.get('t') == 'books':
            cid, term = check_books(j)
            if not cid:
                continue
            if term is None or not books_set(cfg):
                w.send_text(json.dumps({'t': 'books', 'id': cid, 'ok': False, 'why': 'bad_command' if term is None else 'no_bookshelf'}, separators=(',', ':')))
                continue
            if searching['t'] is not None and searching['t'].is_alive():
                w.send_text(json.dumps({'t': 'books', 'id': cid, 'ok': False, 'why': 'busy'}, separators=(',', ':')))
                continue

            def search(cid=cid, term=term):
                res = run_books(cid, term, cfg, http)
                log('house link: book search: %s' % ('%d found' % len(res['books']) if res['ok'] else res['why']))
                try:
                    w.send_text(json.dumps(res, separators=(',', ':')))
                except Exception:   # the socket dropped meanwhile: the site has already said so
                    pass
            searching['t'] = threading.Thread(target=search, name='house-link-books', daemon=True)
            searching['t'].start()
            continue
        if not isinstance(j, dict) or j.get('t') != 'cmd':
            continue
        cmd, why = check_command(j, allowed, len(scenes), len(family))
        names = dict(speakers)
        if why:
            cid = j.get('id') if isinstance(j.get('id'), str) and ID_RE.fullmatch(j.get('id')) else None
            log('house link: refused a command (%s)' % why)
            if cid:
                w.send_text(json.dumps({'t': 'result', 'id': cid, 'ok': False, 'state': 'unknown', 'why': why}))
            continue
        if cmd['kind'] == 'scene':   # a guest light scene: its own call, never queued behind a Play
            res = run_scene(cmd, scenes, presence, tok, http)
            log('house link: scene %s: %s' % (scenes[cmd['scene'] - 1][1], 'on' if res['ok'] else res.get('why', 'failed')))
            w.send_text(json.dumps(res, separators=(',', ':')))
            if lkick is not None:
                lkick.set()
            continue
        if cmd['kind'] == 'family':   # 1.0.42: a family look, script.turn_on on its listed script
            res = run_family(cmd, family, tok, http)
            log('house link: family look %s: %s' % (family[cmd['scene'] - 1][1], 'ran' if res['ok'] else res.get('why', 'failed')))
            w.send_text(json.dumps(res, separators=(',', ':')))
            if lkick is not None:
                lkick.set()
            continue
        name = names.get(cmd['speaker'], cmd['speaker'])
        if queuing['t'] is not None and queuing['t'].is_alive():
            log('house link: %s on %s: busy (still queuing the last Play)' % (cmd['kind'], name))
            w.send_text(json.dumps({'t': 'result', 'id': cmd['id'], 'ok': False, 'state': 'unknown', 'why': 'busy'}, separators=(',', ':')))
            continue
        later = []
        res = run_command(cmd, cfg, tok, http, later=later)
        log('house link: %s on %s: %s' % (cmd['kind'], name, ('ok, ' + res['state']) if res['ok'] else res.get('why', 'failed')))
        w.send_text(json.dumps(res, separators=(',', ':')))
        kick.set()   # read the speakers again now, so the state frame follows the command
        if later:   # the answer is already sent; the rest of the tracks go on behind it
            queuing['t'] = threading.Thread(target=enqueue_rest, args=(later, tok, http, log, name), name='house-link-queue', daemon=True)
            queuing['t'].start()


def loop(cfg, tok, http, log, connector=connect, sleep=time.sleep, clock=time.monotonic, rounds=None):
    """Connect, serve, and reconnect with backoff, for ever (rounds: a count for the test). Never raises."""
    path = socket_url(cfg.get('push_url'))
    try:
        speakers = speakers_from(cfg)
        scenes = scenes_from(cfg)
        family = family_from(cfg)
    except ValueError as e:
        log('house link: %s; it stays off' % e)
        return
    presence = presence_from(cfg)
    if not path or not (speakers or scenes or family or books_set(cfg)):
        return
    n, said = 0, None
    while rounds is None or rounds > 0:
        if rounds is not None:
            rounds -= 1
        t0 = clock()
        w = None
        try:
            w = connector(path)
            log('house link: connected, %d speaker%s, %d scene%s' % (len(speakers), '' if len(speakers) == 1 else 's', len(scenes), '' if len(scenes) == 1 else 's'))
            said = None
            serve(w, speakers, cfg, tok, http, log, clock, scenes=scenes, presence=presence, family=family)
        except Exception as e:   # a drop, a refusal, no network: say it once per kind, then wait and try again
            why = str(e)[:160] or type(e).__name__
            if why != said:
                log('house link: %s; trying again with backoff' % why)
                said = why
        finally:
            if w is not None:
                w.close()
        n = 0 if clock() - t0 >= STEADY_S else n + 1
        sleep(backoff(n))


def start(cfg, tok, http, log, version):
    """Start the house link on its own daemon thread when sonos_speakers names a speaker or guest_scenes a scene. Returns the thread or None."""
    global VERSION
    VERSION = version
    if not (cfg.get('sonos_speakers') or []) and not (cfg.get('guest_scenes') or []) and not (cfg.get('family_scenes') or []) and not books_set(cfg):
        return None
    t = threading.Thread(target=loop, args=(cfg, tok, http, log), name='house-link', daemon=True)
    t.start()
    return t
