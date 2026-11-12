# Home collector (Home Assistant add-on)

A Home Assistant add-on for one family's home site, theleases.org. It reads Home Assistant only, through the Supervisor's proxy
to Core's REST API (`homeassistant_api: true`, nothing else: no Supervisor API, no host network, no ports, no shares), and posts
the entities its options name, as fixed kinds, to the site's collector link. It is published here so that Home Assistant can
install and update it from the Add-on Store. It is of no use without a link from that site.

This repository is written by a workflow from the site's own private repository; changes made here are overwritten.

## Install

1. Settings > Add-ons > Add-on Store > the three dots > **Repositories**, add
   `https://github.com/slease/home-collector-addon`, and close the dialog.
2. Find **Home collector** in the store, open it and press **Install**. It builds on the machine, so the first install takes a few
   minutes.
3. On its Configuration tab paste the site's collector link into `push_url` (Site Status, Home collector card, **Make a link**),
   fill in the options below that apply, and save.
4. On its Info tab turn on **Start on boot**, **Watchdog** and **Auto update**, then press **Start**.
5. The Log tab's first line says `version ...`. Site Status's Home collector line ends with the same version within a minute.

## Updates

Home Assistant checks this repository every few hours and, with **Auto update** on, installs a new version once it is a day old.
To take one at once: Add-on Store > the three dots > **Check for updates**, then **Update** on the add-on. Site Status says when
the house runs an older add-on than the site expects, and sends one quiet Phone Alert if it is still behind an hour later.

## Moving from a local copy

An add-on installed from a copied `/addons/home-collector` folder ("Local add-ons") and this one are two different add-ons to
Home Assistant (the store adds a prefix to the slug), so the move is one by hand. There is no need to update the local copy first:

1. Open the local Home collector's Configuration tab, choose **Edit in YAML** from the three dots, and copy everything to a
   safe place (it holds the collector link and service keys: a password manager, not a note).
2. Uninstall the local Home collector. Its saved job state (`/data`) does not carry over: the jobs start fresh and the site keeps
   each record once, so nothing is counted twice.
3. Install from this repository as above, open Configuration, **Edit in YAML**, paste the options back and save.
4. Turn on **Auto update** and start it. The `/addons/home-collector` folder can then be deleted.

## Options

| Option | Default | What it does |
| --- | --- | --- |
| `push_url` | blank | The site's collector link. Only that site is ever posted to. |
| `poll_seconds` | 15 | How often Home Assistant is read, 5 to 300. A heartbeat goes every 5 minutes. |
| `entities` | none | Entities to send, each with a kind: door, sensor, presence, battery, device_online, media, nas, ups, router_client, light, printer, lock, camera, alarm, garage or appliance. The site takes lock, camera, alarm and garage only when its owner allows them. |
| `all_lights` | true | Every `light.*` entity, on or off with its brightness and area. Light groups are skipped. |
| `all_appliances` | true | Every appliance status entity (washer, dryer, oven and the like), at most 30. |
| `all_power`, `power_exclude`, `power_estimated`, `power_measured` | true, none | Every power and energy sensor as 5-minute buckets; words that skip a sensor or mark its watts estimated or measured. |
| `powerwall_*` | blank | Only when the Log tab says a battery gateway reading is ambiguous: its sensor's entity id. |
| `all_router_clients` | true | The router's client list every 15 minutes. |
| `lan_prefix` | blank | The house LAN's first three parts, such as `10.0.0`, a private range (10, 172.16 to 172.31, 192.168). A client's address is sent only inside it; blank sends none. |
| `all_motion` | true | Motion and occupancy sensors and motion timers. |
| `radarr_*`, `sonarr_*`, `plex_url`, `plex_token` | blank | Local media services, read only (Radarr acts only on the site owner's own requests). Keys stay on this machine and are never sent. |
| `poster_player` | blank | A media player for the site's poster screen. |
| `plex_account_id` | blank | The owner's Plex account id, for music plays and album covers. |
| `music_library` | true | The Plex music library, daily. Needs `plex_url` and `plex_token`. |
| `wake_up`, `wake_stage`, `wake_active`, `wake_log` | true, blank | A wake-up routine's helpers: its stage (an `input_text`), whether it runs (an `input_boolean`) and when its log line changed (an `input_text`, optional; never its words). Blank stage or active turns it off. |

## What leaves the house

Only what the options name, in fixed shapes the site checks (anything else is refused whole): states or numbers, friendly names,
units and times. Never a Home Assistant token, a service key, the collector link, a camera image or a file path. Python 3's
standard library only.
