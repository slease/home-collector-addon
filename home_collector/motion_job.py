#!/usr/bin/env python3
"""The Home collector's motion job (add-on 1.0.19, roadmap R-0448 "Motion on Energy", 11/4/26). collector.py's JOBS
entry 'motion' calls run() every 5 seconds, between polls like the power job's live watts.

Read only. Each call reads Home Assistant's states (the same Supervisor proxy collector.py uses) and finds by itself:
  motion sensors  every binary_sensor whose device_class is motion or occupancy. A battery sensor (device_class battery, or
                  "battery" in its entity id, such as a Third Reality sensor's _battery_low) is never one.
  motion timers   every timer.<name>_motion_off: the timer helper a motion automation starts when its sensor clears, cancels
                  when it detects again, and turns the lights off on when it finishes. Its duration is the delay the owner edits
                  in Home Assistant, so it is read on every call and never typed on the site. A hand-made-off timer
                  (timer.kitchen_lights_manual_off) does not match and is left out.
It posts the latest of each to theleases.org (src/motion.js):
  POST <site>/motion  { v: 1, at, sensors: [{ id, name, on, changed }], timers: [{ id, name, state, duration_s, finishes_at, changed }] }
    at           Unix seconds of the post
    id           the entity id (binary_sensor.kitchen_motion_occupancy, timer.kitchen_motion_off), so the site can pair a timer
                 with its room; the site shows names only
    name         the friendly name, short plain text
    on           true while the sensor detects, false while clear, null while unknown or unavailable
    changed      Home Assistant's last_changed, ISO in UTC (when a clear sensor cleared: its last motion)
    state        active, idle or paused (anything else: left out)
    duration_s   the timer's duration attribute ("0:45:00", "1 day, 2:00:00") in whole seconds, 1 to 86,400; else null
    finishes_at  while active only: the timer's finishes_at attribute, ISO in UTC. The timer's `remaining` attribute is fixed
                 while it runs (Home Assistant updates it only on a pause), so it is never read: the countdown is worked out
                 from finishes_at on the site.
  Nothing else: no other attribute, no area, no address or token. At most MAX_SENSORS sensors and MAX_TIMERS timers.
  A call where nothing changed since the last post the site took sends nothing, but one goes at least every FLOOR_S, so the
  site can tell a quiet house from a stopped add-on (Energy calls the readings stale after 75 seconds). A site that does not
  know the route (404) or refuses it (400) is asked again after RETRY_FIRST_S, then twice as long each time it refuses again, up
  to RETRY_MAX_S (1.0.26, bug b-9b7cba8e: 1.0.25 and before waited an hour at once, so one 404 the site answered during a
  database blip at 10:10 PM on 11/9/26 left Energy's Motion card stale for an hour); a post it takes starts the count again.
  Each refusal says so in the Log tab with the wait.

Option: all_motion (default true; false turns the job off). Python 3 standard library only.
"""
import json
import os
import re
from datetime import datetime, timezone

CORE = 'http://supervisor/core/api'
UA = 'lease-home-collector/motion-1'
SENSOR_RE = re.compile(r'^binary_sensor\.[a-z0-9_]{1,80}$')
TIMER_RE = re.compile(r'^timer\.[a-z0-9_]{1,80}_motion_off$')
MOTION_CLASSES = ('motion', 'occupancy')
TIMER_STATES = ('active', 'idle', 'paused')
MAX_SENSORS = 40
MAX_TIMERS = 40
MAX_DURATION_S = 86400
FLOOR_S = 60          # one post at least this often, changed or not
RETRY_FIRST_S = 90    # a refused post (404, 400): asked again after this long, then twice as long each time (1.0.26)
RETRY_MAX_S = 3600    # never longer than this


def retry_after(n):
    """Seconds to wait after the n-th refusal in a row (0 for the first): 90, 180, 360 ... at most an hour. Pure."""
    return min(RETRY_MAX_S, RETRY_FIRST_S * (2 ** max(0, min(int(n), 12))))


def wait_words(s):
    """'90 seconds', '3 minutes', '1 hour'. Pure."""
    s = int(s)
    if s >= 3600:
        return '1 hour'
    return '%d seconds' % s if s < 120 else '%d minutes' % round(s / 60)
DUR_RE = re.compile(r'^(?:(\d{1,2}) days?, )?(\d{1,2}):(\d{2}):(\d{2})(?:\.\d+)?$')


def token():
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


def clean(v, n):
    """Short plain text, else None."""
    if not isinstance(v, str):
        return None
    t = ' '.join(re.sub(r'[\x00-\x1f\x7f<>{}]', ' ', v).split())[:n]
    return t or None


def iso(v):
    """Home Assistant's ISO time (with an offset, or Z) as ISO in UTC to the second, else None. Pure."""
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def duration_s(v):
    """A timer's duration ("0:05:00", "2:00:00", "1 day, 0:00:00", or a number of seconds) in whole seconds, 1 to
    MAX_DURATION_S, else None. Pure."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        s = int(round(v))
    elif isinstance(v, str):
        m = DUR_RE.match(v.strip())
        if not m:
            return None
        d, h, mi, se = m.groups()
        if int(mi) > 59 or int(se) > 59:
            return None
        s = int(d or 0) * 86400 + int(h) * 3600 + int(mi) * 60 + int(se)
    else:
        return None
    return s if 1 <= s <= MAX_DURATION_S else None


def is_sensor(s):
    eid = s.get('entity_id') if isinstance(s, dict) else None
    if not isinstance(eid, str) or not SENSOR_RE.match(eid) or 'battery' in eid:
        return False
    a = s.get('attributes') if isinstance(s.get('attributes'), dict) else {}
    return a.get('device_class') in MOTION_CLASSES


def select(states):
    """(sensors, timers) to send, each sorted by entity id and capped, with only the fields above. Pure."""
    sensors, timers = [], []
    for s in states if isinstance(states, list) else []:
        if not isinstance(s, dict) or not isinstance(s.get('entity_id'), str):
            continue
        eid = s['entity_id']
        a = s.get('attributes') if isinstance(s.get('attributes'), dict) else {}
        name = clean(a.get('friendly_name'), 80)
        if is_sensor(s):
            st = s.get('state')
            sensors.append({'id': eid, 'name': name, 'on': True if st == 'on' else False if st == 'off' else None, 'changed': iso(s.get('last_changed'))})
        elif TIMER_RE.match(eid):
            st = s.get('state')
            if st not in TIMER_STATES:
                continue
            fin = iso(a.get('finishes_at')) if st == 'active' else None
            timers.append({'id': eid, 'name': name, 'state': st, 'duration_s': duration_s(a.get('duration')), 'finishes_at': fin, 'changed': iso(s.get('last_changed'))})
    sensors.sort(key=lambda x: x['id'])
    timers.sort(key=lambda x: x['id'])
    return sensors[:MAX_SENSORS], timers[:MAX_TIMERS]


def signature(sensors, timers):
    return json.dumps([sensors, timers], sort_keys=True, separators=(',', ':'))


def due(state, sig, now):
    """Whether to post: anything changed since the last post the site took, or FLOOR_S has passed. Pure."""
    at = state.get('sent_at')
    return sig != state.get('sent_sig') or at is None or now - float(at) >= FLOOR_S


def run(cfg, now, http):
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    lines = []
    if cfg.get('fresh'):   # a restart asks again at once: a wait held from before it is dropped (1.0.26)
        state.pop('off_until', None)
        state.pop('off_n', None)
    if cfg.get('all_motion') is False:
        state['log'] = [] if state.get('off_said') else ['motion: all_motion is off']
        state['off_said'] = True
        return state
    state.pop('off_said', None)
    site, tok = site_base(cfg), token()
    if not site or not tok:
        state['log'] = ['motion: the site link or the Supervisor token is missing']
        return state
    if now < float(state.get('off_until') or 0):
        state['log'] = []
        return state
    try:
        status, data = http('GET', CORE + '/states', {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
        states = json.loads((data or b'null').decode('utf-8')) if 200 <= status < 300 else None
    except (OSError, ValueError):
        states = None
    if not isinstance(states, list):
        state['log'] = []
        return state
    sensors, timers = select(states)
    found = '%d,%d' % (len(sensors), len(timers))
    if found != state.get('found'):   # once per change: how many were found (counts only)
        state['found'] = found
        lines.append('motion: found %d motion sensors and %d motion timers' % (len(sensors), len(timers)))
    if not sensors and not timers and not state.get('sent_sig'):
        state['log'] = lines
        for line in lines:
            print('[home-collector] ' + line, flush=True)
        return state
    sig = signature(sensors, timers)
    if due(state, sig, now):
        body = {'v': 1, 'at': int(now), 'sensors': sensors, 'timers': timers}
        try:
            st, _ = http('POST', site + '/motion', {'Content-Type': 'application/json', 'User-Agent': UA}, json.dumps(body, separators=(',', ':')).encode('utf-8'), 10)
        except (OSError, ValueError):
            st = 0
        if st == 200:
            state['sent_sig'], state['sent_at'] = sig, now
            was_said, was_off = state.pop('said', None), state.pop('off_n', None)
            if was_said or was_off:
                lines.append('motion: posts are going again')
        elif st in (400, 404):
            n = int(state.get('off_n') or 0)
            wait = retry_after(n)
            state['off_until'], state['off_n'] = now + wait, n + 1
            lines.append('motion: the site %s motion (%d); trying again in %s' % ('does not take' if st == 404 else 'refused', st, wait_words(wait)))
        elif not state.get('said'):
            state['said'] = True
            lines.append('motion: not taken (site answered %d); trying every 5 seconds' % st)
    state['log'] = lines
    for line in lines:
        print('[home-collector] ' + line, flush=True)
    return state
