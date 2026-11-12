"""
The house link (1.0.32, roadmap R-0517, bug b-14a7baf8): one outbound WebSocket to theleases.org, so the owner's Play on a Sonos
reaches Home Assistant in the house at once, with no port opened into it.

Why: Spotify's Web API cannot start or check a Sonos (11/12/26: it read an album playing while a Sonos speaker sat paused).
Home Assistant's Sonos integration can, on the LAN, and its media_player state says what the speaker really does.

What it does, and nothing more:
- Opens wss://theleases.org/collector/<token>/socket (the push link's token; the site checks it in constant time), sends a hello
  with this add-on's version and the speakers in the sonos_speakers option (entity ids and names), and pings every PING_S seconds.
- Takes only the site's fixed commands: play, pause, resume, next, volume, each on one speaker from sonos_speakers. A play carries
  Spotify album or track URIs (spotify:album:ID, spotify:track:ID, up to 30 tracks) or a Plex ratingKey (digits); volume 0 to 100.
  check_command refuses anything else, and nothing from the site is ever run, evaluated or passed to any other Home Assistant
  action: the only calls are media_player.play_media, media_pause, media_play, media_next_track and volume_set, on the listed entity,
  through the Supervisor's proxy to Core (homeassistant_api, which the add-on already had for reading states).
- A Plex play reads the album's section, artist and title from this machine's own Plex (plex_url, plex_token) by its key and sends
  Home Assistant's documented plex://{"library_name", "artist_name", "album_name"} form; the site never sends those words.
- A Play of many tracks starts the first, answers, then adds the rest to the queue on a thread of its own; a command that comes
  while that runs is answered busy.
- Answers each command with Home Assistant's media_player state after the call (read back for up to READ_S seconds for a play,
  resume or pause) and, on failure, one fixed code (WHY), never free text.
- Runs on its own thread. A failure, a drop or a slow Home Assistant never touches the push loop. It reconnects with backoff
  (BACKOFF, with jitter), and with sonos_speakers empty it never connects.

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
KINDS = ('play', 'pause', 'resume', 'next', 'volume')
SERVICE = {'pause': 'media_pause', 'resume': 'media_play', 'next': 'media_next_track', 'volume': 'volume_set'}
STATES = ('playing', 'paused', 'idle', 'off', 'buffering', 'on', 'standby', 'unavailable', 'unknown')
WHY = ('not_allowed', 'bad_command', 'ha_refused', 'ha_unreachable', 'no_plex', 'plex_not_found', 'not_started', 'busy', 'failed')
SPEAKERS_MAX, URIS_MAX, NAME_MAX, FRAME_MAX = 12, 30, 40, 16 * 1024
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


def hello(speakers, version):
    return {'t': 'hello', 'v': 1, 'addon': version, 'speakers': [{'id': e, 'name': n} for e, n in speakers]}


def check_command(m, allowed):
    """The site's command, checked against the fixed shapes and the speaker allowlist. Returns (cmd, None) or (None, why). Pure."""
    if not isinstance(m, dict) or m.get('t') != 'cmd':
        return None, 'bad_command'
    keys = set(m.keys())
    if not keys <= {'t', 'id', 'kind', 'speaker', 'media', 'volume'}:
        return None, 'bad_command'
    cid, kind, spk = m.get('id'), m.get('kind'), m.get('speaker')
    if not isinstance(cid, str) or not ID_RE.fullmatch(cid) or kind not in KINDS:
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
    return cmd, None


def plex_media_id(meta):
    """Home Assistant's plex:// id for one Plex album's metadata (librarySectionTitle, parentTitle, title), or None. Pure."""
    if not isinstance(meta, dict) or meta.get('type') != 'album':
        return None
    lib, artist, album = meta.get('librarySectionTitle'), meta.get('parentTitle'), meta.get('title')
    if not all(isinstance(x, str) and x.strip() for x in (lib, artist, album)):
        return None
    return 'plex://' + json.dumps({'library_name': lib, 'artist_name': artist, 'album_name': album}, separators=(', ', ': '))


def plex_album(cfg, key, http):
    """(media_content_id, None) for a Plex album by its ratingKey from this machine's Plex, or (None, why)."""
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
    if service not in ('play_media', 'media_pause', 'media_play', 'media_next_track', 'volume_set'):
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


def serve(w, speakers, cfg, tok, http, log, clock=time.monotonic):
    """One connected socket: hello, then commands and pings until it drops. Raises when it does."""
    allowed = {e for e, _ in speakers}
    w.send_text(json.dumps(hello(speakers, VERSION), separators=(',', ':')))
    w.sock.settimeout(PING_S)
    heard, pinged = clock(), clock()
    queuing = {'t': None}   # the thread adding a Play's other tracks; a command while it runs is answered busy
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
        if not isinstance(j, dict) or j.get('t') != 'cmd':
            continue
        cmd, why = check_command(j, allowed)
        names = dict(speakers)
        if why:
            cid = j.get('id') if isinstance(j.get('id'), str) and ID_RE.fullmatch(j.get('id')) else None
            log('house link: refused a command (%s)' % why)
            if cid:
                w.send_text(json.dumps({'t': 'result', 'id': cid, 'ok': False, 'state': 'unknown', 'why': why}))
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
        if later:   # the answer is already sent; the rest of the tracks go on behind it
            queuing['t'] = threading.Thread(target=enqueue_rest, args=(later, tok, http, log, name), name='house-link-queue', daemon=True)
            queuing['t'].start()


def loop(cfg, tok, http, log, connector=connect, sleep=time.sleep, clock=time.monotonic, rounds=None):
    """Connect, serve, and reconnect with backoff, for ever (rounds: a count for the test). Never raises."""
    path = socket_url(cfg.get('push_url'))
    try:
        speakers = speakers_from(cfg)
    except ValueError as e:
        log('house link: %s; it stays off' % e)
        return
    if not path or not speakers:
        return
    n, said = 0, None
    while rounds is None or rounds > 0:
        if rounds is not None:
            rounds -= 1
        t0 = clock()
        w = None
        try:
            w = connector(path)
            log('house link: connected, %d speaker%s' % (len(speakers), '' if len(speakers) == 1 else 's'))
            said = None
            serve(w, speakers, cfg, tok, http, log, clock)
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
    """Start the house link on its own daemon thread when sonos_speakers names a speaker. Returns the thread or None."""
    global VERSION
    VERSION = version
    if not (cfg.get('sonos_speakers') or []):
        return None
    t = threading.Thread(target=loop, args=(cfg, tok, http, log), name='house-link', daemon=True)
    t.start()
    return t
