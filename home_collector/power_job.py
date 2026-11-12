#!/usr/bin/env python3
"""The Home collector's power job (roadmap R-0416 "Device power log", step 1.1; 10/30/26: "log them based on power use
alone every minute or so"). collector.py's JOBS entry 'power' calls run() every 5 seconds (1.0.14; every 10 s in 1.0.12, every minute before): the
buckets below still take one reading a clock minute (the first call in each minute), and each call also sends live watts.

Read only. Each minute it reads Home Assistant's states (the same Supervisor proxy collector.py uses) and picks every sensor
whose device_class is power (unit W or kW) or energy (unit Wh, kWh or MWh, state_class total or total_increasing), groups them
by their Home Assistant device (one batched template call, kept an hour: device_id, the device's name, the area), or with no
device by the friendly name less " Power" or " Energy", and folds the readings into 5-minute buckets on the clock (Unix
seconds, multiples of 300). When a bucket closes it posts every device's bucket to theleases.org (src/device-power.js):
  POST <site>/power  { v: 1, devices: [{ key, name, area, estimated }], rows: [{ key, ts, avg_w, kwh, samples, fresh }], cut, failed }
    estimated  true when the watts are a listed wattage (PowerCalc or a template on a light), not a plug's own reading: the sensor
             carries PowerCalc's calculation_mode, source_entity or source_domain (only that they are there is read, never their
             values), or it has no energy counter and its device has a light; power_estimated and power_measured words force it
    key      16 hex characters of SHA-256 over the Home Assistant device id (or the paired name): never the entity id
    avg_w    the mean of the minute readings of the device's power sensor (below zero counts as zero), null with none
    kwh      the rise of the device's energy counter over the bucket (a counter that went back is a reset: counted from zero),
             null on the first bucket, after a gap, or when it reads over 10 kWh in 5 minutes
    samples  minute readings in the bucket
    fresh    of those minutes, how many the sensor had reported to Home Assistant within FRESH_S (its last_reported, else
             last_updated; 1.0.11, R-0416 step 3.1): a plug that went quiet keeps its last watts in Home Assistant, so the site
             reads a bucket with fresh 0 as no live reading. A sensor with neither time counts as fresh.
    cut      devices left out over MAX_DEVICES; failed  minutes Home Assistant did not answer since the last post
A post that fails is kept and sent with the next (two days and MAX_ROWS at most).

Live watts (1.0.12, approved 10/31/26): every call also posts each grouped device's current watts, the same keys and the
same pick() sensor as the buckets, its current state value only (no average, no counter):
  POST <site>/power/live  { v: 1, at, w: { key: watts } }   at: Unix seconds; at most MAX_DEVICES keys, 0 to MAX_W watts
The site keeps only the latest (site_settings power_live) for the Energy page's watts now. A call where no device moved by
LIVE_STEP_W or more since the last post the site took (and the same devices) sends nothing, but one goes at least every
LIVE_FLOOR_S. A site that does not know the route (404) or refuses it (400) is asked again after LIVE_RETRY_FIRST_S, twice as
long after each refusal in a row, at most LIVE_RETRY_MAX_S (1.0.26, bug b-9b7cba8e: an hour at once before).

The Powerwall gateway (1.0.14, roadmap R-0416 step 5.2, approved 11/1/26): the live post also carries the house's
own readings from Home Assistant's Tesla Powerwall integration (the gateway on the LAN, polled by Home Assistant itself):
  site: { house_w, solar_w, battery_w, grid_w, charge_pct, at }
    house_w, solar_w   the load and the panels, W, 0 or more (whole watts)
    battery_w          the Powerwalls, W: above zero discharging, below zero charging (the sign Tesla's own API uses)
    grid_w             the grid, W: above zero drawn from the grid, below zero sent to it
    charge_pct         the Powerwalls' charge, 0 to 100 (the percent the Tesla app shows)
    at                 Unix seconds Home Assistant last heard the gateway (the newest last_reported of the five, else
                       last_updated; the post's own time when none says), never later than the post
  A reading the gateway does not give is null; the block is left out when none reads or the newest is SITE_MAX_AGE_S old.
  Found by discover_site(): the integration's own sensors (Home Assistant's integration_entities('powerwall'), asked in the
  same hourly template call as the devices; with none, sensor.powerwall_* by id), each placed by its device class (power in
  W or kW; battery in %) and the words in its id, then its name (load, solar, battery, site or grid, charge). Two sensors for
  one reading is ambiguous: that reading is left out and the Log tab says which option names it (powerwall_house,
  powerwall_solar, powerwall_battery, powerwall_grid, powerwall_charge: an entity id each, which wins over discovery). Every
  gateway sensor (the integration's, and any an option names) is kept out of the device list and so out of the 5-minute
  buckets: the house total is not a device, and nothing is counted twice. Over the first PROBE_S of a run the job counts
  how often each reading's report time moved and logs it once (a count and seconds, never an id): Home Assistant polls the
  gateway on its own pace, so a 5-second post is the post's pace, not a promise that every reading changes that often.
  1.0.15 (R-0416 step 5.4): over the same first PROBE_S, plug_probe() counts how often each measured device's own report time
  moved and logs once a median and range, then one line per device by its friendly name (never an id).
  1.0.15 also: with no Powerwall integration, or an integration list that comes back empty, the candidates are sensor.powerwall_*
  and sensor.tesla_* by id (for a gateway read through REST sensors, with no integration), a power reading
  may carry no device class (unit W or kW and the role words decide; a charge is % with 'charge' in its id or name), and energy
  counters, the reading time and the Wall Connector are never readings. The block gains grid_on: true while the gateway's grid
  connected binary_sensor (a binary_sensor with 'grid' and 'connected' in its id or name; option powerwall_grid_connected) is
  on, false while off, left out when it is unknown or unavailable; a change posts at once. The signs are not flipped: the
  options and discovery assume the sensors already use the site's (battery above zero discharging, grid above zero drawn in).

Device kinds from Home Assistant's labels (1.0.21, roadmap R-0416 step 6.2, 11/5/26: "HA sets the category"): with the
devices (hourly, or when the set of sensors changes) one more template call asks Home Assistant's label registry, through Core's
template API (labels(), the same /api/template the devices come from; this add-on has no websocket), for each sensor's own labels
and its device's, cut inside Home Assistant to the ids that start with kind_ (no other label ever leaves Home Assistant). A device
takes the kind_* label of its own sensors first, else its Home Assistant device's; only the ids in KIND_LABELS count, and with two
or more the first in KIND_LABELS' fixed order is sent and a warning is logged once. The 5-minute post then carries kinds: true and
each labeled device row a kind (the label id, kind_fridge ...); a device with no label goes with no kind, and the site falls back to
the kind set by hand on Energy. When the label read fails (an error, an older Home Assistant with no labels(), an answer not the
shape) the post carries no kinds at all, so the site keeps every kind it has (never a blank that would wipe one), the Log tab says
so, and it is asked again in LABEL_RETRY_S. A site that refuses the kinds (400, older than 1.0.21's site) gets the same post again
without them, and none for KINDS_BACKOFF_S.

The new Kind labels (1.0.22, roadmap R-0416 step 6.4, 11/5/26): eleven ids, kind_lights_indoor, kind_lights_outdoor,
kind_refrigeration, kind_laundry, kind_heating_and_cooling, kind_entertainment, kind_network_and_computers, kind_plants,
kind_pet_heat, kind_sleep_and_bath and kind_holiday, each the site's kind with kind_ before it. During the move the eleven of
1.0.21 still count (the site takes both), a new id winning over an old one on the same device; the Log tab counts devices by
new and old ids, so the move can be watched to its end.

What is on this plug (1.0.22, R-0416 step 6.4): Home Assistant keeps, for each shared metered plug, a text helper
input_text.<device>_feeds, named for the plug's device name in lower case with underscores (Office Network Rack is
input_text.office_network_rack_feeds). Each one is matched to its device by that rule (a helper two devices could be is
left out) and its text goes on the device row of the 5-minute post as feeds, only when it changed and once each time the add-on
starts; an emptied or removed helper sends '' so the site clears it. Never parsed: "Never turn off" is just text. The site keeps
the latest only, and refuses (by name, with the reason, in its answer, logged here) a text over 255 characters or one its secret
checks flag. A light's rated watts (1.0.22, R-0416 step 6.5): rated_source() reads one number per device, the listed wattage of
an estimated light, sent as rated_w the same way (null clears). It reads input_number.<device>_rated_w by the same naming rule;
the one function is the place to change if Home Assistant keeps it elsewhere. A site that refuses either field (400, older than
1.0.22's) gets the same post again without them, and none for NOTES_BACKOFF_S.

NEVER sent: an entity id, an attribute but the
names, a label other than a kind_ one, a Home Assistant address or token.

Options: all_power (default true; false turns the job off), power_exclude (a list of words: a sensor whose entity id or name
holds one is skipped), power_estimated and power_measured (lists of words that mark a device estimated or measured whatever the
markers say; measured wins). Tesla's own site sensors (solar, grid, Powerwall, load) are never devices:
they would count the house twice. From 1.0.14 the Powerwall's go in the live post's site block instead (above).

Python 3 standard library only. Home Assistant's template functions (device_id, device_attr, area_name) are from its docs, as
the light areas are; not checked against a live Home Assistant.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone

CORE = 'http://supervisor/core/api'
BUCKET_S = 300
MAX_DEVICES = 150   # was 60 until 10/31/26; the site takes the same (POWER_LIMITS in src/device-power.js)
MAX_ROWS = 150 * 24
MAX_KWH = 10.0          # per 5 minutes (120 kW): more is a counter glitch, not a plug
MAX_W = 100000.0
DEV_EVERY_S = 3600      # devices and rooms asked at most hourly, or when the set of sensors changes
KEEP_S = 2 * 86400
UA = 'lease-home-collector/power-1'
POWER_UNITS = {'w': 1.0, 'kw': 1000.0}
ENERGY_UNITS = {'wh': 0.001, 'kwh': 1.0, 'mwh': 1000.0}
DEFAULT_SKIP = ('powerwall', 'solar', 'grid', 'inverter', 'load_power', 'site_power', 'battery_power', 'generator_power')
ID_RE = re.compile(r'^sensor\.[a-z0-9_]+$')
FRESH_S = 15 * 60       # a sensor heard from within this long is a fresh reading (1.0.11)
LIVE_STEP_W = 1.0       # live watts (1.0.12): a change this big or more posts at once
LIVE_FLOOR_S = 60       # and one goes at least this often, changed or not
LIVE_RETRY_FIRST_S = 90   # a refused live post (404, 400, 413): asked again after this long, then twice as long each time (1.0.26)
LIVE_RETRY_MAX_S = 3600   # never longer than this
# The Powerwall gateway's readings (1.0.14, R-0416 step 5.2)
SITE_ROLES = ('house', 'solar', 'battery', 'grid', 'charge')
SITE_FIELDS = {'house': 'house_w', 'solar': 'solar_w', 'battery': 'battery_w', 'grid': 'grid_w', 'charge': 'charge_pct'}
SITE_WORDS = (('house', ('load',)), ('solar', ('solar',)), ('battery', ('battery',)), ('grid', ('site', 'grid')))
SITE_NOT = ('generator', 'busway', 'import', 'export', 'reserve', 'capacity', 'remaining', 'frequency', 'voltage', 'current', 'fan', 'instant',
            'energy', 'reading', 'wall connector', 'connector')   # 1.0.15: counters, the reading time and the Wall Connector are not readings
# 1.0.15 (R-0416 step 5.4): with no Powerwall integration (a gateway read through REST sensors, one API
# call every 30 s), or an integration list that comes back empty, these are the candidates instead
SITE_PREFIXES = ('sensor.powerwall_', 'sensor.tesla_')
# the gateway's grid up or down: a binary_sensor whose id or name has both words (binary_sensor.tesla_grid_connected); on is up
GRID_UP_WORDS = ('grid', 'connected')
BIN_RE = re.compile(r'^binary_sensor\.[a-z0-9_]+$')
SITE_OPT = {'house': 'powerwall_house', 'solar': 'powerwall_solar', 'battery': 'powerwall_battery', 'grid': 'powerwall_grid',
            'charge': 'powerwall_charge', 'grid_up': 'powerwall_grid_connected'}
SITE_MAX_AGE_S = 300    # the gateway's newest report this old or older: the block is left out (the site falls back to Tesla)
SITE_REFRESH_S = 10     # steady readings Home Assistant heard again: the block's time is sent on at most this often
SITE_MAX_IDS = 200
PROBE_S = 300           # the first 5 minutes of a run: how often the gateway's readings moved, logged once
PROBE = {'t0': None, 'seen': {}, 'said': False}   # this process only, so each start of the add-on probes again
PLUG_PROBE = {'t0': None, 'seen': {}, 'said': False}   # 1.0.15: the same first PROBE_S, for each measured device's own report rate
# 1.0.21 (R-0416 step 6.2): Home Assistant's Kind labels, in the fixed order that settles a device with several (the cold ones first,
# for the site's cold watch). The site maps each onto its own kind (the id less kind_) and refuses any other id.
# 1.0.22 (R-0416 step 6.4): the eleven new labels first (refrigeration first, for the cold watch), then the old ones still on devices
# until the labels move; a device carrying both sends the new one.
KIND_LABELS_NEW = ('kind_refrigeration', 'kind_heating_and_cooling', 'kind_pet_heat', 'kind_holiday', 'kind_plants', 'kind_laundry',
                   'kind_sleep_and_bath', 'kind_network_and_computers', 'kind_entertainment', 'kind_lights_outdoor', 'kind_lights_indoor',
                   'kind_vehicle_charging')   # 1.0.24 (R-0416 step 3.14): the 12th, Home Assistant's car-charging estimate
KIND_LABELS_OLD = ('kind_freezer', 'kind_fridge', 'kind_ac', 'kind_heat_lamp', 'kind_holiday', 'kind_plant', 'kind_washer', 'kind_dryer',
                   'kind_computers', 'kind_av', 'kind_light')
KIND_LABELS = KIND_LABELS_NEW + tuple(k for k in KIND_LABELS_OLD if k not in KIND_LABELS_NEW)
LABEL_RETRY_S = 600     # a label read that failed is tried again after this long (the devices themselves stay hourly)
KINDS_BACKOFF_S = 3600  # a site that refused the kinds (400) is sent none for this long
# 1.0.22 (R-0416 steps 6.4 and 6.5): what is on a shared plug, and a light's rated watts, from Home Assistant's helpers
FEEDS_RE = re.compile(r'^input_text\.([a-z0-9_]+)_feeds$')
RATED_RE = re.compile(r'^input_number\.([a-z0-9_]+)_rated_w$')
FEEDS_MAX = 255         # Home Assistant's own input_text limit; the site refuses more
NOTES_BACKOFF_S = 3600  # a site that refused the fields (400) is sent none for this long
NOTES = {'done': set()}   # this process only: the devices whose text and rating went once since the add-on started


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


def _num(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float('inf'), float('-inf')) else None


def excluded(eid, name, extra=()):
    hay = (eid + ' ' + (name or '')).lower()
    return any(w in eid for w in DEFAULT_SKIP) or any(w and w.lower() in hay for w in extra)


def reported_at(s):
    """When Home Assistant last heard from the sensor, Unix seconds: last_reported (Home Assistant 2024.3 and later, set even when
    the value repeats), else last_updated. None when neither reads. Pure."""
    for k in ('last_reported', 'last_updated'):
        v = s.get(k) if isinstance(s, dict) else None
        if isinstance(v, str) and v.strip():
            try:
                d = datetime.fromisoformat(v.strip().replace('Z', '+00:00'))
            except ValueError:
                continue
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.timestamp()
    return None


MARKERS = ('calculation_mode', 'source_entity', 'source_domain')   # PowerCalc's attributes on an estimated sensor (only their presence is read)


def select(states, extra=(), skip=()):
    """{entity id: (kind 'power' | 'energy', value in W or kWh or None, friendly name, estimated marker, reported_at)} for every
    power and energy sensor. The marker is True when the sensor carries PowerCalc's attributes; their values are never read or sent. Pure."""
    out = {}
    for s in states if isinstance(states, list) else []:
        if not isinstance(s, dict):
            continue
        eid = s.get('entity_id')
        a = s.get('attributes') if isinstance(s.get('attributes'), dict) else {}
        if not isinstance(eid, str) or not ID_RE.match(eid) or 'entity_id' in a:   # a group's attributes list its members
            continue
        dc, unit = a.get('device_class'), str(a.get('unit_of_measurement') or '').strip().lower()
        name = a.get('friendly_name') if isinstance(a.get('friendly_name'), str) else ''
        if dc == 'power' and unit in POWER_UNITS:
            kind, mult = 'power', POWER_UNITS[unit]
        elif dc == 'energy' and unit in ENERGY_UNITS and a.get('state_class') in ('total', 'total_increasing'):
            kind, mult = 'energy', ENERGY_UNITS[unit]
        else:
            continue
        if excluded(eid, name, extra) or eid in skip:   # skip: the Powerwall gateway's sensors (1.0.14), never a device
            continue
        v = _num(s.get('state'))
        if v is not None:
            v = max(0.0, v * mult) if kind == 'power' else v * mult
        out[eid] = (kind, v, name, any(k in a for k in MARKERS), reported_at(s))
    return out


SITE_TPL = "integration_entities('powerwall') | select('match', 'sensor[.]') | list"


def dev_template(ids, site=False):
    """One template: for each sensor, [device id, the device's name, its area, whether that device has a light], as a JSON list
    (ids are checked). site (1.0.14): one more element at the end, the Powerwall integration's sensor ids."""
    parts = []
    for i in ids:
        if ID_RE.match(i):
            parts.append("[device_id('%s'), device_attr('%s', 'name_by_user') or device_attr('%s', 'name'), area_name('%s'), "
                         "(device_id('%s') is not none and (device_entities(device_id('%s')) | select('match', 'light[.]') | list | count) > 0)]" % (i, i, i, i, i, i))
    if site:
        parts.append(SITE_TPL)
    return '{{ [' + ', '.join(parts) + '] | tojson }}'


def _template(http, tok, tpl):
    body = json.dumps({'template': tpl}).encode('utf-8')
    try:
        status, data = http('POST', CORE + '/template', {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'}, body)
        got = json.loads((data or b'null').decode('utf-8')) if 200 <= status < 300 else None
    except (OSError, ValueError):
        return None
    return got if isinstance(got, list) else None


def ask_devices(http, tok, ids):
    """(devices as get_devices gives them or None, the Powerwall integration's sensor ids or None) in one template call. A
    Home Assistant that cannot answer the integration part (an error for the whole template) is asked again without it."""
    if not ids:
        return {}, None
    n = len([i for i in ids if ID_RE.match(i)])
    got = _template(http, tok, dev_template(ids, site=True))
    pw = None
    if got is not None and len(got) == n + 1:
        tail = got[-1]
        if isinstance(tail, list):
            pw = sorted({x for x in tail if isinstance(x, str) and ID_RE.match(x)})[:SITE_MAX_IDS]
        got = got[:-1]
    elif got is None:
        got = _template(http, tok, dev_template(ids))
    return _devices(ids, got), pw


def get_devices(http, tok, ids):
    """{entity id: [device id, device name, area, has a light]} through Core's template API (one call), or None when it fails."""
    if not ids:
        return {}
    return _devices(ids, _template(http, tok, dev_template(ids)))


def _devices(ids, got):
    if not isinstance(got, list):
        return None
    ids = [i for i in ids if ID_RE.match(i)]
    out = {}
    for i, g in zip(ids, got):
        if isinstance(g, list) and len(g) in (3, 4):
            out[i] = [g[0] if isinstance(g[0], str) and g[0] else None, clean(g[1], 60), clean(g[2], 40), len(g) == 4 and g[3] is True]
    return out


def slug(name):
    """A device name as Home Assistant writes it in an entity id: lower case, every run of other characters one underscore.
    Office Network Rack -> office_network_rack. Pure."""
    return re.sub(r'[^a-z0-9]+', '_', str(name or '').lower()).strip('_')


def _text_state(s):
    v = s.get('state') if isinstance(s, dict) else None
    v = v.strip() if isinstance(v, str) else ''
    return '' if v.lower() in ('unknown', 'unavailable', 'none') else v


def rated_source(by_id, sl):
    """A light's rated (listed) watts as Home Assistant keeps them (1.0.22, R-0416 step 6.5), or None. The one place to change if
    Home Assistant keeps the number elsewhere: today the helper input_number.<device>_rated_w, the same naming rule as _feeds.
    by_id: {entity id: state}; sl: the device's slug(). Pure."""
    v = _num((by_id.get('input_number.%s_rated_w' % sl) or {}).get('state'))
    return round(v, 1) if v is not None and 0 < v <= MAX_W else None


def helper_notes(states, groups):
    """({key: {'feeds': text or '', 'rated_w': watts or None, 'helper': whether a feeds helper names it}}, counts) from the states
    Home Assistant already gave (no extra call). A helper is matched to its device by slug(device name); a name two devices share
    is left out. counts: {'feeds': helpers found, 'feeds_matched', 'feeds_loose': [helpers that match no device, by their own
    friendly name], 'rated': helpers found, 'rated_matched'}. Pure."""
    by = {s.get('entity_id'): s for s in states if isinstance(s, dict) and isinstance(s.get('entity_id'), str)} if isinstance(states, list) else {}
    keys = {}
    for key, g in (groups or {}).items():
        keys.setdefault(slug(g.get('name')), []).append(key)
    one = {sl: ks[0] for sl, ks in keys.items() if sl and len(ks) == 1}
    out, c = {}, {'feeds': 0, 'feeds_matched': 0, 'feeds_loose': [], 'rated': 0, 'rated_matched': 0}
    for eid, st in by.items():
        m = FEEDS_RE.match(eid)
        if m:
            c['feeds'] += 1
            key = one.get(m.group(1))
            if key is None:
                a = st.get('attributes') if isinstance(st.get('attributes'), dict) else {}
                c['feeds_loose'].append(clean(a.get('friendly_name'), 60) or m.group(1).replace('_', ' '))
                continue
            c['feeds_matched'] += 1
            out.setdefault(key, {'feeds': '', 'rated_w': None, 'helper': False}).update({'feeds': _text_state(st), 'helper': True})
        elif RATED_RE.match(eid):
            c['rated'] += 1
    for sl, key in one.items():
        r = rated_source(by, sl)
        if r is not None:
            c['rated_matched'] += 1
            out.setdefault(key, {'feeds': '', 'rated_w': None, 'helper': False})['rated_w'] = r
    c['feeds_loose'] = sorted(c['feeds_loose'])[:20]
    return out, c


def note_fields(state, key, cur):
    """The feeds and rated_w a device row carries this post (1.0.22): a field goes when it differs from the last one the site took,
    and once each start while it has a value (or had one). {} when nothing goes. Pure but for reading state and NOTES."""
    sent = (state.get('notes_sent') or {}).get(key) or ['', None]
    cur = cur or {'feeds': '', 'rated_w': None, 'helper': False}
    start = key not in NOTES['done']
    out = {}
    if cur['feeds'] != sent[0] or (start and (cur['feeds'] or sent[0] or cur.get('helper'))):
        out['feeds'] = cur['feeds']
    if cur['rated_w'] != sent[1] or (start and (cur['rated_w'] is not None or sent[1] is not None)):
        out['rated_w'] = cur['rated_w']
    return out


def label_template(ids):
    """One template (1.0.21): for each sensor, [its own kind_ labels, its device's kind_ labels], as a JSON list (ids are checked).
    labels() reads Home Assistant's label registry; the select keeps only ids that start with kind_, inside Home Assistant."""
    parts = []
    for i in ids:
        if ID_RE.match(i):
            parts.append("[labels('%s') | select('match', 'kind_') | list, (labels(device_id('%s')) | select('match', 'kind_') | list) if device_id('%s') else []]" % (i, i, i))
    return '{{ [' + ', '.join(parts) + '] | tojson }}'


def ask_labels(http, tok, ids):
    """{entity id: [its kind_ labels, its device's kind_ labels]} through Core's template API (one call), or None when the call
    fails or the answer is not that shape: then no kind is sent at all."""
    ids = [i for i in ids if ID_RE.match(i)]
    if not ids:
        return {}
    got = _template(http, tok, label_template(ids))
    if not isinstance(got, list) or len(got) != len(ids):
        return None
    out = {}
    for i, g in zip(ids, got):
        if not (isinstance(g, list) and len(g) == 2 and all(isinstance(x, list) and all(isinstance(y, str) for y in x) for x in g)):
            return None
        out[i] = [[y for y in g[0] if y.startswith('kind_')][:20], [y for y in g[1] if y.startswith('kind_')][:20]]
    return out


def group_kinds(groups, labels):
    """({key: label id}, {key: [the kind labels it had, when several]}) from group() (which keeps each device's sensor ids) and
    ask_labels(). A device's own sensors' labels first (the one read, then the counter, then the rest by id), else its Home
    Assistant device's; only KIND_LABELS count; of several, the first in KIND_LABELS' order. Pure."""
    kinds, multi = {}, {}
    for key, x in (groups or {}).items():
        ids = [e for e in (x.get('power'), x.get('energy')) if e] + sorted(set(x.get('ids') or []) - {x.get('power'), x.get('energy')})
        own, dev = [], []
        for e in ids:
            ent, dv = (labels or {}).get(e) or [[], []]
            own += [k for k in ent if k in KIND_LABELS and k not in own]
            dev += [k for k in dv if k in KIND_LABELS and k not in dev]
        found = own or dev
        if not found:
            continue
        kinds[key] = min(found, key=KIND_LABELS.index)
        if len(found) > 1:
            multi[key] = sorted(found, key=KIND_LABELS.index)
    return kinds, multi


def base_name(name, eid):
    """The friendly name less a trailing Power or Energy word (pairs a plug's two sensors with no device), else the id's words."""
    n = clean(name, 80) or eid.split('.', 1)[1].replace('_', ' ')
    # a template helper's own words too (10/31/26, for 1.0.8): "X Power Corrected", "X Energy kWh", "X Power Estimated" and
    # "X Energy Estimated" all read as X, so a plug's helpers group as one device (the site also merges them by name on read)
    n = re.sub(r'\s+(power corrected|power estimated|energy kwh|energy estimated|power|energy|consumption|total consumption|energy total|total energy)$', '', n, flags=re.I).strip()
    return n or eid


HELPER_IDS = ('_power_corrected', '_energy_kwh', '_power_estimated', '_energy_estimated')


def pick(ids):
    """The sensor a device is read from when it has several: a helper (Power Corrected, Energy kWh, then the Estimated ones), else
    one whose id says total, else the first by id. Stable. A helper first: three plugs' own counters are Wh labeled kWh."""
    ids = sorted(ids)
    for suf in HELPER_IDS:
        hit = [i for i in ids if i.endswith(suf)]
        if hit:
            return hit[0]
    tot = [i for i in ids if 'total' in i and 'today' not in i]
    return (tot or ids)[0] if ids else None


def words_in(words, *texts):
    hay = ' '.join(t for t in texts if t).lower()
    return any(w and w.lower() in hay for w in words)


def group(sel, devs, est_words=(), meas_words=()):
    """{key: {name, area, power, energy, estimated}} from select() and get_devices(). Pure.
    estimated: the power_measured words win, then the power_estimated words, then PowerCalc's attributes on either sensor, then
    a power reading with no energy counter on a device that has a light (a light's listed wattage, not a plug). Else measured."""
    g = {}
    for eid, t in sel.items():
        kind, name = t[0], t[2]
        d = (devs or {}).get(eid)
        if d and d[0]:
            gid, gname, area = 'd:' + d[0], d[1] or base_name(name, eid), d[2]
        else:
            b = base_name(name, eid)
            gid, gname, area = 'n:' + b.lower(), b, d[2] if d else None
        key = hashlib.sha256(gid.encode('utf-8')).hexdigest()[:16]
        x = g.setdefault(key, {'name': clean(gname, 60) or 'Device', 'area': area, 'p': [], 'e': [], 'gid': gid})
        x['p' if kind == 'power' else 'e'].append(eid)
    # 1.0.9 (10/31/26): a plug's helpers (Power Corrected, Energy kWh) have no device of their own, so 1.0.8 grouped them by name
    # beside the plug's own device of the same name, and the site showed every Innr plug twice. A name group joins the one device
    # group of the same name (its key stays the device's, so the plug's history runs on); pick() then reads the helpers first.
    by_name = {}
    for k, x in g.items():
        if x['gid'].startswith('d:'):
            by_name.setdefault(x['name'].lower(), []).append(k)
    for k in [k for k, x in g.items() if x['gid'].startswith('n:')]:
        hit = by_name.get(g[k]['name'].lower()) or []
        if len(hit) == 1:
            d = g[hit[0]]
            d['p'] += g[k]['p']
            d['e'] += g[k]['e']
            del g[k]
    for x in g.values():
        x.pop('gid', None)
        x['ids'] = sorted(set(x['p'] + x['e']))   # 1.0.21: every sensor of the device, for its labels (group_kinds); never sent
        x['power'], x['energy'] = pick(x.pop('p')), pick(x.pop('e'))
        names = [x['name']] + [(sel.get(e) or (None, None, ''))[2] for e in (x['power'], x['energy']) if e] + [e for e in (x['power'], x['energy']) if e]
        marked = any(e and len(sel.get(e) or ()) > 3 and sel[e][3] for e in (x['power'], x['energy']))
        light = bool(x['power']) and not x['energy'] and bool(((devs or {}).get(x['power']) or [None, None, None, False])[3:4] == [True])
        if words_in(meas_words, *names):
            x['estimated'] = False
        elif words_in(est_words, *names):
            x['estimated'] = True
        else:
            # a sensor named "... Power Estimated" (a template on a light, not PowerCalc) is an estimate too (10/31/26; the site also
            # reads it so from the name in public/energy/devices.js dedupe, until this add-on is installed)
            x['estimated'] = bool(marked or light or any(re.search(r'\bpower estimated\b', (n or ''), re.I) for n in names)
                                  or any(e and (e.endswith('_power_estimated') or e.endswith('_energy_estimated')) for e in (x['power'], x['energy'])))
    return g


def delta(prev, last):
    """kWh between two counter readings: a counter that went back was reset, so it counted from zero. None when unknown or wild."""
    if prev is None or last is None:
        return None
    d = last - prev if last >= prev else last
    return round(d, 5) if 0 <= d <= MAX_KWH else None


def close(state, cur, gap):
    """Close bucket `cur`: each device's row goes to pending. gap: the next bucket is not the following one, so no counter
    rise is carried across it."""
    acc, prev = state.get('acc') or {}, state.setdefault('e_prev', {})
    rows = state.setdefault('pending', [])
    for key, a in acc.items():
        n = int(a.get('n') or 0)
        avg = round(a['sum'] / n, 1) if n and a.get('sum') is not None else None
        kwh = delta(prev.get(key), a.get('e_last'))
        if avg is not None and avg > MAX_W:
            avg = None
        if avg is not None or kwh is not None:
            rows.append({'key': key, 'ts': cur, 'avg_w': avg, 'kwh': kwh, 'samples': max(n, int(a.get('en') or 0)), 'fresh': int(a.get('fn') or 0)})
        if a.get('e_last') is not None:
            prev[key] = a['e_last']
    if gap:
        state['e_prev'] = {}
    state['acc'] = {}


def fold(state, groups, sel, now, kinds=None):
    """One minute's readings into the open bucket, closing the last one when the clock passed a 5-minute mark. Pure but state."""
    b = int(now // BUCKET_S) * BUCKET_S
    cur = state.get('cur')
    if cur is not None and b != cur:
        close(state, int(cur), b - int(cur) > BUCKET_S)
    state['cur'] = b
    acc, meta = state.setdefault('acc', {}), state.setdefault('meta', {})
    for key, g in groups.items():
        a = acc.setdefault(key, {'sum': None, 'n': 0, 'e_last': None, 'en': 0, 'fn': 0})
        meta[key] = [g['name'], g['area'], bool(g.get('estimated'))] + ([kinds[key]] if kinds and kinds.get(key) else [])
        pv = sel.get(g['power'])[1] if g['power'] and sel.get(g['power']) else None
        ev = sel.get(g['energy'])[1] if g['energy'] and sel.get(g['energy']) else None
        if pv is not None:
            a['sum'] = (a['sum'] or 0.0) + pv
            a['n'] += 1
        if ev is not None:
            a['e_last'] = ev
            a['en'] += 1
        # fresh: the sensor the watts come from (else the counter) was heard from within FRESH_S of this minute
        src = g['power'] if pv is not None else g['energy'] if ev is not None else None
        if src:
            at = (list(sel.get(src) or ()) + [None] * 5)[4]
            if at is None or now - at <= FRESH_S:
                a['fn'] = int(a.get('fn') or 0) + 1


def payload(state, now, kinds=False, notes=None):
    """The 5-minute post. kinds (1.0.21): the labels were read this run, so the post says kinds: true and each labeled device row
    carries its kind label id (from meta); False sends no kinds at all, and the site keeps the kinds it has. notes (1.0.22): this
    run's helper_notes(); each device row carries feeds and rated_w only as note_fields() says. None sends none."""
    rows = [r for r in state.get('pending') or [] if r['ts'] >= now - KEEP_S][-MAX_ROWS:]
    state['pending'] = rows
    meta = state.get('meta') or {}
    keys = sorted({r['key'] for r in rows})
    devices = [{'key': k, 'name': (meta.get(k) or ['Device'])[0] or 'Device', 'area': (meta.get(k) or [None, None])[1],
                'estimated': bool((list(meta.get(k) or []) + [None, None, False])[2])} for k in keys]
    for d in devices:
        if d['area'] is None:
            del d['area']
        m = list(meta.get(d['key']) or [])
        if kinds and len(m) > 3 and m[3] in KIND_LABELS:
            d['kind'] = m[3]
        if notes is not None:
            d.update(note_fields(state, d['key'], notes.get(d['key'])))
    body = {'v': 1, 'devices': devices, 'rows': rows, 'cut': int(state.get('cut') or 0), 'failed': int(state.get('failed') or 0)}
    if kinds:
        body['kinds'] = True
    return body


# ---- the Powerwall gateway's readings (1.0.14, R-0416 step 5.2). Home Assistant's Tesla Powerwall integration names its sensors
# "<site> Load power", "Solar power", "Battery power", "Site power" (the grid) in kW and "Charge" in % (device class battery),
# with "Backup reserve" also a battery percent; ids such as sensor.powerwall_load_power (sensor.powerwall_load_now on older
# releases). From Home Assistant's docs, NOT a live gateway: the options below name any sensor when discovery cannot.

def _words(t):
    return [w for w in re.split(r'[^a-z0-9]+', str(t or '').lower()) if w]


def _has(words, w):
    """Whether a SITE_NOT word (or phrase) is in these words. 'energy' in the integration's own device name, "Home Energy
    Gateway", is the gateway, not a counter, so it does not count. Pure."""
    t = ' ' + ' '.join(words) + ' '
    if w == 'energy':
        t = t.replace(' energy gateway ', ' gateway ')
    return (' ' + w + ' ') in t


def site_role(eid, a):
    """Which gateway reading a sensor is ('house', 'solar', 'battery', 'grid', 'charge') or None. Pure."""
    a = a if isinstance(a, dict) else {}
    dc, unit = a.get('device_class'), str(a.get('unit_of_measurement') or '').strip().lower()
    ids, names = _words(eid.split('.', 1)[-1]), _words(a.get('friendly_name'))
    if any(_has(ids, w) or _has(names, w) for w in SITE_NOT):
        return None
    # 1.0.15: a REST sensor may carry no device class; the unit and the words then decide (never another device class)
    if dc in ('battery', None, '') and unit == '%':
        return 'charge' if 'charge' in ids or 'charge' in names or 'charged' in ids else None
    if dc not in ('power', None, '') or unit not in POWER_UNITS:
        return None
    for words in (ids, names[-3:]):   # the id first; the name's last words (the device's own name comes first in it)
        hit = [r for r, ws in SITE_WORDS if any(w in words for w in ws)]
        if len(hit) == 1:
            return hit[0]
        if hit:
            return None
    return None


def grid_up_like(eid, a):
    """Whether a binary_sensor is the gateway's grid connected (on: the grid is up). Pure."""
    a = a if isinstance(a, dict) else {}
    hay = set(_words(eid.split('.', 1)[-1])) | set(_words(a.get('friendly_name')))
    return all(w in hay for w in GRID_UP_WORDS)


def discover_site(states, pw_ids, cfg):
    """({role: entity id}, {role: how many sensors could be it}, [roles whose option names no sensor], every gateway sensor id).
    pw_ids: the integration's sensor ids. When it is None (not known) or empty (1.0.15: no integration, or one with nothing in its
    list), the candidates are sensor.powerwall_* and sensor.tesla_* by id. The grid up role (grid_up) is a binary_sensor with
    'grid' and 'connected' in its id or name. An option (powerwall_<role>, powerwall_grid_connected; an entity id) wins over
    discovery. Pure."""
    by = {s.get('entity_id'): s for s in states if isinstance(s, dict) and isinstance(s.get('entity_id'), str)} if isinstance(states, list) else {}
    fallback = not pw_ids
    cands = [i for i in (pw_ids if not fallback else [e for e in by if e.startswith(SITE_PREFIXES)]) if isinstance(i, str) and ID_RE.match(i)]
    found, amb, missing = {}, {}, []
    for role in SITE_ROLES + ('grid_up',):
        opt = str(cfg.get(SITE_OPT[role]) or '').strip()
        if role == 'grid_up':
            if opt:
                if BIN_RE.match(opt) and opt in by:
                    found[role] = opt
                else:
                    missing.append(role)
                continue
            hit = sorted(i for i in by if BIN_RE.match(i) and grid_up_like(i, (by[i] or {}).get('attributes')))
            if len(hit) == 1:
                found[role] = hit[0]
            elif hit:
                amb[role] = len(hit)
            continue
        if opt:
            if ID_RE.match(opt) and opt in by:
                found[role] = opt
            else:
                missing.append(role)
            continue
        hit = sorted(i for i in cands if i in by and site_role(i, (by[i] or {}).get('attributes')) == role)
        if len(hit) > 1:   # an older and a newer id for one reading: the plain one wins (..._load_power or ..._load_now)
            plain = [i for i in hit if re.search(r'_(power|now|charge|percentage_charged)$', i)]
            hit = plain if len(plain) == 1 else hit
        if len(hit) == 1:
            found[role] = hit[0]
        elif hit:
            amb[role] = len(hit)
    if fallback:   # 1.0.15: by prefix, only the sensors that are site readings (sensor.tesla_* also holds a car's and a Wall
        # Connector's, which stay devices); a house or solar energy counter is the house total too, never a device
        site_w = {w for _r, ws in SITE_WORDS for w in ws}
        cands = [i for i in cands if i.startswith('sensor.powerwall_') or site_w & set(_words(i.split('.', 1)[-1]))]
    ids = set(cands) | set(found.values()) | {str(cfg.get('powerwall_' + r) or '').strip() for r in SITE_ROLES}
    return found, amb, missing, {i for i in ids if ID_RE.match(i)}


def site_read(states, found, now):
    """The site block from the found sensors, or None when none reads or the newest report is SITE_MAX_AGE_S old. Pure."""
    by = {s.get('entity_id'): s for s in states if isinstance(s, dict)} if isinstance(states, list) else {}
    out, times, any_v = {}, [], False
    for role in SITE_ROLES:
        s = by.get(found.get(role)) if found.get(role) else None
        v = None
        if s:
            a = s.get('attributes') if isinstance(s.get('attributes'), dict) else {}
            v = _num(s.get('state'))
            if v is not None and role != 'charge':
                v = v * POWER_UNITS.get(str(a.get('unit_of_measurement') or '').strip().lower(), 1.0)
                v = int(round(max(0.0, v) if role in ('house', 'solar') else v))
                v = v if abs(v) <= MAX_W else None
            elif v is not None:
                v = round(v, 1) if 0 <= v <= 100 else None
            if v is not None:
                any_v = True
                t = reported_at(s)
                if t is not None:
                    times.append(t)
        out[SITE_FIELDS[role]] = v
    if not any_v:
        return None
    at = int(min(now, max(times))) if times else int(now)
    if now - at >= SITE_MAX_AGE_S:
        return None
    out['at'] = at
    g = by.get(found.get('grid_up')) if found.get('grid_up') else None   # 1.0.15: on is up, off is down, anything else left out
    st = str((g or {}).get('state') or '').strip().lower()
    if st in ('on', 'off'):
        out['grid_on'] = st == 'on'
    return out


def probe(states, found, now, lines):
    """Over the first PROBE_S of this process, how often each found reading's report time moved, logged once."""
    if PROBE['said'] or not found:
        return
    if PROBE['t0'] is None:
        PROBE['t0'] = now
    by = {s.get('entity_id'): s for s in states if isinstance(s, dict)} if isinstance(states, list) else {}
    for role, eid in found.items():
        t = reported_at(by.get(eid) or {})
        if t is not None:
            seen = PROBE['seen'].setdefault(role, [])
            if t not in seen and len(seen) < 2000:
                seen.append(t)
    if now - PROBE['t0'] < PROBE_S:
        return
    PROBE['said'] = True
    parts = []
    for role in SITE_ROLES + ('grid_up',):
        if role not in found:
            continue
        seen = sorted(PROBE['seen'].get(role) or [])
        if len(seen) >= 2:
            parts.append('%s %d times (about every %d s)' % (role, len(seen), max(1, round((seen[-1] - seen[0]) / (len(seen) - 1)))))
        else:
            parts.append('%s %s' % (role, 'once' if seen else 'with no report time'))
    lines.append('power: over the first %d minutes Home Assistant updated the Powerwall readings %s; the job reads every 5 s, so faster is not seen'
                 % (PROBE_S // 60, ', '.join(parts)))


def plug_probe(groups, sel, now, lines):
    """1.0.15 (R-0416 step 5.4): over the first PROBE_S of this process, how often each measured device (a grouped device that is
    not estimated; the gateway's sensors are never devices) had a new report time from Home Assistant (reported_at of its power
    sensor, else its energy counter), logged once: a summary line with the median and the range, then one line per device by its
    friendly name (never an entity id), at most MAX_DEVICES. The job reads every 5 s, so a faster plug still shows as 5. Pure but
    for PLUG_PROBE, which is this process only."""
    if PLUG_PROBE['said'] or not isinstance(groups, dict) or not isinstance(sel, dict):
        return
    if PLUG_PROBE['t0'] is None:
        PLUG_PROBE['t0'] = now
    for key, g in groups.items():
        if g.get('estimated'):
            continue
        t = None
        for eid in (g.get('power'), g.get('energy')):
            v = sel.get(eid) if eid else None
            if v and len(v) > 4 and v[4] is not None:
                t = v[4]
                break
        x = PLUG_PROBE['seen'].setdefault(key, {'name': g.get('name') or 'Device', 'at': []})
        x['name'] = g.get('name') or x['name']
        if t is not None and t not in x['at'] and len(x['at']) < 2000:
            x['at'].append(t)
    if now - PLUG_PROBE['t0'] < PROBE_S:
        return
    PLUG_PROBE['said'] = True
    rates, quiet, per = [], 0, []
    for key, x in PLUG_PROBE['seen'].items():
        at = sorted(x['at'])
        if len(at) >= 2:
            n = max(1, int(round((at[-1] - at[0]) / (len(at) - 1))))
            rates.append(n)
            per.append((x['name'].lower(), key, '%s about every %d s' % (x['name'], n)))
        else:
            quiet += 1
            per.append((x['name'].lower(), key, '%s did not report in %d minutes' % (x['name'], PROBE_S // 60)))
    if not per:
        return
    mins = PROBE_S // 60
    if rates:
        rates.sort()
        m = len(rates) // 2
        med = rates[m] if len(rates) % 2 else int(round((rates[m - 1] + rates[m]) / 2))
        head = 'power: over the first %d minutes the plugs reported a median of every %d s (%d to %d s)' % (mins, med, rates[0], rates[-1])
        if quiet:
            head += '; %d did not report again' % quiet
    else:
        head = 'power: over the first %d minutes none of the %d plugs reported again' % (mins, quiet)
    lines.append(head + '; the job reads every 5 s, so faster is not seen')
    for _n, _k, line in sorted(per)[:MAX_DEVICES]:
        lines.append('power: ' + line)


def site_moved(last, cur, now, last_at):
    """Whether the site block is news against the last one the site took. Pure."""
    if (last is None) != (cur is None):
        return True
    if cur is None:
        return False
    if last.get('grid_on') != cur.get('grid_on'):   # 1.0.15: the grid went down or came back: at once
        return True
    for f in ('house_w', 'solar_w', 'battery_w', 'grid_w', 'charge_pct'):
        a, b = last.get(f), cur.get(f)
        if (a is None) != (b is None):
            return True
        if a is not None and abs(float(a) - float(b)) >= (0.1 if f == 'charge_pct' else LIVE_STEP_W):
            return True
    # Home Assistant heard steady readings again: its newer time goes on at most every SITE_REFRESH_S (a block whose time is the
    # post's own, because no sensor carried one, is never news for its time alone)
    at = int(cur.get('at') or 0)
    return int(last.get('at') or 0) < at < int(now) and (last_at is None or now - float(last_at) >= SITE_REFRESH_S)


def live_watts(groups, sel):
    """{key: watts} now: each device's pick()ed power sensor's current value (W, rounded to 0.1), skipping one with no reading or
    over MAX_W. The same keys as the buckets. Pure."""
    out = {}
    for key, g in groups.items():
        t = sel.get(g['power']) if g.get('power') else None
        v = t[1] if t else None
        if v is not None and 0 <= v <= MAX_W:
            out[key] = round(v, 1)
    return out


def live_due(state, w, now, block=None):
    """Whether to post these live watts: the devices changed, any one moved LIVE_STEP_W or more since the last post the site took,
    the Powerwall block moved (site_moved), or LIVE_FLOOR_S has passed since it. Pure."""
    last, at = state.get('live_w'), state.get('live_at')
    if not isinstance(last, dict) or at is None or now - float(at) >= LIVE_FLOOR_S:
        return True
    if site_moved(state.get('live_site') if isinstance(state.get('live_site'), dict) else None, block, now, at):
        return True
    if set(last) != set(w):
        return True
    return any(abs(float(w[k]) - float(last[k])) >= LIVE_STEP_W for k in w)


def post_live(state, site, groups, sel, now, http, lines, block=None):
    if now < float(state.get('live_off_until') or 0):
        return
    w = live_watts(groups, sel)
    if not w and not block and not state.get('live_w') and not state.get('live_site'):   # nothing reads, and nothing did at the last post
        return
    if not live_due(state, w, now, block):
        return
    body = {'v': 1, 'at': int(now), 'w': w}
    if block:
        body['site'] = block
    try:
        st, _ = http('POST', site + '/power/live', {'Content-Type': 'application/json', 'User-Agent': UA}, json.dumps(body, separators=(',', ':')).encode('utf-8'), 10)
    except (OSError, ValueError):
        st = 0
    if st == 200:
        state['live_w'], state['live_at'], state['live_site'] = w, now, block
        was_said, was_off = state.pop('live_said', None), state.pop('live_off_n', None)
        if was_said or was_off:
            lines.append('power: live watts are going again')
    elif st in (400, 404, 413):
        n = int(state.get('live_off_n') or 0)
        wait = min(LIVE_RETRY_MAX_S, LIVE_RETRY_FIRST_S * (2 ** max(0, min(n, 12))))
        state['live_off_until'], state['live_off_n'] = now + wait, n + 1
        words = '1 hour' if wait >= 3600 else ('%d seconds' % wait if wait < 120 else '%d minutes' % round(wait / 60))
        lines.append('power: the site %s live watts (%d); trying again in %s' % ('does not take' if st == 404 else 'refused', st, words))
    elif not state.get('live_said'):
        state['live_said'] = True
        lines.append('power: live watts not taken (site answered %d); trying every 5 seconds' % st)


def label_kinds(state, groups, ids, now, http, tok, lines):
    """(this run's {device key: kind label id}, whether the labels were read) (1.0.21). The labels are asked with the devices
    (state['lab_at'] is cleared then), and again LABEL_RETRY_S after a read that failed; a failed read sends no kinds at all.
    Logs once per change: how many devices are labeled, or that the read failed; and once per device a warning for several labels."""
    if now - float(state.get('lab_at') or 0) >= (DEV_EVERY_S if isinstance(state.get('lab'), dict) else LABEL_RETRY_S):
        state['lab'] = ask_labels(http, tok, ids)
        state['lab_at'] = now
    lab = state.get('lab') if isinstance(state.get('lab'), dict) else None
    if lab is None:
        if state.get('lab_said') != 'failed':
            state['lab_said'] = 'failed'
            lines.append('power: could not read the Kind labels from Home Assistant; no kinds sent (the site keeps the ones it has), trying again in %d minutes' % (LABEL_RETRY_S // 60))
        return {}, False
    kinds, multi = group_kinds(groups, lab)
    warned = set(state.get('kind_warned') or [])
    for key, ks in sorted(multi.items()):
        if key not in warned:
            warned.add(key)
            lines.append('power: %s has %d Kind labels in Home Assistant (%s); sending %s. Keep one.' % ((groups.get(key) or {}).get('name') or 'A device', len(ks), ', '.join(ks), kinds[key]))
    state['kind_warned'] = sorted(warned & set(multi))
    new = sum(1 for k in kinds.values() if k in KIND_LABELS_NEW)
    said = 'ok:%d/%d:%d' % (len(kinds), len(groups), new)
    if state.get('lab_said') != said:
        state['lab_said'] = said
        lines.append('power: Kind labels read from Home Assistant: %d of %d devices labeled (%d with the new ids, %d with the old)' % (len(kinds), len(groups), new, len(kinds) - new))
    return kinds, True


def note_log(state, c, lines):
    """Once per change (1.0.22): how many What is on this plug and rated-watts helpers were found and matched to a device."""
    said = '%d/%d/%s/%d/%d' % (c['feeds'], c['feeds_matched'], ','.join(c['feeds_loose']), c['rated'], c['rated_matched'])
    if state.get('notes_said') == said:
        return
    state['notes_said'] = said
    lines.append('power: What is on this plug: %d input_text.*_feeds helpers found, %d matched to a device%s' % (
        c['feeds'], c['feeds_matched'], ('; %d match no device name (%s)' % (len(c['feeds_loose']), ', '.join(c['feeds_loose']))) if c['feeds_loose'] else ''))
    lines.append('power: rated watts: %d input_number.*_rated_w helpers found, %d matched to a device' % (c['rated'], c['rated_matched']))


def note_answer(state, data, body, lines):
    """After the site took a post (1.0.22): what went is what the site now has, and any text it refused is logged once."""
    sent = state.setdefault('notes_sent', {})
    for d in body.get('devices') or []:
        if 'feeds' in d or 'rated_w' in d:
            was = list(sent.get(d['key']) or ['', None])
            if 'feeds' in d:
                was[0] = d['feeds']
            if 'rated_w' in d:
                was[1] = d['rated_w']
            sent[d['key']] = was
            NOTES['done'].add(d['key'])
    try:
        refused = (json.loads((data or b'{}').decode('utf-8')) or {}).get('refused') or []
    except (ValueError, AttributeError):
        refused = []
    refused = [clean(x, 300) for x in refused if isinstance(x, str)][:20]
    if refused and refused != state.get('notes_refused'):
        for x in refused:
            lines.append('power: the site did not keep a What is on this plug text: %s' % x)
    state['notes_refused'] = refused


def run(cfg, now, http):
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    lines = []
    if cfg.get('fresh'):   # a restart asks again at once: a held live-watts wait is dropped (1.0.26)
        state.pop('live_off_until', None)
        state.pop('live_off_n', None)
    if cfg.get('all_power') is False:
        state['log'] = [] if state.get('off_said') else ['power: all_power is off']
        state['off_said'] = True
        return state
    state.pop('off_said', None)
    site, tok = site_base(cfg), token()
    if not site or not tok:
        state['log'] = ['power: the site link or the Supervisor token is missing']
        return state
    words = lambda k: [str(w).strip() for w in (cfg.get(k) or []) if isinstance(w, str) and str(w).strip()][:50]
    extra, est_words, meas_words = words('power_exclude'), words('power_estimated'), words('power_measured')
    hdr = {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'}
    try:
        status, data = http('GET', CORE + '/states', hdr)
        states = json.loads((data or b'null').decode('utf-8')) if 200 <= status < 300 else None
    except (OSError, ValueError):
        states = None
    minute = int(now // 60)
    sample = minute != state.get('fold_min')   # one bucket reading a clock minute; the calls between only send live watts
    if sample:
        state['fold_min'] = minute
    found, block = {}, None
    if not isinstance(states, list):
        if sample:
            state['failed'] = int(state.get('failed') or 0) + 1
        sel = None
    else:
        pw = state.get('pw_ids') if isinstance(state.get('pw_ids'), list) else None
        found, amb, missing, gw = discover_site(states, pw, cfg)
        sel = select(states, extra, gw)
    if sel is not None:
        ids = sorted(sel)
        sig = hashlib.sha256('|'.join(ids).encode('utf-8')).hexdigest()[:16]
        if sig != state.get('dev_for') or now - float(state.get('dev_at') or 0) >= DEV_EVERY_S:
            got, pw_new = ask_devices(http, tok, ids)
            if got is not None or 'devs' not in state:
                state['devs'] = got or {}
            state['lab_at'] = 0   # 1.0.21: the labels are read again with the devices
            if pw_new is not None and pw_new != state.get('pw_ids'):   # the integration's sensors: rediscover, and keep them out of the devices
                state['pw_ids'] = pw_new
                found, amb, missing, gw = discover_site(states, pw_new, cfg)
                sel = select(states, extra, gw)
                ids = sorted(sel)
                sig = hashlib.sha256('|'.join(ids).encode('utf-8')).hexdigest()[:16]
            state['dev_at'], state['dev_for'] = now, sig
        said = '|'.join([','.join(sorted(found)), ','.join('%s%d' % kv for kv in sorted(amb.items())), ','.join(missing)])
        if said != state.get('site_said'):   # once per change: which readings were found, which are ambiguous, which option names nothing
            state['site_said'] = said
            if found:
                lines.append('power: Powerwall gateway readings found: ' + ', '.join(r for r in SITE_ROLES + ('grid_up',) if r in found))
            elif not amb and not missing:
                lines.append("power: no Powerwall gateway readings in Home Assistant (the Tesla Powerwall integration, or sensors named powerwall_ or tesla_); the site keeps Tesla's 15-minute reading")
            for r, n in sorted(amb.items()):
                lines.append('power: %d sensors could be the Powerwall %s reading; name one with the %s option' % (n, r, SITE_OPT[r]))
            for r in missing:
                lines.append('power: the %s option names no sensor Home Assistant has' % SITE_OPT[r])
        block = site_read(states, found, now)
        probe(states, found, now, lines)
        groups = group(sel, state.get('devs') or {}, est_words, meas_words)
        if len(groups) > MAX_DEVICES:
            keep = sorted(groups, key=lambda k: (groups[k]['name'].lower(), k))[:MAX_DEVICES]
            cut = len(groups) - MAX_DEVICES
            if cut != state.get('cut'):
                names = sorted(groups[k]['name'] for k in groups if k not in set(keep))
                lines.append('power: %d devices left out, over the %d-device limit (the first by name are sent): %s' % (cut, MAX_DEVICES, ', '.join(names)))
            state['cut'] = cut
            groups = {k: groups[k] for k in keep}
        else:
            state['cut'] = 0
        plug_probe(groups, sel, now, lines)
        kinds, kinds_ok = label_kinds(state, groups, ids, now, http, tok, lines)
        notes, nc = helper_notes(states, groups)   # 1.0.22: what is on each shared plug, a light's rated watts
        note_log(state, nc, lines)
        cfg['outcome'] = ('kinds: %d of %d devices labeled in Home Assistant' % (len(kinds), len(groups))) if kinds_ok else 'kinds: none sent (the Kind labels could not be read)'
        if sample:
            fold(state, groups, sel, now, kinds)
        post_live(state, site, groups, sel, now, http, lines, block)
    else:
        kinds_ok, notes = False, None
        cfg['outcome'] = 'kinds: none sent (Home Assistant did not answer the states)'
    if sample and state.get('pending'):
        send_kinds = kinds_ok and now >= float(state.get('kinds_refused_until') or 0)
        send_notes = notes if now >= float(state.get('notes_refused_until') or 0) else None
        body = payload(state, now, send_kinds, send_notes)
        post = lambda b: http('POST', site + '/power', {'Content-Type': 'application/json', 'User-Agent': UA}, json.dumps(b, separators=(',', ':')).encode('utf-8'))
        try:
            st, data = post(body)
        except (OSError, ValueError):
            st, data = 0, b''
        has_notes = any('feeds' in d or 'rated_w' in d for d in body['devices'])
        if st == 400 and (send_kinds or has_notes):   # an older site refuses the kinds (before 1.0.21) or the notes (before 1.0.22): again without them
            if send_kinds:
                state['kinds_refused_until'] = now + KINDS_BACKOFF_S
            if has_notes:
                state['notes_refused_until'] = now + NOTES_BACKOFF_S
            lines.append('power: the site refused the %s (400); the buckets go again without them, and none for an hour'
                         % ' and '.join(x for x, on in (('device kinds', send_kinds), ('What is on this plug and rated watts', has_notes)) if on))
            body = payload(state, now, False)
            try:
                st, data = post(body)
            except (OSError, ValueError):
                st, data = 0, b''
        if st == 200:
            note_answer(state, data, body, lines)
            state['pending'], state['failed'] = [], 0
        elif st in (400, 413):   # the site refused the shape: sending it again would not help
            state['pending'] = []
        lines.append('power: %d buckets from %d devices sent, site answered %d' % (len(body['rows']), len(body['devices']), st))
    state['log'] = lines
    for line in lines:
        print('[home-collector] ' + line, flush=True)
    return state
