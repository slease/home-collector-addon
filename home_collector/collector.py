"""
Home collector add-on (roadmap R-0003; src/home-collector.js is the site side). Python standard library only.

Reads Home Assistant only, through the Supervisor's proxy to Core's REST API (http://supervisor/core/api/states with the
SUPERVISOR_TOKEN the Supervisor gives an add-on that sets homeassistant_api: true), keeps the entities its options list, turns each
into one of the site's fixed kinds and posts them to theleases.org: the changed ones when something changes, and all of them every
HEARTBEAT_S as a heartbeat. Nothing goes anywhere else: the push address must be https://theleases.org/collector/<token>/push.
Entities not in the options never leave this machine, and neither does any attribute but the friendly name and the unit.
"""
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.request

OPTIONS = '/data/options.json'
CORE = 'http://supervisor/core/api'
HEARTBEAT_S = 300
PUSH_RE = re.compile(r'^https://theleases\.org/collector/[A-Za-z0-9_-]{20,64}/push$')
KINDS = ('door', 'sensor', 'presence', 'battery', 'device_online', 'media', 'nas', 'ups', 'router_client',
         'light', 'printer', 'lock', 'camera', 'alarm', 'garage', 'appliance')
NUMERIC = ('sensor', 'battery', 'router_client', 'printer')   # printer: a toner or drum level, percent
MAX_ITEMS = 200


VERSION = '1.0.32'   # sent on every push as `addon` (1.0.29, bug b-4c9bf392); the site's ADDON_VERSION (src/home-collector.js) must match


def log(msg):
    print('[home-collector] ' + msg, flush=True)


def _nth_sunday(y, m, n):
    """The day of the month of the n-th Sunday of month m (1.0.20). Pure."""
    first = datetime(y, m, 1).weekday()          # Monday 0 ... Sunday 6
    return 1 + (6 - first) % 7 + 7 * (n - 1)


def central(ts):
    """Unix seconds as Central (US) wall-clock time, a naive datetime: CDT from 2 AM on the second Sunday of March to 2 AM on the
    first Sunday of November, CST otherwise. Worked out here (1.0.20) because the add-on's base image may have no time zone data. Pure."""
    u = datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
    y = u.year
    start = datetime(y, 3, _nth_sunday(y, 3, 2), 8)    # 2 AM CST is 08:00 UTC
    end = datetime(y, 11, _nth_sunday(y, 11, 1), 7)    # 2 AM CDT is 07:00 UTC
    return u + timedelta(hours=-5 if start <= u < end else -6)


def stamp(ts=None):
    """The Central time a log line starts with, like '11:42:05 PM' (11/5/26: "the collector log should have timestamps
    also"; R-0451 1.3). Pure when ts is given."""
    t = central(time.time() if ts is None else ts)
    return '%d:%02d:%02d %s' % ((t.hour % 12) or 12, t.minute, t.second, 'AM' if t.hour < 12 else 'PM')


class Stamped:
    """stdout with each line started by stamp(): every job prints its own lines, so the stamp goes on at the one place they all
    reach. Installed by the entry point only, never on import, so a test's captured output stays as the job wrote it."""
    def __init__(self, raw, clock=None):
        self.raw, self.clock, self.buf = raw, clock or time.time, ''

    def write(self, s):
        self.buf += str(s)
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            self.raw.write(stamp(self.clock()) + ' ' + line + '\n')
        return len(s)

    def flush(self):
        if self.buf:
            self.raw.write(stamp(self.clock()) + ' ' + self.buf + '\n')
            self.buf = ''
        self.raw.flush()

    def __getattr__(self, k):
        return getattr(self.raw, k)


def token():
    t = os.environ.get('SUPERVISOR_TOKEN')
    if t:
        return t
    try:
        with open('/run/s6/container_environment/SUPERVISOR_TOKEN') as f:
            return f.read().strip()
    except OSError:
        return None


def load_options(path=None):
    """The add-on's options, checked. Returns (push_url, poll_seconds, {entity_id: kind}) or raises ValueError."""
    with open(path or OPTIONS) as f:
        o = json.load(f)
    url = str(o.get('push_url') or '').strip()
    if not PUSH_RE.match(url):
        raise ValueError('push_url must be the link from Site Status: https://theleases.org/collector/<token>/push')
    poll = int(o.get('poll_seconds') or 15)
    poll = max(5, min(300, poll))
    allow = {}
    for e in o.get('entities') or []:
        eid, kind = str(e.get('entity') or '').strip().lower(), e.get('kind')
        if not re.match(r'^[a-z_]+\.[a-z0-9_]+$', eid) or kind not in KINDS:
            raise ValueError('entities: "%s" needs an entity id and one of the kinds' % eid[:60])
        allow[eid] = kind
    if len(allow) > MAX_ITEMS:
        raise ValueError('At most %d entities.' % MAX_ITEMS)
    return url, poll, allow


def number(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float('inf'), float('-inf')):
        return None
    return int(v) if v == int(v) and abs(v) < 1e9 else round(v, 3)


def unix_minutes(s):
    """A Home Assistant timestamp state (ISO 8601) as whole Unix minutes, or None. Minutes, so the value only changes when the time
    does (a backup finishing), never on each poll."""
    try:
        d = datetime.fromisoformat(str(s).strip().replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() // 60)


def to_state(kind, state, attrs):
    """Home Assistant's state (a string) as the site's (state, value). Anything it cannot read is ('unknown', None)."""
    s = str(state or '').lower()
    if s in ('unavailable', 'unknown', 'none', ''):
        return 'unknown', None
    if kind == 'door':
        return {'on': 'open', 'off': 'closed', 'open': 'open', 'closed': 'closed'}.get(s, 'unknown'), None
    if kind == 'light':   # on or off, and while on its brightness as a whole percent (10/30/26: dim levels on the floor plan); color never leaves
        st = {'on': 'on', 'off': 'off'}.get(s, 'unknown')
        b = attrs.get('brightness') if isinstance(attrs, dict) else None
        if st == 'on' and isinstance(b, (int, float)) and not isinstance(b, bool) and b == b and 0 <= b <= 255:
            return st, int(round(b / 2.55))
        return st, None
    if kind == 'presence':
        return ('home', None) if s in ('home', 'on') else ('away', None)   # person.*: home or a zone; binary_sensor.*_present: on or off
    if kind == 'device_online':
        return {'on': 'online', 'off': 'offline', 'home': 'online', 'not_home': 'offline', 'connected': 'online',
                'disconnected': 'offline', 'online': 'online', 'offline': 'offline'}.get(s, 'unknown'), None
    if kind == 'media':
        return {'playing': 'playing', 'paused': 'paused', 'idle': 'idle', 'standby': 'idle', 'buffering': 'playing',
                'on': 'idle', 'off': 'off'}.get(s, 'unknown'), None
    if kind == 'nas':
        if attrs.get('device_class') == 'timestamp':   # a last good backup time (R-0017): Unix minutes, unit unix_min
            return None, unix_minutes(state)
        v = number(s)
        if v is not None:
            return None, v
        # 'attention': Synology DSM's word for a volume or drive that needs a look (R-0017 2.3, add-on 1.0.8); it read as unknown before
        return {'ok': 'ok', 'normal': 'ok', 'good': 'ok', 'on': 'ok', 'warning': 'warn', 'degraded': 'warn', 'attention': 'warn',
                'crashed': 'problem', 'failed': 'problem', 'failing': 'problem', 'abnormal': 'problem',
                'off': 'problem'}.get(s, 'unknown'), None
    if kind == 'ups':
        v = number(s)
        if v is not None:
            return None, v
        flags = s.replace(',', ' ').split()
        if 'lb' in flags or 'low battery' in s:
            return 'low_battery', None
        if 'ob' in flags or 'on battery' in s:
            return 'on_battery', None
        if 'ol' in flags or s in ('online', 'on line'):
            return 'online', None
        return 'unknown', None
    if kind in NUMERIC:
        v = number(s)
        if v is None:
            return 'unknown', None
        if kind == 'battery':
            v = max(0, min(100, v))
        if kind == 'router_client':
            v = int(max(0, v))
        return None, v
    if kind == 'lock':
        return {'locked': 'locked', 'unlocked': 'unlocked', 'jammed': 'jammed', 'open': 'unlocked'}.get(s, 'unknown'), None
    if kind == 'camera':
        return ('online', None)
    if kind == 'alarm':
        return {'disarmed': 'disarmed', 'armed_home': 'armed_home', 'armed_away': 'armed_away', 'armed_night': 'armed_night',
                'armed_vacation': 'armed_away', 'armed_custom_bypass': 'armed_home', 'pending': 'pending', 'arming': 'pending',
                'triggered': 'triggered'}.get(s, 'unknown'), None
    if kind == 'garage':
        return {'open': 'open', 'closed': 'closed', 'opening': 'opening', 'closing': 'closing'}.get(s, 'unknown'), None
    if kind == 'appliance':
        return appliance_state(s), None
    return 'unknown', None


# ---- Appliances (add-on 1.0.7; 10/30/26: show appliance status and energy where Home Assistant knows it).
# LG ThinQ's current status values (10/30/26: the appliances are LG) are in here: power_off, initial, running, pause,
# end, detecting, rinsing, spinning, drying, cooling, washing, preheating, cooking, dispensing.
APPLIANCE_STATES = {
    'running': ('run', 'running', 'on', 'wash', 'washing', 'rinse', 'rinsing', 'spin', 'spinning', 'dry', 'drying', 'heat', 'heating',
                'cleaning', 'clean', 'cooking', 'preheat', 'preheating', 'returning', 'cooling', 'detecting', 'dispensing'),
    'idle': ('idle', 'standby', 'ready', 'docked', 'initial', 'sleep', 'eco', 'electric', 'heat_pump', 'high_demand', 'performance', 'gas'),
    'off': ('off', 'power_off'),
    'paused': ('pause', 'paused'),
    'done': ('end', 'finished', 'complete', 'completed', 'done'),
}
STATUS_IDS = ('current_status', 'run_state', 'operation')   # the run-state sensor, preferred over the device's other sensors, in this order
APPLIANCE_WORDS = ('washer', 'dryer', 'dishwasher', 'oven', 'range', 'microwave', 'refrigerator', 'fridge', 'freezer')
# Parts of an appliance that are not its running state: a door, a light, a filter, a fault, a timer, a reading.
APPLIANCE_SKIP = ('door', 'lock', 'light', 'filter', 'error', 'fault', 'problem', 'child', 'remote', 'wifi', 'connect', 'connectivity',
                  'temperature', 'temp', 'power', 'energy', 'remaining', 'time', 'left', 'battery', 'signal', 'rssi', 'update', 'detergent',
                  'rinse_aid', 'softener', 'humidity', 'water_level', 'ice', 'count', 'cycles',
                  # a radio's own readings, not the appliance (1.0.9; 10/31/26: a fridge's Node status showed on Energy)
                  'node_status', 'last_seen', 'linkquality', 'lqi', 'firmware', 'identify', 'ping')
APPLIANCE_NOT_CLASSES = ('door', 'opening', 'lock', 'problem', 'connectivity', 'power', 'energy', 'temperature', 'humidity', 'battery',
                         'timestamp', 'duration', 'current', 'voltage', 'signal_strength', 'update', 'safety', 'light')
MAX_APPLIANCES = 30


def appliance_state(s):
    """Home Assistant's state as one of running, idle, off, paused, done, unknown. Pure."""
    t = str(s or '').strip().lower().replace(' ', '_').replace('-', '_')
    for st, words in APPLIANCE_STATES.items():
        if t in words:
            return st
    return 'unknown'


def is_appliance(s):
    """An entity that reads an appliance's running state: a water heater or a vacuum, or a sensor, binary_sensor or select whose id
    (as whole words) or device class names a washer, dryer, dishwasher, oven, range, microwave, fridge or freezer, and is not one of
    its parts or readings (a door, a temperature, a timer). Conservative on purpose. Pure."""
    eid = s.get('entity_id') if isinstance(s, dict) else None
    if not isinstance(eid, str) or not re.match(r'^[a-z_]+\.[a-z0-9_]+$', eid):
        return False
    a = s.get('attributes') if isinstance(s.get('attributes'), dict) else {}
    dom, obj = eid.split('.', 1)
    if 'entity_id' in a:
        return False
    if dom in ('water_heater', 'vacuum'):
        return True
    if dom not in ('sensor', 'binary_sensor', 'select'):
        return False
    dc = str(a.get('device_class') or '').lower()
    if dc in APPLIANCE_NOT_CLASSES or a.get('unit_of_measurement') or any(w in obj for w in APPLIANCE_SKIP):
        return False
    words = obj.split('_')
    if dom == 'select' and not any(k in obj for k in STATUS_IDS):   # a select is usually a setting (express mode), not a state
        return False
    if dom == 'sensor' and any(k in obj for k in STATUS_IDS[:2]):   # an LG ThinQ "current status" or a run state, whatever the device is named
        return True
    return any(w in words for w in APPLIANCE_WORDS) or any(w in dc.split('_') for w in APPLIANCE_WORDS)


def appliance_candidates(states, allow):
    """Every entity is_appliance() finds that is not already listed, by entity id. Pure."""
    return sorted(s['entity_id'] for s in (states if isinstance(states, list) else []) if is_appliance(s) and s['entity_id'] not in allow)


def status_rank(eid):
    obj = eid.split('.', 1)[1]
    for i, k in enumerate(STATUS_IDS):
        if k in obj:
            return i
    return len(STATUS_IDS)


def one_per_device(ids, devices=None):
    """One appliance a Home Assistant device: its run-state sensor (current_status, then run_state, then operation), else the
    first by id. An entity with no device stands alone. devices: {entity id: device id}. Pure."""
    best = {}
    for eid in sorted(ids):
        dev = (devices or {}).get(eid) or eid
        if dev not in best or (status_rank(eid), eid) < (status_rank(best[dev]), best[dev]):
            best[dev] = eid
    return sorted(best.values())


def with_appliances(states, allow, on=True, devices=None):
    """The watch list plus one appliance a device (one_per_device of appliance_candidates), as kind appliance, when
    all_appliances is on, at most MAX_APPLIANCES by entity id and within MAX_ITEMS. Returns (allow, picked ids, left out). Pure."""
    out = dict(allow)
    if not on:
        return out, [], 0
    found = one_per_device(appliance_candidates(states, out), devices)
    room = max(0, min(MAX_APPLIANCES, MAX_ITEMS - len(out)))
    for eid in found[:room]:
        out[eid] = 'appliance'
    return out, found[:room], max(0, len(found) - room)


def device_ids(tok, ids):
    """{entity id: its Home Assistant device id} through Core's template API (one call), or {} when it fails."""
    ok = [i for i in ids if re.match(r'^[a-z_]+\.[a-z0-9_]+$', i)]
    if not ok:
        return {}
    body = json.dumps({'template': '{{ [' + ', '.join("device_id('%s')" % i for i in ok) + '] | tojson }}'}).encode('utf-8')
    req = urllib.request.Request(CORE + '/template', data=body, method='POST', headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            got = json.loads(r.read().decode('utf-8'))
    except Exception:
        return {}
    return {i: d for i, d in zip(ok, got if isinstance(got, list) else []) if isinstance(d, str) and d}


def sibling_template(ids):
    """One template: for each appliance, the ids on its Home Assistant device that read a time remaining, as a JSON list."""
    parts = []
    for i in ids:
        if re.match(r'^[a-z_]+\.[a-z0-9_]+$', i):
            parts.append("((device_entities(device_id('%s')) | select('search', '(remaining|time_left|time_to_end|finish_time|end_time)') | list) if device_id('%s') else [])" % (i, i))
    return '{{ [' + ', '.join(parts) + '] | tojson }}'


def get_siblings(tok, ids):
    """{appliance id: the sensor id reading its time remaining} through Core's template API (one call), or {} when it fails."""
    if not ids:
        return {}
    body = json.dumps({'template': sibling_template(ids)}).encode('utf-8')
    req = urllib.request.Request(CORE + '/template', data=body, method='POST', headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            got = json.loads(r.read().decode('utf-8'))
    except Exception:
        return {}
    out = {}
    for i, g in zip(ids, got if isinstance(got, list) else []):
        c = sorted((x for x in (g if isinstance(g, list) else []) if isinstance(x, str) and re.match(r'^sensor\.[a-z0-9_]+$', x)),
                   key=lambda x: (0 if 'remaining' in x or 'time_left' in x else 1, x))   # a time remaining before a finish time
        if c:
            out[i] = c[0]
    return out


def remaining_minutes(s, now=None):
    """Minutes left from a time-remaining sensor: a number in min, h or s, a duration as H:MM or H:MM:SS, or a timestamp (the
    finish time). Whole minutes, 0 to 1440, else None. Pure."""
    if not isinstance(s, dict):
        return None
    a = s.get('attributes') if isinstance(s.get('attributes'), dict) else {}
    st = s.get('state')
    if a.get('device_class') == 'timestamp':
        m = unix_minutes(st)
        if m is None:
            return None
        v = m - int((now if now is not None else time.time()) // 60)
    elif isinstance(st, str) and re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', st.strip()):
        p = [int(x) for x in st.strip().split(':')]
        v = p[0] * 60 + p[1] + (p[2] / 60 if len(p) == 3 else 0)
    else:
        n = number(st)
        if n is None:
            return None
        unit = str(a.get('unit_of_measurement') or 'min').strip().lower()
        mult = {'min': 1, 'mins': 1, 'minutes': 1, 'm': 1, 'h': 60, 'hr': 60, 'hours': 60, 's': 1 / 60, 'sec': 1 / 60, 'seconds': 1 / 60}.get(unit)
        if mult is None:
            return None
        v = n * mult
    v = int(round(v))
    return v if 0 <= v <= 1440 else (0 if -5 <= v < 0 else None)


AREA_EVERY_S = 3600   # each light's Home Assistant area (its room) is asked at most hourly, or when the set of lights changes


def with_lights(states, allow, on=True):
    """The allowlist plus every light.* entity Home Assistant has, as kind light, when the all_lights option is on
    (10/29/26: no hand-listed lights; roadmap R-0413 step 1.3). A light group (its attributes carry entity_id, such as a Hue room
    group) is skipped so nothing counts twice. Listed entities come first; lights fill the rest up to MAX_ITEMS by entity id.
    Returns (allow, lights cut off). Pure."""
    out = dict(allow)
    if not on:
        return out, 0
    lights = []
    for s in states if isinstance(states, list) else []:
        eid = s.get('entity_id') if isinstance(s, dict) else None
        attrs = s.get('attributes') if isinstance(s, dict) and isinstance(s.get('attributes'), dict) else {}
        if isinstance(eid, str) and eid.startswith('light.') and re.match(r'^[a-z_]+\.[a-z0-9_]+$', eid) and eid not in out and 'entity_id' not in attrs:
            lights.append(eid)
    lights.sort()
    room = max(0, MAX_ITEMS - len(out))
    for eid in lights[:room]:
        out[eid] = 'light'
    return out, max(0, len(lights) - room)


def area_template(ids):
    """One Home Assistant template giving each light's area name, in order, as a JSON list (ids are checked entity ids)."""
    return '{{ [' + ', '.join("area_name('%s')" % i for i in ids if re.match(r'^[a-z_]+\.[a-z0-9_]+$', i)) + '] | tojson }}'


def clean_area(v):
    """An area name as short plain text (40 characters), else None."""
    if not isinstance(v, str):
        return None
    t = ' '.join(re.sub(r'[\x00-\x1f\x7f<>{}]', ' ', v).split())[:40]
    return t or None


def get_areas(tok, ids):
    """{light id: area name} through Core's template API (one call), or {} when it fails."""
    if not ids:
        return {}
    body = json.dumps({'template': area_template(ids)}).encode('utf-8')
    req = urllib.request.Request(CORE + '/template', data=body, method='POST', headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            got = json.loads(r.read().decode('utf-8'))
    except Exception:   # never let the room names stop a push
        return {}
    return {i: clean_area(a) for i, a in zip(ids, got) if clean_area(a)} if isinstance(got, list) else {}


def items_from_states(states, allow, areas=None, siblings=None, now=None):
    """Home Assistant's /api/states answer filtered to the allowlist and turned into the site's items, in allowlist order.
    areas: {light or appliance id: area name}; a light or appliance carries its area (its room) as `area`, nothing else does.
    siblings: {appliance id: the sensor id reading its time remaining}; an appliance carries that as `value`, unit min."""
    by_id, every = {}, {}
    for s in states if isinstance(states, list) else []:
        if isinstance(s, dict) and s.get('entity_id') in allow:
            by_id[s['entity_id']] = s
        if isinstance(s, dict) and siblings and s.get('entity_id') in siblings.values():
            every[s['entity_id']] = s
    out = []
    for eid, kind in allow.items():
        s = by_id.get(eid)
        if s is None:
            continue
        attrs = s.get('attributes') if isinstance(s.get('attributes'), dict) else {}
        state, value = to_state(kind, s.get('state'), attrs)
        if kind == 'nas' and attrs.get('device_class') == 'timestamp' and value is None:
            state = 'unknown'
        item = {'kind': kind, 'id': eid, 'at': str(s.get('last_changed') or s.get('last_updated') or '')}
        name = attrs.get('friendly_name')
        if isinstance(name, str) and name.strip():
            item['name'] = ' '.join(name.split())[:60]
        if state is not None:
            item['state'] = state
        if value is not None:
            item['value'] = value
            unit = 'unix_min' if kind == 'nas' and attrs.get('device_class') == 'timestamp' else '%' if kind == 'light' else attrs.get('unit_of_measurement')
            if isinstance(unit, str) and 0 < len(unit) <= 12:
                item['unit'] = unit
        if kind in ('light', 'appliance') and areas and areas.get(eid):
            item['area'] = areas[eid]
        if kind == 'appliance' and siblings and siblings.get(eid) and item.get('state') in ('running', 'paused'):
            left = remaining_minutes(every.get(siblings[eid]), now)
            if left is not None:
                item['value'], item['unit'] = left, 'min'
        if not item['at']:
            continue
        out.append(item)
    return out


def payload(items, reason, watching=None, version=True):
    """The push body. watching (a heartbeat only, at most hourly): the entity ids this add-on watches, for Site Status.
    addon (1.0.29): this add-on's VERSION, on every push, so Site Status can say when the house runs an older one; version=False
    leaves it out, for a site from before 1.0.29 that refuses the field."""
    b = {'v': 1, 'reason': reason, 'items': items[:MAX_ITEMS]}
    if version:
        b['addon'] = VERSION
    if watching is not None and reason == 'heartbeat':
        b['watching'] = list(watching)[:MAX_WATCHING]
    return b


WATCH_EVERY_S = 3600   # the watch list rides a heartbeat at most hourly (1.0.11, R-0416 step 3.8)
MAX_WATCHING = 1000


def watch_list(watch, states, opts, power_mod=None):
    """Every entity id the add-on watches, sorted: the options list plus what all_lights and all_appliances picked (watch), plus
    every sensor all_power's job reads (power_job.select with power_exclude) while all_power is on. Entity ids only: never the
    push link or an option's value. Pure."""
    ids = set(e for e in watch if isinstance(e, str) and re.match(r'^[a-z_]+\.[a-z0-9_]+$', e))
    if power_mod is not None and opts.get('all_power', True) is not False and callable(getattr(power_mod, 'select', None)):
        extra = [str(w).strip() for w in (opts.get('power_exclude') or []) if isinstance(w, str) and str(w).strip()][:50]
        try:
            ids.update(power_mod.select(states, extra))
        except Exception:
            pass
    return sorted(ids)[:MAX_WATCHING]


def changed(items, last):
    """The items whose state or value differs from what was last sent (last: {id: (state, value)})."""
    return [it for it in items if last.get(it['id']) != (it.get('state'), it.get('value'))]


def get_states(tok):
    req = urllib.request.Request(CORE + '/states', headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))


def behind_line(ans, said):
    """The one Log line when the site expects a newer add-on (1.0.29): the site's push answer carries `expected` and `behind`.
    Returns (line or None, what to remember), so it is said once per expected version, never on every push. Pure."""
    exp = ans.get('expected') if isinstance(ans, dict) else None
    if not isinstance(exp, str) or not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,4}$', exp):
        return None, said
    if ans.get('behind') is not True or exp == said:
        return None, (exp if ans.get('behind') is True else None)
    return ('the site expects add-on %s and this is %s: copy the new folder over /addons/home-collector and press Update '
            '(or Rebuild) in Home Assistant' % (exp, VERSION)), exp


def push(url, body):
    if not PUSH_RE.match(url):
        raise ValueError('refusing to send anywhere but theleases.org')
    data = json.dumps(body, separators=(',', ':')).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST', headers={'Content-Type': 'application/json', 'User-Agent': 'lease-home-collector/1'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode('utf-8') or '{}')
        except ValueError:
            msg = {}
        return e.code, msg


# ---- Extra jobs (R-0378 and later): optional modules beside this file, each with run(cfg, now, http).
# A job runs only when every option it needs is set (blank options skip it), at most every `every` seconds, in the same loop.
# cfg is the add-on's options plus cfg['state'], the job's own state from STATE_DIR/<name>.json; when run returns a dict it is saved
# as the new state. A job's options (Radarr, Plex) stay on this machine: nothing here sends them, and push() only ever sends items.
# http(method, url, headers=None, body=None, timeout=20) -> (status, bytes) is plain urllib; each job decides where it may call.
STATE_DIR = '/data/jobs'
JOBS = [
    {'name': 'movies', 'module': 'movies_job', 'needs': ('radarr_url', 'radarr_key', 'plex_url', 'plex_token'), 'every': 300},   # actions and (1.0.13) TV plays every 5 minutes; movies_job.py sends the library at most hourly itself (R-0378)
    {'name': 'streams', 'module': 'streams_job', 'needs': ('plex_url', 'plex_token'), 'every': 15, 'tick': True},   # read only: what Plex is streaming, sampled every 15 s on the fast path (1.0.27, R-0484 3.6; each minute on the slow thread before), posted on any change and every minute while one plays, else every 5 (R-0407)
    {'name': 'tv', 'module': 'tv_job', 'needs': ('sonarr_url', 'sonarr_key'), 'every': 300},   # read only: Sonarr's shows, first-time imports and calendar, posted at most hourly by tv_job.py itself (R-0412)
    {'name': 'power', 'module': 'power_job', 'needs': ('push_url',), 'every': 5, 'tick': True},   # read only: every power and energy sensor, sampled each minute, posted as 5-minute buckets (R-0416), and live watts with the Powerwall gateway's readings every 5 s (1.0.14; 10 s in 1.0.12; only when changed, at least every 60 s); all_power: false turns it off
    {'name': 'motion', 'module': 'motion_job', 'needs': ('push_url',), 'every': 5, 'tick': True},   # read only: the motion sensors and each motion automation's timer (timer.*_motion_off), every 5 s when changed, at least every 60 s (1.0.19, R-0448); all_motion: false turns it off
    {'name': 'nowplaying', 'module': 'nowplaying_job', 'needs': ('push_url', 'poster_player'), 'every': 5, 'tick': True},   # read only: what the home theater's player plays, each poll, for the poster screen (R-0018 3.4)
    {'name': 'clients', 'module': 'clients_job', 'needs': ('push_url',), 'every': 900},   # read only: the router's client list (ASUSWRT device_trackers), every 15 minutes (R-0164 0.1); all_router_clients: false turns it off
    {'name': 'plays', 'module': 'plays_job', 'needs': ('push_url', 'plex_url', 'plex_token', 'plex_account_id'), 'every': 300},   # read only: the owner's Plex music plays for the Listening log, every 5 minutes (R-0018 3.5; every 15 before 1.0.13)
    {'name': 'covers', 'module': 'covers_job', 'needs': ('push_url', 'plex_url', 'plex_token', 'plex_account_id'), 'every': 300},   # read only: each played album's cover from Plex, once per album, at most 12 a run (1.0.18, R-0447; the album id from parentThumb since 1.0.20)
    {'name': 'library', 'module': 'library_job', 'needs': ('push_url', 'plex_url', 'plex_token'), 'every': 300},   # read only: the Plex music library (artists, albums, tracks), daily and when a section changes, a few pages a run (1.0.20, R-0451); music_library: false turns it off
    {'name': 'wake', 'module': 'wake_job', 'needs': ('push_url',), 'every': 15},   # read only: the wake-up routine's three helpers, posted when one changes (1.0.20, R-0453); wake_up: false turns it off
    {'name': 'queue', 'module': 'queue_job', 'needs': ('push_url',), 'every': 60},   # read only: the Sonarr and Radarr download queues, a row per title with percent done, posted when it changes (1.0.25, R-0484 3.1); skips an app whose options are blank
]
# Every job's run ends in one outcome line (1.0.20, R-0451 1.3): "job <name>: ok ..." with the counts the job put in
# cfg['outcome'], or "job <name>: error ..." when it threw or set cfg['failed'] (a Plex or site refusal it will retry), so a job that does nothing, or fails, still shows in the Log tab. A job that runs
# more often than OUTCOME_EVERY_S (the 5-second power, motion and Now showing jobs, the minute streams job, the wake job) would
# bury the log, so its line comes on its first run, whenever it turns from ok to failing or back (or its error changes), and
# otherwise as one summary every OUTCOME_EVERY_S: "job power: ok, 180 runs since 11:30:05 PM".
OUTCOME_EVERY_S = 300
OUTCOMES = {}   # name -> {'said': last line's kind and error, 'since': unix, 'runs': n, 'errors': n, 'last': text}
OUTCOME_LOCK = threading.Lock()   # the fast jobs and the slow jobs' thread both write outcome lines (1.0.26)
# The fast path (1.0.26, bug b-9b7cba8e): the jobs marked 'tick' (power, motion, Now showing, every 5 s) run on the main loop, and
# every other job runs on one worker thread (SLOW), started after a poll only when the last batch has finished, so a slow minute
# job (a Sonarr read that hangs, a Plex library walk) can never hold back the 5-second posts. Each job is run by one thread only
# and keeps its own state file; `ran` and `mods` are shared, but each job's keys are written only by the thread that runs it.
SLOW = {'thread': None}


def http(method, url, headers=None, body=None, timeout=20):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def raw_options(path=None):
    try:
        with open(path or OPTIONS) as f:
            o = json.load(f)
        return o if isinstance(o, dict) else {}
    except (OSError, ValueError):
        return {}


def due_jobs(opts, ran, now, jobs=None):
    """The jobs whose options are all set and whose interval has passed. ran: {name: last run, monotonic seconds}. Pure."""
    out = []
    for j in jobs if jobs is not None else JOBS:
        if any(not str(opts.get(k) or '').strip() for k in j['needs']):
            continue
        if j['name'] in ran and now - ran[j['name']] < j.get('every', 3600):
            continue
        out.append(j)
    return out


def load_job(j):
    """The job's module from beside this file, or None when it is not installed."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), j['module'] + '.py')
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(j['module'], path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m if callable(getattr(m, 'run', None)) else None


def outcome(j, ok, text, now=None):
    """The job's outcome line (see OUTCOMES above), printed or counted toward the next summary. Returns the line printed, or None."""
    with OUTCOME_LOCK:
        return _outcome(j, ok, text, now)


def _outcome(j, ok, text, now=None):
    now = time.time() if now is None else now
    name, every = j['name'], j.get('every', 3600)
    line = 'job %s: %s' % (name, ('ok' + (', ' + text if text else '')) if ok else ('error: ' + text))
    if every >= OUTCOME_EVERY_S:
        log(line)
        return line
    o = OUTCOMES.setdefault(name, {'said': None, 'since': now, 'runs': 0, 'errors': 0})
    kind = 'ok' if ok else 'error: ' + text
    o['runs'] += 1
    o['errors'] += 0 if ok else 1
    o['last'] = text
    if kind != o['said']:
        o.update(said=kind, since=now, runs=0, errors=0)
        log(line)
        return line
    if now - o['since'] >= OUTCOME_EVERY_S:
        n, e = o['runs'], o['errors']
        msg = 'job %s: %s, %d runs since %s%s' % (name, 'ok' if not e else '%d of %d runs failed' % (e, n), n, stamp(o['since']),
                                                   (' (last: %s)' % o['last'][:120]) if o.get('last') else '')
        o.update(since=now, runs=0, errors=0)
        log(msg)
        return msg
    return None


def run_jobs(opts, ran, mods, now_mono, state_dir=None, only_tick=False, only_slow=False):
    """Run each due job once; a failing job is logged and tried again at its next interval. only_tick: only the jobs marked
    'tick' (the power job's live watts, 1.0.12), run between polls so they keep their own pace whatever poll_seconds is.
    only_slow: only the others (1.0.26: the worker thread's batch, see SLOW).
    Never raises (1.0.20): a job that cannot load, has unreadable state, throws, or cannot save its state says so in its outcome
    line instead of stopping the add-on or going quiet."""
    d = state_dir or STATE_DIR
    pick = [x for x in JOBS if x.get('tick')] if only_tick else [x for x in JOBS if not x.get('tick')] if only_slow else None
    for j in due_jobs(opts, ran, now_mono, pick):
        fresh = j['name'] not in ran   # the job's first run since the add-on started (1.0.26: a held retry wait is dropped then)
        ran[j['name']] = now_mono
        if j['name'] not in mods:
            try:
                mods[j['name']] = load_job(j)
            except Exception as e:   # a module that does not import (1.0.20: this stopped the whole add-on before)
                mods[j['name']] = None
                outcome(j, False, 'could not load %s.py: %s: %s' % (j['module'], type(e).__name__, str(e)[:160]))
                continue
            if mods[j['name']] is None:
                outcome(j, False, '%s.py is not installed; copy the whole add-on folder again' % j['module'])
        m = mods[j['name']]
        if m is None:
            continue
        sp = os.path.join(d, j['name'] + '.json')
        note = ''
        try:
            with open(sp) as f:
                st = json.load(f)
            if not isinstance(st, dict):
                st, note = {}, 'its saved state was not readable, so it started fresh'
        except OSError:
            st = {}
        except ValueError:
            st, note = {}, 'its saved state was not readable, so it started fresh'
        cfg = dict(opts, state=st, fresh=fresh)
        try:
            new = m.run(cfg, time.time(), http)
            if isinstance(new, dict):
                os.makedirs(d, exist_ok=True)
                with open(sp + '.tmp', 'w') as f:
                    json.dump(new, f)
                os.replace(sp + '.tmp', sp)
            said = str(cfg.get('outcome') or '').strip()[:300]
            outcome(j, not cfg.get('failed'), '; '.join(x for x in (said, note) if x) or ('' if not cfg.get('failed') else 'it did not finish'))
        except Exception as e:
            outcome(j, False, '%s: %s' % (type(e).__name__, str(e)[:200]))


def main():
    try:
        url, poll, allow = load_options()
    except (OSError, ValueError) as e:
        log('options: %s' % e)
        return 1
    tok = token()
    if not tok:
        log('no SUPERVISOR_TOKEN: homeassistant_api must be true and run.sh must use with-contenv')
        return 1
    log('version %s: watching %d entities, polling every %ds, heartbeat every %ds' % (VERSION, len(allow), poll, HEARTBEAT_S))
    last, beat = {}, None  # None: the first push is a heartbeat, however long the machine has been up
    opts, ran, mods = raw_options(), {}, {}
    all_lights = opts.get('all_lights', True) is not False
    all_appliances = opts.get('all_appliances', True) is not False
    areas, areas_at, areas_for, cut_said = {}, None, (), None
    sibs, picked_said, acut_said = {}, None, None
    adev, adev_at, adev_for = {}, None, ()
    watch_at = None   # when the watch list last went (monotonic); None sends it with the first heartbeat
    send_version, behind_said = True, None   # 1.0.29: the version rides every push unless an older site refused it
    power_mod = load_job({'module': 'power_job'})
    start_house_link(opts, tok)
    while True:
        try:
            states = get_states(tok)
            watch, cut = with_lights(states, allow, all_lights)
            if cut and cut != cut_said:
                log('all_lights: %d lights left out, over the %d-item limit (the first by entity id are sent)' % (cut, MAX_ITEMS))
            cut_said = cut
            cands = tuple(appliance_candidates(states, watch)) if all_appliances else ()
            if cands != adev_for or adev_at is None or time.monotonic() - adev_at >= AREA_EVERY_S:
                adev, adev_at, adev_for = device_ids(tok, list(cands)), time.monotonic(), cands
            watch, picked, acut = with_appliances(states, watch, all_appliances, adev)
            if tuple(picked) != picked_said:
                log('all_appliances: %d picked%s' % (len(picked), (': ' + ', '.join(picked))[:600] if picked else ''))
            if acut and acut != acut_said:
                log('all_appliances: %d left out, over the %d-appliance limit' % (acut, MAX_APPLIANCES))
            picked_said, acut_said = tuple(picked), acut
            lights = tuple(sorted(e for e, k in watch.items() if k in ('light', 'appliance')))
            if lights != areas_for or areas_at is None or time.monotonic() - areas_at >= AREA_EVERY_S:
                areas, areas_at, areas_for = get_areas(tok, list(lights)), time.monotonic(), lights
                sibs = get_siblings(tok, [e for e in lights if watch.get(e) == 'appliance'])
            items = items_from_states(states, watch, areas, sibs, time.time())
            due = beat is None or time.monotonic() - beat >= HEARTBEAT_S
            send = items if due else changed(items, last)
            wl = watch_list(watch, states, opts, power_mod) if due and (watch_at is None or time.monotonic() - watch_at >= WATCH_EVERY_S) else None
            if send or due:
                code, ans = push(url, payload(send, 'heartbeat' if due else 'change', wl, send_version))
                if send_version and code == 400 and 'addon' in str(ans.get('message', '')):   # a site before 1.0.29: send without the version
                    log('the site does not take the add-on version yet; pushes go without it until the add-on restarts')
                    send_version = False
                    code, ans = push(url, payload(send, 'heartbeat' if due else 'change', wl, False))
                if wl is not None and code == 400:   # a site before 1.0.11 refuses the list: send the heartbeat without it, try the list in an hour
                    log('the site refused the watch list (%s); sent the heartbeat without it' % str(ans.get('message', ''))[:120])
                    watch_at, wl = time.monotonic(), None
                    code, ans = push(url, payload(send, 'heartbeat', None, send_version))
                if code == 200:
                    line, behind_said = behind_line(ans, behind_said)
                    if line:
                        log(line)
                    for it in send:
                        last[it['id']] = (it.get('state'), it.get('value'))
                    if due:
                        beat = time.monotonic()
                    if wl is not None:
                        watch_at = time.monotonic()
                        log('watch list sent: %d entity ids' % len(wl))
                elif code == 404:
                    log('the site did not know the link (404); make a new one on Site Status and paste it in the options')
                else:
                    log('push refused (%s): %s' % (code, str(ans.get('message', ''))[:200]))
        except Exception as e:  # keep running; the next poll tries again
            log('error: %s' % str(e)[:200])
        run_jobs(opts, ran, mods, time.monotonic(), only_tick=True)
        start_slow(opts, ran, mods)
        nap(poll, opts, ran, mods)


def start_house_link(opts, tok):
    """The house link (1.0.32, R-0517): one outbound WebSocket for the owner's Play on Sonos, on its own thread (house_link.py).
    Only when sonos_speakers names a speaker. Never raises: a missing or broken module is one Log line and the pushes go on."""
    if not (opts.get('sonos_speakers') or []):
        return None
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'house_link.py')
        spec = importlib.util.spec_from_file_location('house_link', path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.start(opts, tok, http, log, VERSION)
    except Exception as e:
        log('house link: could not start: %s: %s' % (type(e).__name__, str(e)[:160]))
        return None


def start_slow(opts, ran, mods, runner=None):
    """Start the slow jobs' batch on the worker thread, unless the last batch is still running (1.0.26). Returns the thread
    started, or None. runner is for the test."""
    t = SLOW.get('thread')
    if t is not None and t.is_alive():
        return None
    t = threading.Thread(target=runner or run_jobs, args=(opts, ran, mods, time.monotonic()), kwargs={'only_slow': True}, name='slow-jobs', daemon=True)
    SLOW['thread'] = t
    t.start()
    return t


def next_nap(poll_end, ran, now_mono, jobs=None):
    """Seconds to sleep before the next wake: the poll's end, or a 'tick' job's next turn when that comes first. Pure."""
    t = poll_end
    for j in jobs if jobs is not None else JOBS:
        if j.get('tick') and j['name'] in ran:
            t = min(t, ran[j['name']] + j.get('every', 3600))
    return max(0.5, t - now_mono)


def nap(poll, opts, ran, mods):
    """Sleep out the poll, waking for the 'tick' jobs (the power job's 5-second live watts, 1.0.14; 10 s in 1.0.12) on their own pace."""
    end = time.monotonic() + poll
    while True:
        now = time.monotonic()
        if now >= end:
            return
        time.sleep(min(next_nap(end, ran, now), end - now))
        if time.monotonic() < end:
            run_jobs(opts, ran, mods, time.monotonic(), only_tick=True)


if __name__ == '__main__':
    sys.stdout = Stamped(sys.stdout)   # every line starts with the Central time (1.0.20)
    sys.exit(main())
