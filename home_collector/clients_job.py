#!/usr/bin/env python3
"""The Home collector's router client job (roadmap R-0164 step 0.1, add-on 1.0.11). collector.py's JOBS entry 'clients' calls
run() every 15 minutes while push_url is set; the option all_router_clients (default true) false turns it off.

Read only. It reads Home Assistant's states (the same Supervisor proxy collector.py uses) and takes every device_tracker entity
whose source_type is router (the ASUSWRT integration's clients), and posts the router's client list to theleases.org:
  POST <site>/clients  { v: 1, clients: [{ mac, name, host, ip, conn, home }] }
    mac   the tracker's mac attribute, upper case with colons (AA:BB:CC:DD:EE:FF); a tracker with no valid MAC is skipped
    name  the friendly name, markup stripped, 200 characters at most, or null (also null when the name is only the MAC)
    host  the host_name attribute, the same way, or null
    ip    the ip attribute when it is on the house LAN, the option lan_prefix's /24 (its first three parts; .1 to .254), else
          null (a guest client, IPv6, blank, or lan_prefix blank, 1.0.30): the site refuses the whole post for any other address
    conn  "wired", "2.4", "5" or "6" when the tracker's attributes say how it is connected, else null
    home  the tracker's state is home
At most MAX_CLIENTS, by MAC. The site side is its own route (src/, R-0164); until it is deployed the site answers 404, which is
logged once an hour and is not an error. The log carries counts only, never a MAC, a name or an address, and so does this job's
state (/data/jobs/clients.json). The ASUSWRT attribute names are from Home Assistant's docs and memory, not checked against a
live Home Assistant; conn reads several likely attribute names and stays null when none is there.

Python 3 standard library only.
"""
import json
import os
import re

CORE = 'http://supervisor/core/api'
MAX_CLIENTS = 500
UA = 'lease-home-collector/clients-1'
SAY_404_S = 3600
MAC_RE = re.compile(r'^[0-9A-F]{2}([:-]?)[0-9A-F]{2}(\1[0-9A-F]{2}){4}$')
CONN_KEYS = ('connection_type', 'connection', 'conn_type', 'interface', 'band', 'connected_via', 'wireless_band')


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


def norm_mac(v):
    """A MAC as AA:BB:CC:DD:EE:FF, else None. Colons, dashes or none at all are read. Pure."""
    if not isinstance(v, str):
        return None
    t = v.strip().upper()
    if not MAC_RE.match(t):
        return None
    h = re.sub(r'[:-]', '', t)
    return ':'.join(h[i:i + 2] for i in range(0, 12, 2))


def clean(v, n):
    """Short plain text, else None. A text that is only a MAC is None (an unnamed client is often named by its MAC). Pure."""
    if not isinstance(v, str):
        return None
    t = ' '.join(re.sub(r'[\x00-\x1f\x7f<>{}]', ' ', v).split())[:n]
    if not t or norm_mac(t) or norm_mac(t.replace('_', ':')):
        return None
    return t


PREFIX_RE = re.compile(r'^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){2}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$')


def lan_prefix(cfg):
    """The option lan_prefix (1.0.30), its first three parts such as 10.0.0, or None when blank, not that shape, or not a private
    range (1.0.31: 10.x, 172.16 to 172.31, 192.168). Pure."""
    v = str((cfg or {}).get('lan_prefix') or '').strip().rstrip('.')
    if not PREFIX_RE.match(v):
        return None
    a, b = (int(x) for x in v.split('.')[:2])
    return v if a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) else None


def clean_ip(v, prefix=None):
    """The house LAN's IPv4 address, <prefix>.1 to .254, else None: the site refuses the whole post for any other (a guest
    network client, IPv6, blank), so those go as null, and with no prefix every address does. Pure."""
    if not isinstance(v, str) or not prefix:
        return None
    m = re.match(r'^' + re.escape(prefix) + r'\.(\d{1,3})$', v.strip())
    return v.strip() if m and 1 <= int(m.group(1)) <= 254 and str(int(m.group(1))) == m.group(1) else None


def conn_of(a):
    """How the client is connected, from its tracker's attributes: wired, 2.4, 5, 6, else None. Pure."""
    for k in CONN_KEYS:
        v = a.get(k) if isinstance(a, dict) else None
        if v is None or isinstance(v, bool):
            continue
        t = str(v).strip().lower().replace(' ', '')
        if not t:
            continue
        wireless = any(w in t for w in ('wlan', 'wifi', 'wi-fi', 'wireless', 'ghz', '2g', '5g', '6g'))
        if not wireless and any(w in t for w in ('wired', 'ethernet', 'lan', 'eth', 'cable')):
            return 'wired'
        if '2.4' in t or '2g' in t or t in ('2', '24'):
            return '2.4'
        if '6g' in t or t == '6' or '6ghz' in t:
            return '6'
        if '5g' in t or t == '5' or '5ghz' in t:
            return '5'
    return None


def clients(states, prefix=None):
    """Every router device_tracker with a valid MAC, as the post's clients, by MAC, at most MAX_CLIENTS. -> (clients, skipped
    for no MAC, left out over the cap). Pure."""
    out, seen, no_mac = [], set(), 0
    for s in states if isinstance(states, list) else []:
        if not isinstance(s, dict):
            continue
        eid = s.get('entity_id')
        a = s.get('attributes') if isinstance(s.get('attributes'), dict) else {}
        if not isinstance(eid, str) or not eid.startswith('device_tracker.') or a.get('source_type') != 'router':
            continue
        mac = norm_mac(a.get('mac'))
        if not mac:
            no_mac += 1
            continue
        if mac in seen:
            continue
        seen.add(mac)
        out.append({'mac': mac, 'name': clean(a.get('friendly_name'), 200), 'host': clean(a.get('host_name'), 200),
                    'ip': clean_ip(a.get('ip'), prefix), 'conn': conn_of(a), 'home': str(s.get('state') or '').lower() == 'home'})
    out.sort(key=lambda c: c['mac'])
    return out[:MAX_CLIENTS], no_mac, max(0, len(out) - MAX_CLIENTS)


def run(cfg, now, http):
    state = dict(cfg.get('state')) if isinstance(cfg.get('state'), dict) else {}
    lines = []
    if cfg.get('all_router_clients') is False:
        if not state.get('off_said'):
            lines.append('clients: all_router_clients is off')
        state['off_said'] = True
        state['log'] = lines
        for line in lines:
            print('[home-collector] ' + line, flush=True)
        return state
    state.pop('off_said', None)
    site, tok = site_base(cfg), token()
    if not site or not tok:
        state['log'] = ['clients: the site link or the Supervisor token is missing']
        return state
    try:
        status, data = http('GET', CORE + '/states', {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
        states = json.loads((data or b'null').decode('utf-8')) if 200 <= status < 300 else None
    except (OSError, ValueError):
        states = None
    if not isinstance(states, list):
        state['log'] = ['clients: Home Assistant did not answer']
        print('[home-collector] ' + state['log'][0], flush=True)
        return state
    cl, no_mac, cut = clients(states, lan_prefix(cfg))
    if not cl and not state.get('sent_any'):   # no router trackers yet: nothing to send, said once
        if not state.get('none_said'):
            lines.append('clients: no router device_tracker entities (ASUSWRT) found')
            state['none_said'] = True
        state['log'] = lines
        for line in lines:
            print('[home-collector] ' + line, flush=True)
        return state
    body = json.dumps({'v': 1, 'clients': cl}, separators=(',', ':')).encode('utf-8')
    try:
        st, _ = http('POST', site + '/clients', {'Content-Type': 'application/json', 'User-Agent': UA}, body)
    except (OSError, ValueError):
        st = 0
    home = sum(1 for c in cl if c['home'])
    extra = (', %d without a MAC skipped' % no_mac if no_mac else '') + (', %d left out over %d' % (cut, MAX_CLIENTS) if cut else '')
    if st == 404:   # the site's clients route is not deployed yet (R-0164): say so once an hour, not every run
        if now - float(state.get('said_404') or 0) >= SAY_404_S:
            lines.append('clients: the site has no clients route yet (404); %d clients kept on this machine' % len(cl))
            state['said_404'] = now
    else:
        state.pop('said_404', None)
        if st == 200:
            state['sent_any'] = True
        lines.append('clients: %d sent (%d home%s), site answered %d' % (len(cl), home, extra, st))
    state['last'] = {'count': len(cl), 'home': home, 'code': st, 'at': now}
    state['log'] = lines
    for line in lines:
        print('[home-collector] ' + line, flush=True)
    return state
