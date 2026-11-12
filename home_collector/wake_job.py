#!/usr/bin/env python3
"""The Home collector's wake-up job (add-on 1.0.20; roadmap R-0453, step 1.1). collector.py's JOBS entry 'wake' calls run() every
15 seconds (each poll) while push_url is set.

Read only. Each call reads the wake-up routine's Home Assistant helpers, one at a time (GET /api/states/<entity id> through the
Supervisor proxy), named in the options (1.0.30: no entity id is written here; blank options turn the job off):
  wake_stage    an input_text, the routine's stage: idle, sunrise, briefing, escalated, dining, up or gave_up
  wake_active   an input_boolean, on while the routine runs
  wake_log      optional, an input_text, the routine's own log line. Its words never leave the house: only when it last changed.
and posts to theleases.org (src/wake-up.js) when any of them changed since the last post the site took, and at least every FLOOR_S:
  POST <site>/wake  { v: 1, at, stage, stage_changed, active, active_changed, log_changed }
    stage is one of the seven words or null (any other state, unknown or unavailable); active true, false or null; the *_changed
    times are Home Assistant's last_changed in UTC. Nothing else: no name, no attribute, no other entity.
With none of them in Home Assistant it sends nothing and says so once an hour. An older site (404) or a refused post (400)
is asked again after RETRY_FIRST_S, twice as long after each refusal in a row, at most BACKOFF_S (1.0.26, bug b-9b7cba8e: an
hour at once before, so a 404 during a site database blip held the wake-up stages for an hour). Option: wake_up (default true; false turns the job off). Python 3 standard library only.
"""
import json
import os
import re
from datetime import datetime, timezone

CORE = 'http://supervisor/core/api'
UA = 'lease-home-collector/wake-1'
ID_RE = re.compile(r'^[a-z_]+\.[a-z0-9_]+$')
STAGES = ('idle', 'sunrise', 'briefing', 'escalated', 'dining', 'up', 'gave_up')
FLOOR_S = 300
BACKOFF_S = 3600      # the longest wait after refusals in a row
RETRY_FIRST_S = 90    # the first wait (1.0.26)
NONE_SAY_S = 3600


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


def iso(v):
    """Home Assistant's time as ISO in UTC with a Z, or None. Pure."""
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace('Z', '+00:00'))
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    return t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def helpers(cfg):
    """The (stage, active, log) entity ids from the options (1.0.30), each a valid entity id or None. Pure."""
    def one(k, dom):
        v = str(cfg.get(k) or '').strip().lower()
        return v if ID_RE.match(v) and v.split('.')[0] == dom else None
    return one('wake_stage', 'input_text'), one('wake_active', 'input_boolean'), one('wake_log', 'input_text')


def body_of(stage, active, log, now):
    """The post from the three states (each a dict or None). Pure."""
    st = stage.get('state') if isinstance(stage, dict) else None
    ac = active.get('state') if isinstance(active, dict) else None
    return {'v': 1, 'at': int(now),
            'stage': st if st in STAGES else None, 'stage_changed': iso(stage.get('last_changed')) if isinstance(stage, dict) and st in STAGES else None,
            'active': True if ac == 'on' else False if ac == 'off' else None, 'active_changed': iso(active.get('last_changed')) if isinstance(active, dict) else None,
            'log_changed': iso(log.get('last_changed')) if isinstance(log, dict) else None}


def run(cfg, now, http):
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    if cfg.get('fresh'):   # a restart asks again at once: a wait held from before it is dropped (1.0.26)
        state.pop('off_until', None)
        state.pop('off_n', None)
    if cfg.get('wake_up') is False:
        cfg['outcome'] = 'off (wake_up is false)'
        return state
    stage_id, active_id, log_id = helpers(cfg)
    if not stage_id or not active_id:
        cfg['outcome'] = 'off: wake_stage (an input_text) and wake_active (an input_boolean) are not set in the options'
        return state
    site, tok = site_base(cfg), token()
    if not site or not tok:
        cfg['failed'], cfg['outcome'] = True, 'the site link or the Supervisor token is missing'
        return state
    if now < float(state.get('off_until') or 0):
        cfg['outcome'] = 'waiting until the site takes the wake-up stages again'
        return state
    got, missing, failed = {}, 0, 0
    ask = [e for e in (stage_id, active_id, log_id) if e]
    for eid in ask:
        try:
            status, data = http('GET', CORE + '/states/' + eid, {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
        except (OSError, ValueError):
            status, data = 0, b''
        if status == 404:
            missing += 1
            got[eid] = None
            continue
        try:
            got[eid] = json.loads((data or b'null').decode('utf-8')) if status == 200 else None
        except ValueError:
            got[eid] = None
        if status != 200 or not isinstance(got[eid], dict):
            failed += 1
            got[eid] = None
    if failed:
        cfg['failed'], cfg['outcome'] = True, 'Home Assistant did not answer for %d of the %d helpers; trying again next poll' % (failed, len(ask))
        return state
    if missing == len(ask):
        if now - float(state.get('none_said') or 0) >= NONE_SAY_S:
            state['none_said'] = now
            print('[home-collector] wake: none of the wake-up helpers is in Home Assistant (%s); nothing sent' % stage_id, flush=True)
        cfg['outcome'] = 'no wake-up helpers in Home Assistant'
        return state
    state.pop('none_said', None)
    body = body_of(got.get(stage_id), got.get(active_id), got.get(log_id) if log_id else None, now)
    sig = json.dumps([body['stage'], body['stage_changed'], body['active'], body['active_changed'], body['log_changed']])
    if sig == state.get('sent_sig') and now - float(state.get('sent_at') or 0) < FLOOR_S:
        cfg['outcome'] = 'stage %s, no change' % (body['stage'] or 'unknown')
        return state
    try:
        st, data = http('POST', site + '/wake', {'Content-Type': 'application/json', 'User-Agent': UA}, json.dumps(body, separators=(',', ':')).encode('utf-8'), 10)
    except (OSError, ValueError):
        st, data = 0, b''
    if st == 200:
        state.pop('off_n', None)
        if sig != state.get('sent_sig'):
            print('[home-collector] wake: stage %s, routine %s' % (body['stage'] or 'unknown', 'on' if body['active'] else 'off' if body['active'] is False else 'unknown'), flush=True)
        state['sent_sig'], state['sent_at'] = sig, now
        cfg['outcome'] = 'stage %s, posted' % (body['stage'] or 'unknown')
        return state
    if st in (400, 404):
        n = int(state.get('off_n') or 0)
        wait = min(BACKOFF_S, RETRY_FIRST_S * (2 ** max(0, min(n, 12))))
        state['off_until'], state['off_n'] = now + wait, n + 1
        words = '1 hour' if wait >= 3600 else ('%d seconds' % wait if wait < 120 else '%d minutes' % round(wait / 60))
        try:
            msg = str((json.loads((data or b'{}').decode('utf-8')) or {}).get('message', ''))[:160]
        except (ValueError, AttributeError):
            msg = ''
        cfg['failed'], cfg['outcome'] = True, ('the site does not take the wake-up stages yet (404); trying again in %s' % words if st == 404
                                               else 'the site refused the post (400): %s; trying again in %s' % (msg, words))
        return state
    cfg['failed'], cfg['outcome'] = True, 'the site did not answer (HTTP %d); trying again next poll' % st
    return state
