#!/usr/bin/env python3
"""
seriespack.py -- grab complete-series torrents and import them into Sonarr.

Sonarr's *automatic* pipeline refuses multi-season releases, but its manual-import
API accepts an explicit file -> episode mapping and will import every season in
one shot. This script builds that mapping.

Pipeline:

    series   list Sonarr series and their ids
    search   query Jackett for multi-season / complete-series releases
    grab     send a chosen release to qBittorrent in an isolated category
    status   show what's downloading and what's ready to import
    import   map files to episodes, print the plan, commit only with --confirm

Nothing touches your library without --confirm.

Requires: python 3.8+, `pip install requests`
"""

import argparse
import calendar
import configparser
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "seriespack.ini")
STATE_FILE = os.path.join(HERE, "seriespack_state.json")

TORZNAB_NS = {"torznab": "http://torznab.com/schemas/2015/feed"}


# --------------------------------------------------------------------------
# config / state
# --------------------------------------------------------------------------

class Config:
    def __init__(self, path):
        if not os.path.exists(path):
            sys.exit(
                "No config found at %s\n"
                "Copy seriespack.ini.example to seriespack.ini and fill it in." % path
            )
        # delimiters=('=',) matters: ':' is a ConfigParser delimiter by default,
        # so "D:\downloads = T:\..." would otherwise parse as key "D".
        # interpolation=None keeps '%' in paths from being treated as a variable.
        cp = configparser.ConfigParser(delimiters=("=",), interpolation=None)
        # keep case on the left-hand side of path mappings (Windows paths)
        cp.optionxform = str
        cp.read(path, encoding="utf-8")
        self.cp = cp

        self.sonarr_url = self._get("sonarr", "url").rstrip("/")
        self.sonarr_key = self._get("sonarr", "api_key")

        # optional -- only needed for `hunt --movies`
        if cp.has_section("radarr"):
            self.radarr_url = self._get("radarr", "url").rstrip("/")
            self.radarr_key = self._get("radarr", "api_key")
        else:
            self.radarr_url = self.radarr_key = None

        # hunt pacing: small batches + a cooldown per item is what keeps
        # scheduled runs from hammering the indexers
        # clean thresholds
        self.clean_stalled_hours = float(self._get("clean", "stalled_hours", "24"))
        self.clean_meta_hours = float(self._get("clean", "meta_hours", "1"))
        self.clean_seed_ratio = float(self._get("clean", "seed_ratio", "2.0"))
        self.clean_seed_hours = float(self._get("clean", "seed_hours", "336"))
        self.clean_queue_stuck_days = float(self._get("clean", "queue_stuck_days", "3"))

        self.hunt_tv_batch = int(self._get("hunt", "tv_batch", "5"))
        self.hunt_movie_batch = int(self._get("hunt", "movie_batch", "3"))
        self.hunt_cooldown_days = float(self._get("hunt", "cooldown_days", "7"))
        self.hunt_delay = float(self._get("hunt", "delay_seconds", "5"))
        # a season missing at least this fraction of its aired episodes is
        # hunted as one SeasonSearch instead of per-episode searches
        self.hunt_season_threshold = float(self._get("hunt", "season_threshold", "0.8"))
        # a show with at least this many mostly-missing seasons is hunted as
        # ONE complete-series pack grab instead of season-by-season; 0 disables
        self.hunt_pack_min_seasons = int(self._get("hunt", "pack_min_seasons", "2"))
        # series-pack grabs per run (each one is a multi-GB download)
        self.hunt_pack_batch = int(self._get("hunt", "pack_batch", "1"))
        # auto-import tolerates this fraction of unmapped (never-imported)
        # files; conflicts always block
        self.hunt_pack_max_unmapped = float(self._get("hunt", "pack_max_unmapped", "0.05"))
        # cutoff-unmet (upgrade) searches per run; the *arrs only ever grab
        # strict quality improvements on these. 0 disables.
        self.hunt_upgrade_tv_batch = int(self._get("hunt", "upgrade_tv_batch", "2"))
        self.hunt_upgrade_movie_batch = int(self._get("hunt", "upgrade_movie_batch", "1"))
        # where pack grabs download: 'qbittorrent' (the [qbittorrent] client)
        # or 'seedbox' (.torrent dropped into pack_watch_dir over SFTP;
        # rutorrent seeds it, sync delivers it to pack_import_dir)
        self.hunt_pack_delivery = self._get(
            "hunt", "pack_delivery", "qbittorrent").strip().lower()
        self.hunt_pack_watch = self._get("hunt", "pack_watch_dir", "rwatch/packs")
        self.hunt_pack_import_dir = self._get("hunt", "pack_import_dir", "")
        # no new pack grabs while this many are still downloading/importing
        # (keeps slow, rare torrents from piling up on the seedbox disk);
        # 0 = unlimited
        self.hunt_pack_pending_max = int(self._get("hunt", "pack_pending_max", "5"))
        # a seedbox pack that hasn't arrived after this many days was
        # probably deleted on the seedbox -- give up its slot (0 = never)
        self.hunt_pack_grab_days = float(self._get("hunt", "pack_grab_days", "14"))

        self.jackett_url = self._get("jackett", "url").rstrip("/")
        self.jackett_key = self._get("jackett", "api_key")
        self.jackett_indexer = self._get("jackett", "indexer", "all")
        self.jackett_cats = self._get("jackett", "categories", "5000")

        # optional since the seedbox took over downloads -- commands that
        # actually talk to qBittorrent check qb_url themselves
        self.qb_url = self._get("qbittorrent", "url", "").rstrip("/") or None
        self.qb_user = self._get("qbittorrent", "username", "")
        self.qb_pass = self._get("qbittorrent", "password", "")
        self.qb_category = self._get("qbittorrent", "category", "seriespacks")
        self.qb_savepath = self._get("qbittorrent", "save_path", "")

        self.min_seeders = int(self._get("preferences", "min_seeders", "3"))
        self.max_size_gb = float(self._get("preferences", "max_size_gb", "400"))
        self.min_file_mb = float(self._get("preferences", "min_file_mb", "50"))
        self.import_mode = self._get("preferences", "import_mode", "copy")
        self.quality_order = [
            q.strip().lower()
            for q in self._get("preferences", "quality_order", "1080p,720p,2160p").split(",")
            if q.strip()
        ]

        # path mappings: qbittorrent-side prefix -> sonarr-side prefix
        self.path_maps = []
        if cp.has_section("paths"):
            for src, dst in cp.items("paths"):
                self.path_maps.append((src.strip(), dst.strip()))

        # optional -- only needed for `sync` (seedbox -> NAS pull)
        if cp.has_section("seedbox"):
            self.seedbox_host = self._get("seedbox", "host")
            self.seedbox_port = int(self._get("seedbox", "port", "22"))
            self.seedbox_user = self._get("seedbox", "username")
            self.seedbox_pass = self._get("seedbox", "password", "")
            self.seedbox_key = self._get("seedbox", "key_file", "")
            self.seedbox_staging = self._get("seedbox", "staging")
        else:
            self.seedbox_host = None
        self.seedbox_skip_samples = self._get(
            "seedbox", "skip_samples", "true").strip().lower() in ("true", "yes", "1", "on")
        self.seedbox_sample_max_mb = float(self._get("seedbox", "sample_max_mb", "200"))
        # extract .zip/.rar/.7z in staging before delivery (the *arrs can't
        # import archives); rar/7z need 7-Zip or UnRAR installed
        self.seedbox_extract = self._get(
            "seedbox", "extract", "true").strip().lower() in ("true", "yes", "1", "on")
        self.seedbox_unrar = self._get("seedbox", "unrar_tool", "")
        # delete delivered items from the import dirs once this old and no
        # longer referenced by any *arr queue; 0 disables
        self.sync_prune_days = float(self._get("seedbox", "prune_days", "3"))
        # sync mappings: seedbox completed dir -> local import dir
        self.sync_maps = []
        if cp.has_section("sync"):
            for src, dst in cp.items("sync"):
                self.sync_maps.append((src.strip(), dst.strip()))

    def _get(self, section, option, default=None):
        if self.cp.has_section(section) and self.cp.has_option(section, option):
            val = self.cp.get(section, option).strip()
            if val:
                return val
        if default is not None:
            return default
        sys.exit("Config is missing [%s] %s" % (section, option))


def safe_console():
    """
    Never let an exotic character kill a run: release and movie titles carry
    things like U+200E (crashed hunt live on a cp1252 console printing a
    Radarr title). Unencodable characters print as '?' instead of raising.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            pass
    return {"grabs": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


# --------------------------------------------------------------------------
# release-name parsing
# --------------------------------------------------------------------------

RANGE_PATTERNS = [
    re.compile(r"\bS(\d{1,2})\s*[-–—~]\s*S?(\d{1,2})\b", re.I),
    re.compile(r"\bSeasons?\s*(\d{1,2})\s*(?:[-–—~]|to|thru|through)\s*(\d{1,2})\b", re.I),
]
COMPLETE_RE = re.compile(
    r"\b(?:complete|full|entire)[\s._-]*(?:series|collection|seasons?|show|set)\b"
    r"|\bseries[\s._-]*complete\b"
    r"|\ball[\s._-]*seasons?\b",
    re.I,
)
SEASON_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})(?![\dEe])", re.I)
SINGLE_EP_RE = re.compile(r"\bS\d{1,2}[\s._-]?E\d{1,3}\b", re.I)
FILE_EP_RE = re.compile(
    r"(?<![A-Za-z0-9])S(\d{1,2})[\s._-]?E(\d{1,3})(?:[\s._-]*(?:E|-E?)(\d{1,3}))?",
    re.I,
)
ALT_EP_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})x(\d{1,3})(?![\d])", re.I)
# '318 Partners Part 1.avi' style: leading 3-4 digit number, season+2-digit ep.
# Only trusted when the season digits agree with the file's Season folder.
NUM_EP_RE = re.compile(r"^\s*(\d{3,4})(?!\d)")
FOLDER_SEASON_RE = re.compile(r"\bseason[\s._-]*(\d{1,2})(?!\d)", re.I)
# '\dx' prefix covers the '1xInterview 1' style seen in the 21JS pack
EXTRAS_RE = re.compile(
    r"(^|[\W_]|\dx)(samples?|extras?|interviews?|featurettes?|"
    r"behind[\W_]the[\W_]scenes|deleted[\W_]scenes?|bloopers?|"
    r"gag[\W_]reels?|trailers?|outtakes?)([\W_]|$)",
    re.I,
)

QUALITY_TOKENS = ["2160p", "1080p", "720p", "480p"]


def detect_seasons(title):
    """
    Return (seasons:set|None, kind:str).

    kind is one of 'range', 'tokens', 'complete', 'single', 'unknown'.
    seasons is None when the release claims completeness without naming numbers.
    """
    if SINGLE_EP_RE.search(title):
        return set(), "single"

    for pat in RANGE_PATTERNS:
        m = pat.search(title)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if hi > lo and hi - lo < 40:
                return set(range(lo, hi + 1)), "range"

    tokens = {int(t) for t in SEASON_TOKEN_RE.findall(title)}
    if len(tokens) >= 3:
        return tokens, "tokens"

    if COMPLETE_RE.search(title):
        return None, "complete"

    if len(tokens) == 1:
        return tokens, "single"

    return set(), "unknown"


def quality_of(title):
    low = title.lower()
    for q in QUALITY_TOKENS:
        if q in low:
            return q
    return "unknown"


def score_release(rel, wanted_seasons, cfg):
    """Higher is better. Returns None if the release should be discarded."""
    seasons, kind = rel["seasons"], rel["kind"]

    if kind in ("single", "unknown"):
        return None
    if rel["seeders"] < cfg.min_seeders:
        return None
    size_gb = rel["size"] / (1024.0 ** 3)
    if size_gb > cfg.max_size_gb:
        return None

    score = 0.0

    # coverage of the seasons we actually want
    if seasons is None:
        coverage = 0.9          # claims complete but didn't enumerate
        rel["coverage"] = "claims complete"
    elif wanted_seasons:
        hit = len(seasons & wanted_seasons)
        coverage = hit / float(len(wanted_seasons))
        rel["coverage"] = "%d/%d seasons" % (hit, len(wanted_seasons))
    else:
        coverage = 0.5
        rel["coverage"] = "%d seasons" % len(seasons)
    score += coverage * 100

    # quality preference
    q = rel["quality"]
    if q in cfg.quality_order:
        score += (len(cfg.quality_order) - cfg.quality_order.index(q)) * 12

    # seeders, with diminishing returns
    score += min(rel["seeders"], 200) ** 0.5 * 3

    # a plausible size for the amount of content
    if size_gb < 0.5:
        score -= 40

    rel["score"] = round(score, 1)
    return score


# --------------------------------------------------------------------------
# path translation (qBittorrent VM -> Sonarr VM)
# --------------------------------------------------------------------------

def _is_windowsish(p):
    return bool(re.match(r"^[A-Za-z]:", p)) or p.startswith("\\\\")


def translate_path(path, mappings):
    """Rewrite a path qBittorrent reported into one Sonarr can open."""
    if not path:
        return path
    norm = path.replace("/", "\\")
    for src, dst in mappings:
        srcn = src.replace("/", "\\").rstrip("\\")
        if norm.lower() == srcn.lower():
            return dst
        if norm.lower().startswith(srcn.lower() + "\\"):
            rest = norm[len(srcn):].lstrip("\\")
            sep = "\\" if _is_windowsish(dst) else "/"
            dstn = dst.rstrip("\\/")
            if sep == "/":
                rest = rest.replace("\\", "/")
            return dstn + sep + rest
    return path


# --------------------------------------------------------------------------
# API clients
# --------------------------------------------------------------------------

class Arr(object):
    """Shared plumbing for Sonarr and Radarr (same auth, same /api/v3 shape)."""

    def __init__(self, base, key):
        self.base = base
        self.s = requests.Session()
        self.s.headers.update({"X-Api-Key": key, "Accept": "application/json"})

    def get(self, path, timeout=60, **params):
        r = self.s.get("%s/api/v3/%s" % (self.base, path), params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def post(self, path, payload):
        r = self.s.post("%s/api/v3/%s" % (self.base, path), json=payload, timeout=60)
        r.raise_for_status()
        return r.json() if r.content else {}

    def all_series(self):
        return self.get("series")

    def find_series(self, query):
        q = query.strip().lower()
        allc = self.all_series()
        exact = [s for s in allc if s["title"].lower() == q]
        if exact:
            return exact
        if q.isdigit():
            byid = [s for s in allc if s["id"] == int(q)]
            if byid:
                return byid
        return [s for s in allc if q in s["title"].lower()]

    def episodes(self, series_id):
        return self.get("episode", seriesId=series_id)

    def manual_import(self, folder):
        # Sonarr walks the folder and probes every file's media info before
        # answering; on a multi-season pack over a network share that takes
        # minutes. Verified live: 60s times out on a large folder.
        #
        # No seriesId: on 4.0.19 the seriesId variant of this endpoint returned
        # 0 items for a pack the plain scan found 48 files in. The plain scan
        # attaches a series to each item; map_files enforces the match instead.
        return self.get(
            "manualimport",
            timeout=600,
            folder=folder,
            filterExistingFiles="true",
        )

    def command(self, payload):
        return self.post("command", payload)

    def wait_command(self, cmd_id, timeout=900):
        deadline = time.time() + timeout
        while time.time() < deadline:
            c = self.get("command/%d" % cmd_id)
            if c.get("status") in ("completed", "failed", "aborted"):
                return c
            time.sleep(2)
        return {"status": "timeout"}

    def missing_page(self, page, **params):
        return self.get("wanted/missing", page=page, pageSize=200, **params)

    def cutoff_page(self, page, **params):
        """Items that have a file but sit below the quality profile cutoff."""
        return self.get("wanted/cutoff", page=page, pageSize=200, **params)

    def delete(self, path, payload=None, **params):
        r = self.s.delete("%s/api/v3/%s" % (self.base, path),
                          params=params, json=payload, timeout=60)
        r.raise_for_status()


class Sonarr(Arr):
    def __init__(self, cfg):
        Arr.__init__(self, cfg.sonarr_url, cfg.sonarr_key)


class Radarr(Arr):
    def __init__(self, cfg):
        Arr.__init__(self, cfg.radarr_url, cfg.radarr_key)


class Jackett:
    def __init__(self, cfg):
        self.cfg = cfg
        self.s = requests.Session()

    def search(self, query):
        url = "%s/api/v2.0/indexers/%s/results/torznab/api" % (
            self.cfg.jackett_url,
            self.cfg.jackett_indexer,
        )
        params = {
            "apikey": self.cfg.jackett_key,
            "t": "search",
            "q": query,
            "cat": self.cfg.jackett_cats,
        }
        try:
            r = self.s.get(url, params=params, timeout=120)
            r.raise_for_status()
        except requests.RequestException as exc:
            print("  ! jackett query failed (%s): %s" % (query, exc))
            return []
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as exc:
            print("  ! could not parse jackett response: %s" % exc)
            return []

        out = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            attrs = {}
            for a in item.findall("torznab:attr", TORZNAB_NS):
                attrs[a.get("name")] = a.get("value")

            link = item.findtext("link") or ""
            enc = item.find("enclosure")
            if enc is not None and enc.get("url"):
                link = enc.get("url")
            magnet = attrs.get("magneturl") or ""

            size = 0
            for cand in (item.findtext("size"), attrs.get("size")):
                try:
                    size = int(cand)
                    break
                except (TypeError, ValueError):
                    continue

            try:
                seeders = int(attrs.get("seeders") or 0)
            except ValueError:
                seeders = 0

            tracker = item.findtext("jackettindexer") or attrs.get("tracker") or "?"

            out.append({
                "title": title,
                "link": link,
                "magnet": magnet,
                "size": size,
                "seeders": seeders,
                "tracker": tracker.strip(),
                "guid": item.findtext("guid") or link,
            })
        return out


class QBit:
    def __init__(self, cfg):
        if not cfg.qb_url:
            sys.exit("This command needs qBittorrent: set [qbittorrent] url "
                     "in the config.")
        self.base = cfg.qb_url
        self.s = requests.Session()
        # qBittorrent's CSRF check rejects requests without a matching Referer
        self.s.headers.update({"Referer": self.base, "Origin": self.base})
        self._login(cfg)

    def _login(self, cfg):
        if not cfg.qb_user:
            return
        r = self.s.post(
            "%s/api/v2/auth/login" % self.base,
            data={"username": cfg.qb_user, "password": cfg.qb_pass},
            timeout=30,
        )
        if r.text.strip() != "Ok.":
            sys.exit("qBittorrent login failed: %s" % r.text.strip())

    def torrents(self, category=None):
        params = {"category": category} if category else {}
        r = self.s.get("%s/api/v2/torrents/info" % self.base, params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def delete(self, hashes, delete_files=True):
        r = self.s.post(
            "%s/api/v2/torrents/delete" % self.base,
            data={"hashes": "|".join(hashes),
                  "deleteFiles": "true" if delete_files else "false"},
            timeout=60,
        )
        r.raise_for_status()

    def add(self, url, category, savepath=""):
        data = {"urls": url, "category": category, "paused": "false", "autoTMM": "false"}
        if savepath:
            data["savepath"] = savepath
        r = self.s.post("%s/api/v2/torrents/add" % self.base, data=data, timeout=120)
        r.raise_for_status()
        if r.text.strip() and r.text.strip() != "Ok.":
            raise RuntimeError("qBittorrent rejected the torrent: %s" % r.text.strip())


# States where the data on disk is not safe to hand to Sonarr even though
# progress reads 1.0: 'moving' = relocating from the temp path to the save
# path, 'checking*' = files under re-verification.
UNSETTLED_STATES = ("moving", "checkingUP", "checkingDL", "checkingResumeData")


def is_complete(t):
    return (
        t.get("progress", 0) >= 1.0
        and t.get("amount_left", 0) == 0
        and t.get("state") not in UNSETTLED_STATES
    )


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f%s" % (n, unit)
        n /= 1024.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def pick_series(sonarr, query):
    matches = sonarr.find_series(query)
    if not matches:
        sys.exit("No series in Sonarr matched %r" % query)
    if len(matches) == 1:
        return matches[0]
    print("Multiple matches -- rerun with a more specific title or the id:\n")
    for s in matches[:25]:
        print("  [%d] %s (%s)" % (s["id"], s["title"], s.get("year", "?")))
    sys.exit(1)


def monitored_seasons(series):
    return {
        s["seasonNumber"]
        for s in series.get("seasons", [])
        if s.get("monitored") and s["seasonNumber"] != 0
    }


def build_queries(series):
    title = series["title"]
    seasons = monitored_seasons(series)
    last = max(seasons) if seasons else 1
    queries = [
        "%s Complete Series" % title,
        "%s Complete" % title,
        "%s S01-S%02d" % (title, last),
        "%s Season 1-%d" % (title, last),
    ]
    if last >= 2:
        queries.append("%s Seasons 1-%d" % (title, last))
    # de-dup while preserving order
    seen, out = set(), []
    for q in queries:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def find_releases(cfg, series, verbose=True):
    jackett = Jackett(cfg)
    wanted = monitored_seasons(series)
    seen, releases = set(), []

    for q in build_queries(series):
        if verbose:
            print("  searching: %s" % q)
        for rel in jackett.search(q):
            key = rel["guid"] or rel["title"]
            if key in seen:
                continue
            seen.add(key)
            rel["seasons"], rel["kind"] = detect_seasons(rel["title"])
            rel["quality"] = quality_of(rel["title"])
            if score_release(rel, wanted, cfg) is not None:
                releases.append(rel)

    releases.sort(key=lambda r: r["score"], reverse=True)
    return releases


def print_releases(releases):
    if not releases:
        print("\nNo multi-season releases passed the filters.")
        print("Try lowering min_seeders, or search the tracker by hand and use "
              "`import --folder` once it's downloaded.")
        return
    print("\n  #  score  seeders  size      quality  coverage         tracker      title")
    print("  " + "-" * 110)
    for i, r in enumerate(releases):
        print("  %-2d %-6s %-8d %-9s %-8s %-16s %-12s %s" % (
            i,
            r["score"],
            r["seeders"],
            human_size(r["size"]),
            r["quality"],
            r.get("coverage", "?"),
            r["tracker"][:12],
            r["title"][:80],
        ))


def folder_season(relpath):
    """
    Season number claimed by the file's containing folder(s), or None.

    Pack roots that name a RANGE of seasons ('Show S01-S05', 'Complete
    Series') are ignored -- only a folder naming one season counts.
    """
    parts = re.split(r"[\\/]", relpath)[:-1]
    for comp in reversed(parts):
        if COMPLETE_RE.search(comp) or any(p.search(comp) for p in RANGE_PATTERNS):
            continue
        m = FOLDER_SEASON_RE.search(comp) or SEASON_TOKEN_RE.search(comp)
        if m:
            return int(m.group(1))
    return None


def episode_lookup(sonarr, series_id):
    idx = {}
    for e in sonarr.episodes(series_id):
        idx[(e["seasonNumber"], e["episodeNumber"])] = e
    return idx


def map_files(items, series_id, ep_idx, cfg):
    """
    Turn manualimport results into a concrete file -> episode plan.

    Returns (planned, skipped, unmapped).
    """
    planned, skipped, unmapped = [], [], []

    for item in items:
        name = item.get("relativePath") or item.get("name") or item.get("path") or ""
        base = os.path.basename(name.replace("\\", "/"))
        size = item.get("size", 0) or 0

        if "sample" in base.lower() or size < cfg.min_file_mb * 1024 * 1024:
            skipped.append((base, "sample/too small"))
            continue

        # The scan runs without a seriesId filter, so Sonarr may attribute a
        # file to a different show. Never import those, and never regex-guess
        # them into this series either.
        item_series = item.get("series") or {}
        if item_series.get("id") is not None and item_series["id"] != series_id:
            unmapped.append((base, ["Sonarr matched a different series: %s"
                                    % item_series.get("title", "?")]))
            continue

        eps = item.get("episodes") or []
        source = "sonarr"
        fseason = folder_season(name)
        veto_note = None

        if eps and fseason is not None:
            got = {e["seasonNumber"] for e in eps}
            if 0 not in got and got != {fseason}:
                # Sonarr's guess contradicts the season folder (seen live in
                # the 21 Jump Street pack: '318 Partners Part 1.avi' in a
                # Season 3 folder mapped to S01E01). Distrust it and re-parse.
                veto_note = "folder says season %d but Sonarr mapped %s" % (
                    fseason, ",".join("S%02d" % s for s in sorted(got)))
                eps = []

        if not eps:
            # Sonarr didn't map it (or was vetoed); try the filename ourselves.
            matched = []
            m = FILE_EP_RE.search(base) or FILE_EP_RE.search(name)
            if m:
                season = int(m.group(1))
                first = int(m.group(2))
                last = int(m.group(3)) if m.group(3) else first
                for num in range(first, last + 1):
                    e = ep_idx.get((season, num))
                    if e:
                        matched.append(e)
            else:
                m2 = ALT_EP_RE.search(base)
                if m2:
                    e = ep_idx.get((int(m2.group(1)), int(m2.group(2))))
                    if e:
                        matched.append(e)
            if not matched and fseason is not None:
                # bare 'NNN Title.avi': season digit(s) + 2-digit episode,
                # trusted only when they agree with the Season folder
                m3 = NUM_EP_RE.match(base)
                if m3 and int(m3.group(1)[:-2]) == fseason:
                    e = ep_idx.get((fseason, int(m3.group(1)[-2:])))
                    if e:
                        matched.append(e)
            if matched and fseason is not None and \
                    {e["seasonNumber"] for e in matched} != {fseason}:
                matched = []    # name and folder disagree -- never guess
            if matched:
                eps = matched
                source = "regex"

        if not eps:
            if EXTRAS_RE.search(base):
                skipped.append((base, "extra"))
                continue
            reasons = list(item.get("rejections") or [])
            if veto_note:
                reasons.append(veto_note)
            unmapped.append((base, reasons))
            continue

        planned.append({
            "base": base,
            "source": source,
            "episodes": eps,
            "payload": {
                "path": item["path"],
                "seriesId": series_id,
                "episodeIds": [e["id"] for e in eps],
                "quality": item.get("quality"),
                "languages": item.get("languages") or [{"id": 1, "name": "English"}],
                "releaseGroup": item.get("releaseGroup") or "",
                "episodeFileId": 0,
                "indexerFlags": item.get("indexerFlags", 0),
            },
            "rejections": item.get("rejections") or [],
        })

    return planned, skipped, unmapped


def print_plan(planned, skipped, unmapped):
    print("\nImport plan")
    print("-" * 78)
    for p in sorted(planned, key=lambda x: [(e["seasonNumber"], e["episodeNumber"]) for e in x["episodes"]]):
        tags = ", ".join(
            "S%02dE%02d" % (e["seasonNumber"], e["episodeNumber"]) for e in p["episodes"]
        )
        marker = {"sonarr": " ", "renumber": "!"}.get(p["source"], "~")
        note = ""
        if p["rejections"]:
            msgs = [r.get("reason", str(r)) if isinstance(r, dict) else str(r) for r in p["rejections"]]
            note = "   [!] " + "; ".join(msgs)
        print(" %s %-22s %s%s" % (marker, tags, p["base"][:60], note))

    if skipped:
        print("\nSkipped (%d):" % len(skipped))
        for base, why in skipped[:15]:
            print("   - %s  (%s)" % (base[:60], why))

    if unmapped:
        print("\nUNMAPPED (%d) -- these will NOT be imported:" % len(unmapped))
        for base, rej in unmapped[:25]:
            msgs = [r.get("reason", str(r)) if isinstance(r, dict) else str(r) for r in rej]
            print("   ? %s%s" % (base[:60], ("  " + "; ".join(msgs)) if msgs else ""))

    print("\n  '~' = mapped by filename regex rather than by Sonarr. Check those rows.")
    print("  '!' = renumbered by --remap/--offset. Double-check those rows.")


SKIP_ARG_RE = re.compile(r"^S(\d{1,2})E(\d{1,3})$", re.I)
REMAP_ARG_RE = re.compile(r"^S(\d{1,2})E(\d{1,3})=S(\d{1,2})E(\d{1,3})$", re.I)
OFFSET_ARG_RE = re.compile(r"^S(\d{1,2}):([+-]\d{1,3})$", re.I)


def parse_remap_args(vals):
    out = {}
    for v in vals:
        m = REMAP_ARG_RE.match(v.strip())
        if not m:
            sys.exit("--remap wants SxxEyy=SxxEyy (e.g. S02E05=S02E06), got %r" % v)
        out[(int(m.group(1)), int(m.group(2)))] = (int(m.group(3)), int(m.group(4)))
    return out


def parse_offset_args(vals):
    out = {}
    for v in vals:
        m = OFFSET_ARG_RE.match(v.strip())
        if not m:
            sys.exit("--offset wants Sxx:+N or Sxx:-N (e.g. S02:+1), got %r" % v)
        out[int(m.group(1))] = int(m.group(2))
    return out


def apply_renumber(planned, unmapped, remaps, offsets, ep_idx):
    """
    Re-target files whose episode numbers Sonarr has wrong.

    Explicit --remap wins over --offset; both override Sonarr's own mapping,
    because the whole point is that the user says Sonarr is wrong. A target
    that doesn't exist in Sonarr's episode list drops the file to unmapped
    rather than guessing.
    """
    if not remaps and not offsets:
        return planned
    kept = []
    for p in planned:
        new_eps, changed, bad = [], False, None
        for e in p["episodes"]:
            key = (e["seasonNumber"], e["episodeNumber"])
            if key in remaps:
                tgt, changed = remaps[key], True
            elif key[0] in offsets:
                tgt, changed = (key[0], key[1] + offsets[key[0]]), True
            else:
                tgt = key
            te = ep_idx.get(tgt)
            if te is None:
                bad = tgt
                break
            new_eps.append(te)
        if bad is not None:
            unmapped.append((p["base"],
                             ["renumber target S%02dE%02d does not exist in Sonarr" % bad]))
            continue
        if changed:
            p = dict(p)
            p["episodes"] = new_eps
            p["payload"] = dict(p["payload"], episodeIds=[e["id"] for e in new_eps])
            p["source"] = "renumber"
        kept.append(p)
    return kept


def parse_skip_args(vals):
    out = set()
    for v in vals:
        m = SKIP_ARG_RE.match(v.strip())
        if not m:
            sys.exit("--skip wants SxxEyy (e.g. S03E02), got %r" % v)
        out.add((int(m.group(1)), int(m.group(2))))
    return out


def apply_skips(planned, skipped, skip_eps):
    """Drop planned files touching any explicitly skipped episode."""
    if not skip_eps:
        return planned
    kept = []
    for p in planned:
        if any((e["seasonNumber"], e["episodeNumber"]) in skip_eps for e in p["episodes"]):
            skipped.append((p["base"], "excluded by --skip"))
        else:
            kept.append(p)
    return kept


def check_conflicts(planned):
    seen, dupes = {}, []
    for p in planned:
        for eid in p["payload"]["episodeIds"]:
            if eid in seen:
                dupes.append((seen[eid], p["base"]))
            seen[eid] = p["base"]
    return dupes


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_series(cfg, args):
    sonarr = Sonarr(cfg)
    rows = sonarr.find_series(args.query) if args.query else sonarr.all_series()
    rows.sort(key=lambda s: s["title"])
    for s in rows:
        seasons = monitored_seasons(s)
        stats = s.get("statistics", {})
        print("[%5d] %-55s %2d monitored seasons  %d/%d eps" % (
            s["id"],
            s["title"][:55],
            len(seasons),
            stats.get("episodeFileCount", 0),
            stats.get("totalEpisodeCount", 0),
        ))


def cmd_search(cfg, args):
    sonarr = Sonarr(cfg)
    series = pick_series(sonarr, args.query)
    print("Series: %s (id %d), monitored seasons: %s"
          % (series["title"], series["id"], sorted(monitored_seasons(series)) or "none"))
    print_releases(find_releases(cfg, series))


def grab_release(cfg, state, series, rel, auto=False):
    """Send a release to qBittorrent and track it. Returns the new hash or None."""
    qb = QBit(cfg)
    before = {t["hash"] for t in qb.torrents(cfg.qb_category)}
    qb.add(rel["link"] or rel["magnet"], cfg.qb_category, cfg.qb_savepath)

    new_hash = None
    for _ in range(20):
        time.sleep(1.5)
        fresh = [t for t in qb.torrents(cfg.qb_category) if t["hash"] not in before]
        if fresh:
            new_hash = fresh[0]["hash"]
            break

    if new_hash:
        meta = {
            "seriesId": series["id"],
            "seriesTitle": series["title"],
            "release": rel["title"],
            "grabbedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if auto:
            meta["auto"] = True
        state.setdefault("grabs", {})[new_hash] = meta
        save_state(state)
    return new_hash


def cmd_grab(cfg, args):
    sonarr = Sonarr(cfg)
    series = pick_series(sonarr, args.query)
    print("Series: %s (id %d)" % (series["title"], series["id"]))

    releases = find_releases(cfg, series)
    if not releases:
        print_releases(releases)
        return

    if args.pick is None:
        print_releases(releases)
        if not args.auto:
            print("\nRe-run with --pick N to grab one, or --auto to take #0.")
            return
        choice = 0
    else:
        choice = args.pick

    if choice < 0 or choice >= len(releases):
        sys.exit("No release at index %d" % choice)

    rel = releases[choice]
    url = rel["link"] or rel["magnet"]
    if not url:
        sys.exit("That release has no usable download link.")

    print("\nGrabbing: %s" % rel["title"])
    print("  %s | %s | %d seeders | %s"
          % (rel["tracker"], human_size(rel["size"]), rel["seeders"], rel.get("coverage", "")))

    state = load_state()
    new_hash = grab_release(cfg, state, series, rel)
    if new_hash:
        print("\nAdded to qBittorrent under category '%s' (hash %s)."
              % (cfg.qb_category, new_hash[:12]))
    else:
        print("\nAdded, but qBittorrent didn't report a new torrent in category '%s'."
              % cfg.qb_category)
        print("Check the client; you can still import later with:")
        print("  seriespack.py import \"%s\" --folder <path>" % series["title"])
        return

    print("Once it finishes:  seriespack.py import \"%s\"" % series["title"])


def cmd_status(cfg, args):
    qb = QBit(cfg)
    state = load_state()
    torrents = qb.torrents(cfg.qb_category)
    if not torrents:
        print("Nothing in qBittorrent category '%s'." % cfg.qb_category)
        return
    print("%-14s %-8s %-10s %-28s %s" % ("hash", "progress", "state", "series", "name"))
    print("-" * 100)
    for t in torrents:
        meta = state["grabs"].get(t["hash"], {})
        print("%-14s %-8s %-10s %-28s %s" % (
            t["hash"][:12],
            "%.0f%%" % (t.get("progress", 0) * 100),
            t.get("state", "?")[:10],
            meta.get("seriesTitle", "-")[:28],
            t.get("name", "")[:50],
        ))


# --------------------------------------------------------------------------
# sync (seedbox -> NAS: staging pull, verify, atomic move into import dir)
# --------------------------------------------------------------------------

def prune_candidates(items, keep_names, now, prune_days):
    """
    items: [(name, mtime)] of DELIVERED entries in an import dir. Prunable
    when older than prune_days AND not named by any *arr queue record.
    prune_days <= 0 disables pruning entirely.
    """
    if prune_days <= 0:
        return []
    cutoff = now - prune_days * 86400.0
    return [n for n, mt in items if n not in keep_names and mt < cutoff]


def parse_iso_utc(s):
    """Unix time for an ISO-8601 UTC stamp like 2026-09-02T05:41:00(.123)Z."""
    if not s:
        return None
    try:
        d = datetime.datetime.fromisoformat(s.strip().rstrip("Zz").split(".")[0])
        return float(calendar.timegm(d.timetuple()))
    except ValueError:
        return None


STUCK_QUEUE_STATES = ("importPending", "importBlocked", "importFailed")


def pick_stuck_queue(records, now, stuck_days):
    """
    Queue downloads finished but unable to import for longer than stuck_days,
    grouped by downloadId (a pack is many per-episode rows). Rows still
    downloading, young, or missing a grab date are never touched.
    """
    out = {}
    for r in records:
        if r.get("trackedDownloadState") not in STUCK_QUEUE_STATES:
            continue
        added = parse_iso_utc(r.get("added"))
        if added is None or now - added < stuck_days * 86400.0:
            continue
        d = out.setdefault(r.get("downloadId") or "rec-%s" % r.get("id"),
                           {"ids": [], "title": r.get("title") or "?",
                            "age_days": (now - added) / 86400.0})
        d["ids"].append(r["id"])
    return out


def plan_sync_items(remote_items, synced, import_existing):
    """
    Which remote items to pull. remote_items: [(name, total_size)].
    Skips items already delivered at the same size, and items whose name
    already exists in the import dir. A size change re-pulls.
    """
    out = []
    for name, size in remote_items:
        if name in import_existing:
            continue
        if synced.get(name) == size:
            continue
        out.append((name, size))
    return out


SAMPLE_RE = re.compile(r"(^|[\W_])samples?([\W_]|$)", re.I)


def is_sample(relpath, size, max_bytes):
    """
    A file is a sample only when it LOOKS like one (a Sample/ folder or a
    'sample' token in the path) AND is small. The size guard protects shows
    with 'Sample' in the title and real content mislabeled 'sample'.
    """
    return size < max_bytes and bool(SAMPLE_RE.search(relpath))


def manifest_matches(remote_manifest, local_manifest):
    """Both are [(relative_path, size)]; order-insensitive equality."""
    return sorted(remote_manifest) == sorted(local_manifest)


def _bdec(b, i):
    """Minimal bencode decoder: returns (value, next_index)."""
    c = b[i:i + 1]
    if c == b"i":
        j = b.index(b"e", i)
        return int(b[i + 1:j]), j + 1
    if c == b"l":
        i, out = i + 1, []
        while b[i:i + 1] != b"e":
            v, i = _bdec(b, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i, out = i + 1, {}
        while b[i:i + 1] != b"e":
            k, i = _bdec(b, i)
            v, i = _bdec(b, i)
            out[k] = v
        return out, i + 1
    j = b.index(b":", i)
    n = int(b[i:j])
    if n < 0 or j + 1 + n > len(b):
        raise ValueError("bad string length")
    return b[j + 1:j + 1 + n], j + 1 + n


def torrent_name(data):
    """
    The exact folder/file name a torrent creates (bencoded info.name), or
    None if the bytes aren't a torrent. This is what the item will be
    called in rtorrent's completed dir and after sync delivers it.
    """
    try:
        meta, _ = _bdec(data, 0)
        return meta[b"info"][b"name"].decode("utf-8", "replace")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None


def seedbox_connect(cfg):
    """(ssh_client, sftp) for the configured seedbox. Caller closes client."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = {"port": cfg.seedbox_port, "username": cfg.seedbox_user, "timeout": 30}
    if cfg.seedbox_key:
        kwargs["key_filename"] = cfg.seedbox_key
    else:
        kwargs["password"] = cfg.seedbox_pass
    client.connect(cfg.seedbox_host, **kwargs)
    return client, client.open_sftp()


def archive_sets(names):
    """
    Group archive filenames into (primary, [other volumes]) sets by stem.

    'x.rar' + 'x.r00'... and 'x.part1.rar' + 'x.part2.rar'... are one set
    each; .zip and .7z are single-file sets. Volumes without a primary are
    left alone -- an incomplete set is never extracted or deleted.
    """
    prim, extra = {}, {}
    for n in sorted(names):
        low = n.lower()
        m = re.match(r"^(.*)\.part(\d+)\.rar$", low)
        if m:
            if int(m.group(2)) == 1:
                prim[m.group(1)] = n
            else:
                extra.setdefault(m.group(1), []).append(n)
            continue
        m = re.match(r"^(.*)\.(rar|zip|7z)$", low)
        if m:
            prim[m.group(1)] = n
            continue
        m = re.match(r"^(.*)\.r\d{2}$", low)
        if m:
            extra.setdefault(m.group(1), []).append(n)
    return [(p, extra.get(stem, [])) for stem, p in sorted(prim.items())]


def find_extractor(configured=""):
    """Path to a rar/7z-capable extractor, or None. Zip needs no tool."""
    cands = [configured] if configured else []
    for name in ("7z", "unrar"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    cands += [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
        r"C:\Program Files\WinRAR\UnRAR.exe",
        r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def extractor_cmd(tool, archive, dest):
    if "7z" in os.path.basename(tool).lower():
        return [tool, "x", "-y", "-aoa", "-o" + dest, archive]
    # unrar wants the destination with a trailing separator
    return [tool, "x", "-y", "-o+", archive, dest + os.sep]


def extract_archives(item_dir, tool):
    """
    Extract every archive set under item_dir in place; the volumes are
    deleted only after their extraction succeeds. Returns (count, errors).
    """
    count, errors = 0, []
    for root, _dirs, files in os.walk(item_dir):
        for primary, volumes in archive_sets(files):
            apath = os.path.join(root, primary)
            try:
                if primary.lower().endswith(".zip"):
                    with zipfile.ZipFile(apath) as zf:
                        bad = zf.testzip()
                        if bad:
                            raise RuntimeError("corrupt member %s" % bad)
                        zf.extractall(root)
                elif not tool:
                    raise RuntimeError("no extractor available -- install "
                                       "7-Zip or set [seedbox] unrar_tool")
                else:
                    r = subprocess.run(
                        extractor_cmd(tool, apath, root),
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    if r.returncode != 0:
                        msg = (r.stderr or b"").decode("utf-8", "replace").strip()
                        raise RuntimeError(msg[:200] or "exit code %d" % r.returncode)
                count += 1
                os.remove(apath)
                for v in volumes:
                    try:
                        os.remove(os.path.join(root, v))
                    except OSError:
                        pass
            except (RuntimeError, OSError, zipfile.BadZipFile) as exc:
                errors.append("%s: %s" % (primary, exc))
    return count, errors


def promote_file_to_dir(path):
    """
    Turn a staged bare file into a same-named directory holding it, so a
    single-file archive torrent can extract in place yet still deliver
    under the exact name the *arr queue references.
    """
    tmp = path + ".xdir"
    os.mkdir(tmp)
    os.replace(path, os.path.join(tmp, os.path.basename(path)))
    os.rename(tmp, path)
    return path


def maybe_extract(stage, kind, cfg):
    """
    Extract archives in a verified staged item before delivery, so the
    *arrs only ever see the unpacked result. Failures never block delivery:
    a corrupt archive delivered as-is goes queue-stuck, and clean --queues
    blocklists + re-searches it, which is the right outcome. (A crash
    between extraction and delivery re-pulls the archives next run --
    wasteful but safe.)
    """
    tool = find_extractor(cfg.seedbox_unrar)
    if kind == "file":
        base = os.path.basename(stage)
        if not archive_sets([base]):
            return stage
        if not base.lower().endswith(".zip") and not tool:
            print("  [!] %s is an archive but no extractor was found -- "
                  "install 7-Zip or set [seedbox] unrar_tool" % base)
            return stage
        stage = promote_file_to_dir(stage)
    n, errors = extract_archives(stage, tool)
    if n:
        print("  extracted %d archive(s)" % n)
    for e in errors:
        print("  [!] extraction failed (delivering as-is): %s" % e)
    return stage


def atomic_deliver(staging_path, import_dir, name):
    """
    Rename a fully-verified item from staging into the import folder.

    os.rename is atomic on one volume, so the watching *arr either sees
    nothing or sees the complete item -- never a partial. A cross-volume
    staging dir is a config error, not something to paper over with a
    slow (and visible) copy.
    """
    dst = os.path.join(import_dir, name)
    if os.path.exists(dst):
        raise RuntimeError("delivery target already exists: %s" % dst)
    try:
        os.rename(staging_path, dst)
    except OSError as exc:
        raise RuntimeError(
            "could not rename staging -> import (%s). The staging folder must "
            "be on the SAME drive as the import folder so the move is atomic; "
            "put [seedbox] staging next to your import dirs." % exc
        )


def list_remote_items(sftp, remote_dir):
    """
    Top-level entries of remote_dir as [(name, total_size, 'file'|'dir')].

    A single-file torrent completes as a bare file, not a folder -- both
    kinds must sync (a bare file crashed the first live run).
    """
    import stat as statmod
    out = []
    for e in sorted(sftp.listdir_attr(remote_dir), key=lambda x: x.filename):
        if statmod.S_ISDIR(e.st_mode):
            man = _sftp_walk(sftp, remote_dir + "/" + e.filename)
            out.append((e.filename, sum(s for _, s in man), "dir"))
        else:
            out.append((e.filename, e.st_size or 0, "file"))
    return out


def pull_item(sftp, cfg, remote_dir, name, kind, import_dir, total):
    """Pull one completed item (file or folder) via staging; deliver atomically."""
    stage = os.path.join(cfg.seedbox_staging, name)
    if kind == "file":
        man = [(name, total)]
        base, stage_root = remote_dir, cfg.seedbox_staging
    else:
        man = _sftp_walk(sftp, remote_dir + "/" + name)
        base, stage_root = remote_dir + "/" + name, stage

    if cfg.seedbox_skip_samples:
        max_b = cfg.seedbox_sample_max_mb * 1024 * 1024
        samples = [f for f in man if is_sample(f[0], f[1], max_b)]
        if samples:
            man = [f for f in man if f not in samples]
            print("  skipping %d sample file(s): %s" % (
                len(samples), ", ".join(r[:40] for r, _ in samples[:3])))
    if not man:
        print("  %s is all samples -- nothing to transfer, marking done" % name)
        return True

    print("  pulling: %s (%s, %d files)" % (name, human_size(total), len(man)))
    for rel, size in man:
        fetched = _sftp_pull_file(
            sftp, base + "/" + rel,
            os.path.join(stage_root, rel.replace("/", os.sep)), size, label=rel)
        if fetched:
            print("    %s (%s)" % (rel, human_size(size)))

    if kind == "file":
        local = [(name, os.path.getsize(stage))] if os.path.exists(stage) else []
    else:
        local = []
        for root, _dirs, files in os.walk(stage):
            for f in files:
                p = os.path.join(root, f)
                local.append((os.path.relpath(p, stage).replace(os.sep, "/"),
                              os.path.getsize(p)))
    if not manifest_matches(man, local):
        raise RuntimeError("verification failed -- staging does not match remote")
    if cfg.seedbox_extract:
        stage = maybe_extract(stage, kind, cfg)
    atomic_deliver(stage, import_dir, name)
    print("  delivered: %s" % name)
    return True


def _sftp_walk(sftp, path):
    """[(relative_path, size)] for every file under path (posix remote)."""
    import stat as statmod
    out = []

    def walk(p, rel):
        for entry in sftp.listdir_attr(p):
            rp = (rel + "/" + entry.filename) if rel else entry.filename
            if statmod.S_ISDIR(entry.st_mode):
                walk(p + "/" + entry.filename, rp)
            else:
                out.append((rp, entry.st_size))

    walk(path, "")
    return out


def eta_str(secs):
    secs = int(secs)
    if secs >= 3600:
        return "%dh%02dm" % (secs // 3600, secs % 3600 // 60)
    if secs >= 60:
        return "%dm%02ds" % (secs // 60, secs % 60)
    return "%ds" % secs


class PullProgress(object):
    """
    Coarse download progress for the task window: one LINE roughly every
    `interval` seconds (percent, transferred/total, speed, ETA). The
    runner's logging pipeline is line-buffered, so a self-overwriting \\r
    ticker would never show up -- discrete lines do, and they read fine in
    the log afterwards. Files that finish inside the interval print nothing.
    """

    def __init__(self, label, total, interval=10.0, clock=time.time, sink=None):
        self.label, self.total, self.interval = label, float(total), interval
        self.clock = clock
        self.sink = sink if sink is not None else (lambda s: print(s))
        self.last_t = clock()
        self.last_b = 0

    def __call__(self, done, _total=None):
        now = self.clock()
        if self.total <= 0 or done >= self.total or \
                now - self.last_t < self.interval:
            return
        rate = (done - self.last_b) / max(now - self.last_t, 1e-9)
        left = (self.total - done) / rate if rate > 0 else 0
        self.sink("      %3d%%  %s / %s  %s/s  ~%s left  %s" % (
            int(done * 100.0 / self.total),
            human_size(done), human_size(self.total),
            human_size(rate), eta_str(left), self.label))
        self.last_t, self.last_b = now, done


# below this, a pull is long enough that silent minutes would look hung
PROGRESS_MIN_BYTES = 50 * 1024 * 1024


def _sftp_pull_file(sftp, remote_file, local_file, size, label=None):
    """Pull one file via a temp name; skip if already present at full size."""
    if os.path.exists(local_file) and os.path.getsize(local_file) == size:
        return False
    d = os.path.dirname(local_file)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = local_file + ".part"
    if size >= PROGRESS_MIN_BYTES:
        sftp.get(remote_file, tmp,
                 callback=PullProgress(label or os.path.basename(local_file), size))
    else:
        sftp.get(remote_file, tmp)
    os.replace(tmp, local_file)
    return True


def _queue_keep_names(cfg):
    """
    Item names every *arr queue currently references (by output path basename
    and by release title). Returns None when any configured *arr can't be
    asked -- in that case NOTHING may be pruned.
    """
    names = set()
    clients = [Sonarr(cfg)]
    if cfg.radarr_url:
        clients.append(Radarr(cfg))
    for client in clients:
        try:
            q = client.get("queue", pageSize=1000)
        except requests.RequestException:
            return None
        for r in q.get("records", []):
            op = (r.get("outputPath") or "").replace("\\", "/").rstrip("/")
            if op:
                names.add(op.split("/")[-1])
            if r.get("title"):
                names.add(r["title"])
    return names


def _prune_delivered(cfg, synced, dry, keep_extra=()):
    keep = _queue_keep_names(cfg)
    if keep is None:
        print("\n[!] an *arr queue was unreachable -- skipping prune this run")
        return
    # seedbox-routed packs aren't in any *arr queue; while their grab is
    # still tracked (not yet imported) they must survive pruning
    keep = keep | set(keep_extra)
    now = time.time()
    for remote_dir, import_dir in cfg.sync_maps:
        if not os.path.isdir(import_dir):
            continue
        delivered = {k.split("|", 1)[1] for k in synced
                     if k.startswith(remote_dir + "|")}
        items = []
        for n in os.listdir(import_dir):
            if n not in delivered:
                continue        # never touch anything sync didn't deliver
            try:
                items.append((n, os.path.getmtime(os.path.join(import_dir, n))))
            except OSError:
                continue
        for n in prune_candidates(items, keep, now, cfg.sync_prune_days):
            p = os.path.join(import_dir, n)
            if dry:
                print("  would prune delivered item: %s" % n)
                continue
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
                print("  pruned delivered item: %s" % n)
            except OSError as exc:
                print("  [!] prune failed for %s: %s" % (n, exc))


def cmd_sync(cfg, args):
    if not cfg.seedbox_host:
        sys.exit("No [seedbox] section in the config -- nothing to sync.")
    if not cfg.sync_maps:
        sys.exit("No [sync] mappings in the config -- nothing to sync.")
    try:
        import paramiko
    except ImportError:
        sys.exit("sync needs paramiko. Run:  pip install paramiko")

    state = load_state()
    synced = state.setdefault("synced", {})
    # snapshot before hygiene drops entries for items gone from the seedbox,
    # so their local delivered copies can still be pruned
    delivered_snapshot = dict(synced)

    print("Connecting to %s ..." % cfg.seedbox_host)
    client, sftp = seedbox_connect(cfg)

    pulled = errors = 0
    try:
        for remote_dir, import_dir in cfg.sync_maps:
            print("\n== %s -> %s ==" % (remote_dir, import_dir))
            if not os.path.isdir(import_dir):
                print("  [!] import dir not reachable from this machine -- skipping")
                continue
            try:
                listed = list_remote_items(sftp, remote_dir)
            except IOError as exc:
                print("  [!] can't list remote dir: %s" % exc)
                errors += 1
                continue

            remote_items = [(n, t) for n, t, _k in listed]
            kind = {n: k for n, t, k in listed}

            # drop state for items that left the seedbox entirely
            present = {n for n, _t, _k in listed}
            for k in [k for k in synced
                      if k.startswith(remote_dir + "|")
                      and k.split("|", 1)[1] not in present]:
                del synced[k]

            existing = set(os.listdir(import_dir))
            todo = plan_sync_items(
                remote_items,
                {k.split("|", 1)[1]: v for k, v in synced.items()
                 if k.startswith(remote_dir + "|")},
                existing)
            print("  %d remote item(s), %d to pull" % (len(remote_items), len(todo)))

            for name, total in todo[: args.limit or None]:
                if args.dry:
                    print("  would pull: %s (%s, %s)" % (name, human_size(total), kind[name]))
                    continue
                try:
                    if pull_item(sftp, cfg, remote_dir, name, kind[name], import_dir, total):
                        synced["%s|%s" % (remote_dir, name)] = total
                        save_state(state)
                        pulled += 1
                except (IOError, OSError, RuntimeError) as exc:
                    errors += 1
                    print("  [!] %s failed (%s); partial stays in staging for resume" % (name, exc))
    finally:
        sftp.close()
        client.close()

    if cfg.sync_prune_days > 0 and not args.no_prune:
        delivered_snapshot.update(synced)
        pending_packs = {m.get("name") for m in (state.get("grabs") or {}).values()
                         if m.get("name")}
        _prune_delivered(cfg, delivered_snapshot, args.dry, keep_extra=pending_packs)
    if not args.dry:
        save_state(state)

    if args.dry:
        print("\nDRY RUN -- nothing was transferred.")
    else:
        print("\n%d item(s) delivered, %d error(s)." % (pulled, errors))


# --------------------------------------------------------------------------
# clean (Cleanuparr-style: flag and remove failed / stalled downloads)
# --------------------------------------------------------------------------

def assess_torrent(t, now, cfg):
    """
    Return why this torrent should be cleaned, or None if it's healthy.

    Deliberately conservative: seeding, actively downloading, and user-paused
    torrents are never flagged, whatever their timestamps say.
    """
    state = t.get("state", "")
    if state in ("error", "missingFiles"):
        return "errored"
    if state == "metaDL":
        if now - t.get("added_on", now) > cfg.clean_meta_hours * 3600.0:
            return "meta-stalled"
        return None
    if state == "stalledDL":
        if now - t.get("last_activity", now) > cfg.clean_stalled_hours * 3600.0:
            return "stalled"
        return None
    return None


SEEDING_STATES = ("uploading", "stalledUP", "stoppedUP", "pausedUP",
                  "queuedUP", "forcedUP")


def assess_seeded(t, cfg):
    """Finished seeds that met the ratio OR seeding-time requirement."""
    if t.get("progress", 0) < 1.0 or t.get("state") not in SEEDING_STATES:
        return None
    if t.get("ratio", 0) >= cfg.clean_seed_ratio:
        return "seeded-ratio"
    if t.get("seeding_time", 0) >= cfg.clean_seed_hours * 3600.0:
        return "seeded-time"
    return None


def unreferenced_on_disk(path):
    """
    True when every file under `path` has hardlink count 1 (nothing else --
    e.g. a Sonarr library import -- links to it). False when something does.
    None when this machine can't see the path, so it can't be verified.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        if os.path.isfile(path):
            return os.stat(path).st_nlink <= 1
        found = False
        for root, _dirs, names in os.walk(path):
            for n in names:
                found = True
                if os.stat(os.path.join(root, n)).st_nlink > 1:
                    return False
        return True if found else None
    except OSError:
        return None


YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _clean_title(s):
    return re.sub(r"[._]+", " ", s).strip(" -([")


def attribute_release(name):
    """
    Best-effort: what was this torrent for?

    Returns ("tv", title, [(season, ep), ...]), ("tv-pack", title, None),
    ("movie", title, year), or (None, cleaned_name, None).
    """
    m = FILE_EP_RE.search(name)
    if m:
        eps = []
        for mm in FILE_EP_RE.finditer(name):
            season, first = int(mm.group(1)), int(mm.group(2))
            last = int(mm.group(3)) if mm.group(3) else first
            for num in range(first, last + 1):
                if (season, num) not in eps:
                    eps.append((season, num))
        return ("tv", _clean_title(name[:m.start()]), eps)

    seasons, kind = detect_seasons(name)
    if kind in ("range", "tokens", "complete", "single") and (seasons or kind == "complete"):
        cut = len(name)
        for pat in RANGE_PATTERNS + [COMPLETE_RE, SEASON_TOKEN_RE]:
            mm = pat.search(name)
            if mm:
                cut = min(cut, mm.start())
        return ("tv-pack", _clean_title(name[:cut]), None)

    my = YEAR_RE.search(name)
    if my:
        return ("movie", _clean_title(name[:my.start()]), int(my.group(0)))
    return (None, _clean_title(name), None)


def _research_removed(cfg, removed):
    """Trigger replacement searches for what the removed torrents contained."""
    sonarr = Sonarr(cfg)
    radarr = Radarr(cfg) if cfg.radarr_url else None
    for t, meta in removed:
        name = t.get("name", "")
        try:
            if meta:
                sonarr.command({"name": "SeriesSearch", "seriesId": meta["seriesId"]})
                print("  re-search: %s (SeriesSearch, was a tracked pack)"
                      % meta.get("seriesTitle", meta["seriesId"]))
                continue
            kind, title, extra = attribute_release(name)
            if kind in ("tv", "tv-pack"):
                matches = sonarr.find_series(title)
                if len(matches) == 1:
                    ser = matches[0]
                    if kind == "tv":
                        idx = episode_lookup(sonarr, ser["id"])
                        ids = [idx[k]["id"] for k in extra if k in idx]
                        if ids:
                            sonarr.command({"name": "EpisodeSearch", "episodeIds": ids})
                            print("  re-search: %s %s (EpisodeSearch)" % (
                                ser["title"],
                                ",".join("S%02dE%02d" % k for k in extra)))
                            continue
                    else:
                        sonarr.command({"name": "SeriesSearch", "seriesId": ser["id"]})
                        print("  re-search: %s (SeriesSearch)" % ser["title"])
                        continue
            elif kind == "movie" and radarr:
                movs = [m for m in radarr.get("movie")
                        if m.get("title", "").lower() == title.lower()]
                if len(movs) == 1:
                    radarr.command({"name": "MoviesSearch", "movieIds": [movs[0]["id"]]})
                    print("  re-search: %s (%s) (MoviesSearch)" % (title, extra))
                    continue
            print("  couldn't attribute %r -- search manually" % name[:60])
        except requests.HTTPError as exc:
            print("  re-search failed for %r: %s" % (name[:50], exc))


def _clean_queues(cfg, do_remove):
    """Remove + blocklist *arr queue items stuck unable to import."""
    targets = [("Sonarr", Sonarr(cfg))]
    if cfg.radarr_url:
        targets.append(("Radarr", Radarr(cfg)))
    now = time.time()
    for label, client in targets:
        try:
            q = client.get("queue", pageSize=1000)
        except requests.RequestException as exc:
            print("\n%s queue unreachable (%s) -- skipping" % (label, exc))
            continue
        stuck = pick_stuck_queue(q.get("records", []), now, cfg.clean_queue_stuck_days)
        print("\n%s queue: %d download(s) stuck in import-pending > %gd"
              % (label, len(stuck), cfg.clean_queue_stuck_days))
        for _did, info in sorted(stuck.items(), key=lambda kv: -kv[1]["age_days"]):
            print("  %-60s %.1fd, %d row(s)" % (
                info["title"][:60], info["age_days"], len(info["ids"])))
            if do_remove:
                # keep seeding, blocklist the release, let a search replace it
                client.delete("queue/bulk", payload={"ids": info["ids"]},
                              removeFromClient="false", blocklist="true",
                              skipRedownload="false")
                print("    removed + blocklisted; replacement search triggered")


def cmd_clean(cfg, args):
    if args.queues:
        _clean_queues(cfg, args.remove)

    if not cfg.qb_url:
        # seedbox-only setups: queue hygiene above is all clean can do
        if args.queues:
            print("\nNo [qbittorrent] url configured -- torrent-client checks skipped.")
            return
        sys.exit("No [qbittorrent] url configured -- without it only "
                 "`clean --queues` does anything.")

    qb = QBit(cfg)
    state = load_state()
    now = time.time()
    cat = args.category or cfg.qb_category
    torrents = qb.torrents(cat)
    if not torrents:
        print("\nNothing in qBittorrent category '%s'." % cat)
        return

    flagged, unverifiable = [], 0
    print("%-14s %-8s %-11s %-14s %s" % ("hash", "progress", "state", "verdict", "name"))
    print("-" * 100)
    for t in torrents:
        pending = t["hash"] in state["grabs"]
        reason = assess_torrent(t, now, cfg)
        # A tracked-but-unimported pack is referenced by US: never treat it
        # as done-seeding or orphaned, whatever the numbers say.
        if reason is None and args.seeded and not pending:
            reason = assess_seeded(t, cfg)
        if reason is None and args.orphans and not pending:
            res = unreferenced_on_disk(t.get("content_path") or "")
            if res is None:
                res = unreferenced_on_disk(
                    translate_path(t.get("content_path") or "", cfg.path_maps))
            if res is True:
                reason = "orphaned"
            elif res is None:
                unverifiable += 1
        print("%-14s %-8s %-11s %-14s %s" % (
            t["hash"][:12],
            "%.0f%%" % (t.get("progress", 0) * 100),
            t.get("state", "?")[:11],
            reason or ("pending-import" if pending else "ok"),
            t.get("name", "")[:48],
        ))
        if reason:
            flagged.append((t, state["grabs"].get(t["hash"])))

    if args.orphans and unverifiable:
        print("\n[!] %d torrent(s) whose files this machine can't see were NOT "
              "orphan-checked.\n    Run clean from a machine that can reach the "
              "download paths for hardlink checking." % unverifiable)

    if not flagged:
        print("\nNothing to clean.")
        return

    print("\n%d torrent(s) flagged." % len(flagged))
    if not args.remove:
        print("DRY RUN -- nothing was removed. Re-run with --remove to delete them and their data.")
        return

    qb.delete([t["hash"] for t, _ in flagged], delete_files=True)
    for t, _meta in flagged:
        state["grabs"].pop(t["hash"], None)
        print("removed: %s" % t.get("name", t["hash"])[:70])
    save_state(state)

    if args.research:
        print("\nTriggering replacement searches:")
        _research_removed(cfg, flagged)
    else:
        print("\nRemoved %d torrent(s) with their data. Re-grab replacements with "
              "`grab`, let `hunt` re-search, or use --research next time."
              % len(flagged))


# --------------------------------------------------------------------------
# hunt (Huntarr-style: gently search the backlog a few items at a time)
# --------------------------------------------------------------------------

def pick_by_key(cands, hunted, now, limit, cooldown_days):
    """
    Candidates outside their cooldown window, never-hunted first, then stalest.

    `hunted` maps candidate key -> unix time of the last triggered search; the
    cooldown is what makes scheduled runs cycle through the whole backlog
    instead of re-searching the same items every time.
    """
    cool = cooldown_days * 86400.0
    out = []
    for c in cands:
        last = hunted.get(c["key"], 0)
        if now - last < cool:
            continue
        out.append((last, c))
    out.sort(key=lambda x: x[0])
    return [c for _, c in out[:limit]]


def pick_hunt_candidates(records, hunted, now, limit, cooldown_days, prefix):
    cands = [{"key": "%s:%d" % (prefix, r["id"]), "record": r} for r in records]
    return [c["record"] for c in pick_by_key(cands, hunted, now, limit, cooldown_days)]


def season_missing_fraction(series, season_number):
    """What fraction of this season's aired episodes has no file? None if unknown."""
    for s in series.get("seasons", []):
        if s.get("seasonNumber") == season_number:
            st = s.get("statistics") or {}
            total = st.get("episodeCount", 0)
            if not total:
                return None
            return (total - st.get("episodeFileCount", 0)) / float(total)
    return None


def build_tv_candidates(records, season_threshold, series_lookup=None):
    """
    Collapse missing-episode records into search candidates.

    A season that's mostly missing (>= season_threshold of its aired episodes)
    becomes ONE season-tier candidate: a single SeasonSearch can grab a season
    pack -- Sonarr's automatic pipeline accepts single-season packs, and the
    search also covers individual episodes, so nothing gets starved if no pack
    exists. Scattered gaps stay individual EpisodeSearch candidates.
    """
    out, seen_seasons = [], set()
    for r in records:
        ser = r.get("series") or {}
        sn = r.get("seasonNumber")
        # wanted/missing embeds series without season statistics; a full
        # series record from GET /series must be supplied for the season tier
        full = (series_lookup or {}).get(ser.get("id")) or ser
        frac = season_missing_fraction(full, sn)
        if ser.get("id") is not None and frac is not None and frac >= season_threshold:
            key = "season:%d:%d" % (ser["id"], sn)
            if key in seen_seasons:
                continue
            seen_seasons.add(key)
            out.append({"kind": "season", "key": key, "seriesId": ser["id"],
                        "seasonNumber": sn, "title": ser.get("title", "?")})
        else:
            out.append({"kind": "episode", "key": "ep:%d" % r["id"], "record": r})
    return out


def build_upgrade_candidates(records, season_threshold, series_lookup=None):
    """
    Collapse cutoff-unmet records into search candidates.

    Like build_tv_candidates, but the season tier triggers when >= threshold
    of a season's aired episodes sit below cutoff -- the usual shape when a
    whole season came from one inferior release (seen live: a Netflix show
    filled entirely with HDTV rips instead of WEB-DL). Keys carry an 'up-'
    prefix so upgrade cooldowns never collide with missing-hunt cooldowns.
    """
    per_season = {}
    for r in records:
        per_season.setdefault(
            ((r.get("series") or {}).get("id"), r.get("seasonNumber")), []
        ).append(r)
    out, seen = [], set()
    for r in records:
        ser = r.get("series") or {}
        sid, sn = ser.get("id"), r.get("seasonNumber")
        full = (series_lookup or {}).get(sid) or ser
        total = None
        for sea in full.get("seasons", []):
            if sea.get("seasonNumber") == sn:
                total = (sea.get("statistics") or {}).get("episodeCount") or None
        frac = len(per_season[(sid, sn)]) / float(total) if total else None
        if sid is not None and frac is not None and frac >= season_threshold:
            key = "up-season:%d:%d" % (sid, sn)
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": "season", "key": key, "seriesId": sid,
                        "seasonNumber": sn, "title": ser.get("title", "?")})
        else:
            out.append({"kind": "episode", "key": "up-ep:%d" % r["id"], "record": r})
    return out


def huntable(r):
    """Monitored, and -- for episodes -- belonging to a monitored series.

    Sonarr's wanted/missing can list a monitored episode of an unmonitored
    series; never search those. Radarr records have no 'series' key and are
    judged on their own monitored flag.
    """
    return bool(r.get("monitored", True)) and \
        bool((r.get("series") or {}).get("monitored", True))


def series_pack_candidates(series_list, season_threshold, min_seasons):
    """
    Shows where >= min_seasons monitored seasons are each mostly missing.

    Season-by-season hunting would take such a show weeks; one complete-series
    pack fixes it in a single grab. Specials, unmonitored seasons, and seasons
    with no aired episodes never count. min_seasons <= 0 disables the tier.
    """
    if min_seasons <= 0:
        return []
    out = []
    for s in series_list:
        if not s.get("monitored"):
            continue
        bad = []
        for sea in s.get("seasons", []):
            sn = sea.get("seasonNumber")
            if not sn or not sea.get("monitored", True):
                continue
            frac = season_missing_fraction(s, sn)
            if frac is not None and frac >= season_threshold:
                bad.append(sn)
        if len(bad) >= min_seasons:
            out.append({"kind": "pack", "key": "pack:%d" % s["id"],
                        "series": s, "seasons": sorted(bad)})
    out.sort(key=lambda c: c["series"].get("title", ""))
    return out


def pick_pack_release(releases, missing_seasons, total_episodes, cfg,
                      require_link=False):
    """
    First release (they arrive score-sorted) that would actually solve the
    show. Beyond score_release's filters: enumerated seasons must include
    every mostly-missing season, size-per-episode must clear min_file_mb (a
    single season mislabeled 'complete' fails this), and it needs a usable
    download link. require_link: a magnet is not enough (seedbox watch
    folders need an actual .torrent file).
    """
    for rel in releases:
        if require_link:
            if not rel.get("link"):
                continue
        elif not (rel.get("link") or rel.get("magnet")):
            continue
        if rel["seasons"] is not None and not set(missing_seasons) <= rel["seasons"]:
            continue
        if total_episodes:
            if rel["size"] / float(total_episodes) < cfg.min_file_mb * 1024 * 1024:
                continue
        return rel
    return None


def pack_scan_outcome(planned, have, unmapped, dupes, max_unmapped):
    """
    What to do with a finished pack's scan: 'import', 'done', or 'manual'.

    Conflicts always mean manual. Unmapped files are never imported, so a
    stray one within max_unmapped (e.g. a '523' beyond the season's end)
    costs nothing -- its episode just stays with the normal hunter. A pack
    with nothing importable but its episodes already on disk is DONE, not a
    problem (seen live: a re-scan after a successful import found only the
    already-have files plus the tolerated stray and wrongly parked itself
    for manual attention).
    """
    total = len(planned) + len(have) + len(unmapped)
    if not total or dupes:
        return "manual"
    if len(unmapped) > max_unmapped * total:
        return "manual"
    if planned:
        return "import"
    if have:
        return "done"
    return "manual"


def _mark_pack_done(state, torrent_hash):
    """
    Forget a finished pack's tracking but remember its hash: the torrent
    keeps seeding in the category, and without the done-list adoption would
    re-claim and re-scan it every single run (seen live).
    """
    state.setdefault("packs_done", {})[torrent_hash] = time.time()
    state.get("grabs", {}).pop(torrent_hash, None)
    save_state(state)


def split_already_have(planned):
    """
    Partition the plan into (worth importing, already have).

    A pack file whose every target episode already has a library file is not
    a fill -- importing it could silently replace a better copy (seen live:
    a DVDRip pack vs an existing WEBDL-1080p season 1). Auto-import skips
    those; filling gaps is its whole job, upgrades are Sonarr's.
    """
    keep, have = [], []
    for p in planned:
        eps = p["episodes"]
        if eps and all(e.get("hasFile") for e in eps):
            have.append(p)
        else:
            keep.append(p)
    return keep, have


def candidate_series_id(c):
    if c["kind"] == "season":
        return c["seriesId"]
    if c["kind"] == "pack":
        return c["series"]["id"]
    return (c["record"].get("series") or {}).get("id")


def drop_pack_pending(cands, pending_series_ids):
    """A show with a pack already downloading needs no season/episode searches."""
    return [c for c in cands if candidate_series_id(c) not in pending_series_ids]


def normalize_title(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def match_pack_series(name, series_list, hunted):
    """Match a torrent name to the pack-hunted series it most plausibly is.

    Only series with a 'pack:<id>' entry in the hunt state qualify -- we
    claim strays from our own pack searches, not the user's manual grabs.
    Longest matching title wins ('24: Legacy' beats '24')."""
    norm = normalize_title(name)
    best, best_len = None, 0
    for s in series_list:
        if "pack:%s" % s.get("id") not in hunted:
            continue
        t = normalize_title(s.get("title", ""))
        if t and (norm == t or norm.startswith(t + " ")) and len(t) > best_len:
            best, best_len = s, len(t)
    return best


def pick_adoptions(torrents, grabs, done, series_list, hunted):
    """
    (untracked torrents to adopt as [(hash, name, series)], stale done-hashes).

    Tracked torrents and completed packs are never adopted -- without the
    done-list, a finished pack still seeding would be re-adopted and
    re-scanned every run (seen live). Done-hashes whose torrents have left
    the category are stale and can be forgotten.
    """
    adopt, seen = [], set()
    for t in torrents:
        h = t.get("hash")
        seen.add(h)
        if h in grabs or h in done:
            continue
        s = match_pack_series(t.get("name", ""), series_list, hunted)
        if s is not None:
            adopt.append((h, t.get("name", ""), s))
    return adopt, [h for h in done if h not in seen]


def adopt_untracked_packs(cfg, state, series_list, hunted, dry):
    """
    Claim category torrents that belong to a pack hunt but aren't tracked.

    Seen live: qBittorrent fetched a .torrent through the VPN so slowly that
    it surfaced only after grab_release stopped polling -- downloaded fine,
    but untracked, so it would never have auto-imported.
    """
    grabs = state.setdefault("grabs", {})
    done = state.setdefault("packs_done", {})
    adopt, stale = pick_adoptions(QBit(cfg).torrents(cfg.qb_category),
                                  grabs, done, series_list, hunted)
    changed = False
    for h, name, s in adopt:
        if dry:
            print("  would adopt untracked torrent as %s's pack: %s"
                  % (s["title"], name[:60]))
            continue
        grabs[h] = {
            "seriesId": s["id"],
            "seriesTitle": s["title"],
            "release": name,
            "grabbedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "auto": True,
            "adopted": True,
        }
        changed = True
        print("  adopted untracked torrent as %s's pack: %s"
              % (s["title"], name[:60]))
    if not dry and stale:
        for h in stale:
            del done[h]
        changed = True
    if changed:
        save_state(state)


def auto_import_packs(cfg, sonarr, state, dry):
    """
    Finish the loop for auto-grabbed packs: when one has completed downloading,
    scan it and import ONLY if the mapping is perfect -- every file mapped by
    Sonarr's parser or an exact SxxEyy match, zero unmapped, zero conflicts.
    Anything less is reported and left for a human `import` run.
    """
    grabs = state.get("grabs") or {}
    auto = {h: m for h, m in grabs.items() if m.get("auto")}
    if not auto:
        return
    now = time.time()
    torrents = {t["hash"]: t for t in QBit(cfg).torrents(cfg.qb_category)}
    for h, meta in sorted(auto.items(), key=lambda kv: kv[1].get("seriesTitle", "")):
        title = meta.get("seriesTitle", "?")
        t = torrents.get(h)
        if t is None:
            print("  pack not in qBittorrent (removed?): %s -- check `status`" % title)
            continue
        if not is_complete(t):
            print("  pack still downloading: %s (%.0f%%)"
                  % (title, t.get("progress", 0) * 100))
            continue
        if meta.get("needsManual") and now - meta.get("needsManualAt", 0) < 86400:
            print("  pack awaiting manual import: %s (%s) -- run: seriespack.py "
                  "import \"%s\"  (auto-rechecked daily)"
                  % (title, meta["needsManual"], title))
            continue

        raw = t.get("content_path") or os.path.join(t.get("save_path", ""), t.get("name", ""))
        _scan_and_import_pack(cfg, sonarr, state, h, meta,
                              translate_path(raw, cfg.path_maps), dry)


def _scan_and_import_pack(cfg, sonarr, state, key, meta, folder, dry):
    """The shared back half of pack automation: scan a finished pack's
    folder, apply the gate, and import / close out / park for manual."""
    title = meta.get("seriesTitle", "?")
    if dry:
        print("  would scan finished pack: %s (%s)" % (title, folder))
        return
    print("  pack finished: %s -- scanning %s" % (title, folder))
    try:
        items = sonarr.manual_import(folder)
    except requests.HTTPError as exc:
        print("    scan failed (%s); will retry next run" % exc)
        return

    ep_idx = episode_lookup(sonarr, meta["seriesId"])
    planned, skipped, unmapped = map_files(items, meta["seriesId"], ep_idx, cfg)
    planned, have = split_already_have(planned)
    if have:
        print("    %d file(s) skipped: their episodes already have library "
              "files (auto-import never downgrades)" % len(have))
    dupes = check_conflicts(planned)
    outcome = pack_scan_outcome(planned, have, unmapped, dupes,
                                cfg.hunt_pack_max_unmapped)
    if outcome == "done":
        print("    nothing to import (all episodes already in the library); done.")
        _mark_pack_done(state, key)
        return
    if outcome == "manual":
        meta["needsManual"] = ("%d unmapped, %d conflicts"
                               % (len(unmapped), len(dupes)))
        meta["needsManualAt"] = time.time()
        save_state(state)
        print("    mapping not clean (%d mapped, %d unmapped, %d conflicts, %d skipped)"
              % (len(planned), len(unmapped), len(dupes), len(skipped)))
        print("    left for you: seriespack.py import \"%s\"" % title)
        if planned or unmapped:
            print_plan(planned, skipped, unmapped)
        return

    if unmapped:
        print("    %d unmapped file(s) tolerated -- NOT imported; their "
              "episodes stay with the episode hunter" % len(unmapped))
    cmd = sonarr.command({
        "name": "ManualImport",
        "files": [p["payload"] for p in planned],
        "importMode": cfg.import_mode,
    })
    result = sonarr.wait_command(sonarr_cmd_id(cmd))
    if result.get("status") == "completed":
        print("    imported %d files (mode=%s)." % (len(planned), cfg.import_mode))
        _mark_pack_done(state, key)
    else:
        print("    import %s: %s -- will retry next run"
              % (result.get("status"),
                 result.get("exception") or result.get("message") or "(none)"))


def stale_tmp_uploads(entries, now, max_age=86400.0):
    """
    Leftover '.<name>.tmp' torrent uploads (a crashed upload never renamed;
    seen live). entries = [(name, mtime)]. Only dot-tmp files old enough to
    be certainly dead are returned -- a fresh one may be mid-upload.
    """
    return [n for n, mt in entries
            if n.startswith(".") and n.endswith(".tmp") and now - mt > max_age]


def seedbox_grab(cfg, state, series, rel):
    """
    Route a pack grab through the seedbox instead of qBittorrent: fetch the
    .torrent from Jackett and drop it into the rwatch subfolder over SFTP.
    rutorrent's AutoLabel/AutoMove take it from there, sync delivers the
    finished item into pack_import_dir, and auto-import finishes the job.
    Returns the torrent's item name, or None if the grab didn't happen.
    """
    try:
        r = requests.get(rel["link"], timeout=120)
        r.raise_for_status()
    except requests.RequestException as exc:
        print("    couldn't fetch the .torrent (%s) -- skipping" % exc)
        return None
    name = torrent_name(r.content)
    if not name:
        print("    link didn't return a valid .torrent (magnet-only indexer?) -- skipping")
        return None

    try:
        client, sftp = seedbox_connect(cfg)
    except ImportError:
        print("    seedbox delivery needs paramiko (pip install paramiko) -- skipping")
        return None
    except Exception as exc:
        print("    seedbox connection failed (%s) -- skipping" % exc)
        return None
    try:
        watch = cfg.hunt_pack_watch.rstrip("/")
        try:
            sftp.stat(watch)
        except IOError:
            sftp.mkdir(watch)
        try:
            entries = [(a.filename, a.st_mtime or 0)
                       for a in sftp.listdir_attr(watch)]
            for n in stale_tmp_uploads(entries, time.time()):
                sftp.remove(watch + "/" + n)
                print("    removed stale upload remnant: %s" % n[:60])
        except IOError:
            pass
        fname = re.sub(r'[\\/:*?"<>|]', "_", name)[:180] + ".torrent"
        tmp = watch + "/." + fname + ".tmp"
        with sftp.open(tmp, "wb") as fh:
            fh.write(r.content)
        # rename so rtorrent's watch never sees a partial file
        sftp.rename(tmp, watch + "/" + fname)
    except Exception as exc:
        print("    seedbox upload failed (%s) -- skipping" % exc)
        return None
    finally:
        client.close()

    state.setdefault("grabs", {})["sb:" + name] = {
        "seriesId": series["id"],
        "seriesTitle": series["title"],
        "release": rel["title"],
        "name": name,
        "delivery": "seedbox",
        "grabbedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "grabbedAtTs": time.time(),
        "auto": True,
    }
    save_state(state)
    print("    dropped into seedbox watch (%s): %s" % (cfg.hunt_pack_watch, name))
    return name


def grab_age_days(meta, now):
    """Days since a grab was made, or None if the timestamp is unusable."""
    ts = meta.get("grabbedAtTs")
    if ts is None:
        try:
            ts = time.mktime(time.strptime(meta.get("grabbedAt", ""),
                                           "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            return None
    return (now - ts) / 86400.0


def adopt_delivered_packs(cfg, state, series_list, hunted, dry):
    """
    Claim delivered pack folders nothing tracks. Without this, an item in
    pack_import_dir whose grab record was lost (seen live: torrents wiped
    on the seedbox alongside surviving ones) would sit unimported until
    pruning threw it away.
    """
    if not cfg.hunt_pack_import_dir or not os.path.isdir(cfg.hunt_pack_import_dir):
        return
    grabs = state.setdefault("grabs", {})
    done = state.setdefault("packs_done", {})
    tracked = {m.get("name") for m in grabs.values() if m.get("name")}
    adopted = False
    for n in sorted(os.listdir(cfg.hunt_pack_import_dir)):
        if n in tracked or ("sb:" + n) in done:
            continue
        s = match_pack_series(n, series_list, hunted)
        if s is None:
            continue
        if dry:
            print("  would adopt delivered pack as %s's: %s" % (s["title"], n[:60]))
            continue
        grabs["sb:" + n] = {
            "seriesId": s["id"],
            "seriesTitle": s["title"],
            "release": n,
            "name": n,
            "delivery": "seedbox",
            "grabbedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "grabbedAtTs": time.time(),
            "auto": True,
            "adopted": True,
        }
        adopted = True
        print("  adopted delivered pack as %s's: %s" % (s["title"], n[:60]))
    if adopted:
        save_state(state)


def auto_import_seedbox_packs(cfg, sonarr, state, dry):
    """
    Finish seedbox-routed packs. Sync only ever delivers complete, verified
    items atomically, so 'the item exists in pack_import_dir' IS the
    finished-downloading signal -- no torrent client to ask.
    """
    grabs = state.get("grabs") or {}
    mine = {k: m for k, m in grabs.items() if m.get("delivery") == "seedbox"}
    if not mine:
        return
    if not cfg.hunt_pack_import_dir:
        print("  [!] seedbox pack(s) pending but [hunt] pack_import_dir is not set")
        return
    now = time.time()
    for k, meta in sorted(mine.items(), key=lambda kv: kv[1].get("seriesTitle", "")):
        title = meta.get("seriesTitle", "?")
        path = os.path.join(cfg.hunt_pack_import_dir, meta.get("name", ""))
        if not os.path.exists(path):
            age = grab_age_days(meta, now)
            if age is not None and cfg.hunt_pack_grab_days > 0 \
                    and age > cfg.hunt_pack_grab_days:
                print("  pack never arrived after %.0fd (deleted on the "
                      "seedbox?) -- giving up: %s" % (age, title))
                if not dry:
                    grabs.pop(k, None)
                    save_state(state)
                continue
            print("  pack in transit (seedbox -> sync): %s" % title)
            continue
        if meta.get("needsManual") and now - meta.get("needsManualAt", 0) < 86400:
            print("  pack awaiting manual import: %s (%s) -- run: seriespack.py "
                  "import \"%s\" --folder \"%s\"  (auto-rechecked daily)"
                  % (title, meta["needsManual"], title,
                     translate_path(path, cfg.path_maps)))
            continue
        _scan_and_import_pack(cfg, sonarr, state, k, meta,
                              translate_path(path, cfg.path_maps), dry)


def _gather_missing(client, params, hunted, now, limit, cooldown_days, prefix,
                    pager=None):
    """Page a wanted list (local API, cheap) until `limit` candidates found."""
    pager = pager or client.missing_page
    allrecs, page, total = [], 1, None
    while True:
        data = pager(page, **params)
        recs = [r for r in data.get("records", []) if huntable(r)]
        allrecs.extend(recs)
        total = data.get("totalRecords", 0)
        picked = pick_hunt_candidates(allrecs, hunted, now, limit, cooldown_days, prefix)
        if len(picked) >= limit or page * 200 >= total or not data.get("records"):
            return picked, total
        page += 1


def cmd_hunt(cfg, args):
    do_tv = args.tv or not args.movies
    do_movies = args.movies or not args.tv
    state = load_state()
    hunted = state.setdefault("hunted", {})
    now = time.time()

    def run(client, prefix, limit, params, command_name, ids_key, describe,
            pager=None, noun="missing"):
        picked, total = _gather_missing(
            client, params, hunted, now, limit, cfg.hunt_cooldown_days, prefix,
            pager=pager)
        print("%d %s; %d eligible this run (cooldown %gd):"
              % (total, noun, len(picked), cfg.hunt_cooldown_days))
        if not picked:
            print("  nothing outside the cooldown window.")
        for i, r in enumerate(picked):
            if args.dry:
                print("  would search: %s" % describe(r))
                continue
            client.command({"name": command_name, ids_key: [r["id"]]})
            hunted["%s:%d" % (prefix, r["id"])] = now
            print("  searching: %s" % describe(r))
            if i < len(picked) - 1:
                time.sleep(cfg.hunt_delay)

    def search_tv_candidates(sonarr, picked):
        for i, c in enumerate(picked):
            if c["kind"] == "season":
                desc = "%s season %d (whole season -- SeasonSearch)" % (
                    c["title"], c["seasonNumber"])
                payload = {"name": "SeasonSearch", "seriesId": c["seriesId"],
                           "seasonNumber": c["seasonNumber"]}
            else:
                e = c["record"]
                desc = "%s S%02dE%02d %s" % (
                    (e.get("series") or {}).get("title", "?"),
                    e["seasonNumber"], e["episodeNumber"], e.get("title", ""))
                payload = {"name": "EpisodeSearch", "episodeIds": [e["id"]]}
            if args.dry:
                print("  would search: %s" % desc)
                continue
            sonarr.command(payload)
            hunted[c["key"]] = now
            print("  searching: %s" % desc)
            if i < len(picked) - 1:
                time.sleep(cfg.hunt_delay)

    if do_tv:
        print("== TV (Sonarr) ==")
        sonarr = Sonarr(cfg)
        limit = args.max or cfg.hunt_tv_batch
        params = dict(includeSeries="true", sortKey="airDateUtc",
                      sortDirection="descending", monitored="true")
        series_lookup = {s["id"]: s for s in sonarr.all_series()}

        # -- pack tier: finish grabs that completed, start new ones ----------
        # A qBittorrent/Jackett/seedbox hiccup must not kill episode hunting.
        seedbox_packs = cfg.hunt_pack_delivery == "seedbox"
        try:
            if seedbox_packs:
                adopt_delivered_packs(cfg, state, list(series_lookup.values()),
                                      hunted, args.dry)
                auto_import_seedbox_packs(cfg, sonarr, state, args.dry)
            elif cfg.qb_url:
                adopt_untracked_packs(cfg, state, list(series_lookup.values()),
                                      hunted, args.dry)
                auto_import_packs(cfg, sonarr, state, args.dry)
        except (requests.RequestException, OSError) as exc:
            print("  pack import check failed (%s); continuing" % exc)
        pending = {m.get("seriesId") for m in (state.get("grabs") or {}).values()}
        packs_possible = seedbox_packs or bool(cfg.qb_url)
        if cfg.hunt_pack_min_seasons > 0 and not args.no_packs and not packs_possible:
            print("  [!] pack tier off: pack_delivery=qbittorrent but no "
                  "[qbittorrent] url (set pack_delivery = seedbox?)")
        in_flight = len(state.get("grabs") or {})
        if cfg.hunt_pack_min_seasons > 0 and not args.no_packs and packs_possible \
                and cfg.hunt_pack_pending_max > 0 \
                and in_flight >= cfg.hunt_pack_pending_max:
            print("  pack grabs paused: %d already in flight (pack_pending_max %d)"
                  % (in_flight, cfg.hunt_pack_pending_max))
        elif cfg.hunt_pack_min_seasons > 0 and not args.no_packs and packs_possible:
            pack_cands = drop_pack_pending(
                series_pack_candidates(series_lookup.values(),
                                       cfg.hunt_season_threshold,
                                       cfg.hunt_pack_min_seasons),
                pending)
            for c in pick_by_key(pack_cands, hunted, now,
                                 cfg.hunt_pack_batch, cfg.hunt_cooldown_days):
                s = c["series"]
                desc = "%s (seasons %s mostly missing)" % (
                    s["title"], ",".join(str(n) for n in c["seasons"]))
                if args.dry:
                    print("  would hunt a series pack: %s" % desc)
                    continue
                print("  hunting a series pack: %s" % desc)
                # cooldown whether or not a pack exists: if the trackers have
                # none now, they won't in 30 minutes either
                hunted[c["key"]] = now
                try:
                    rel = pick_pack_release(
                        find_releases(cfg, s, verbose=False), c["seasons"],
                        (s.get("statistics") or {}).get("episodeCount", 0), cfg,
                        require_link=seedbox_packs)
                    if rel is None:
                        print("    no qualifying pack -- season hunting continues instead")
                        continue
                    print("    grabbing: %s" % rel["title"])
                    print("    %s | %s | %d seeders | %s"
                          % (rel["tracker"], human_size(rel["size"]),
                             rel["seeders"], rel.get("coverage", "")))
                    if seedbox_packs:
                        if seedbox_grab(cfg, state, s, rel):
                            pending.add(s["id"])
                    elif grab_release(cfg, state, s, rel, auto=True):
                        pending.add(s["id"])
                    else:
                        print("    qBittorrent hasn't reported the new torrent yet -- "
                              "the next run will adopt it if it shows up")
                except requests.RequestException as exc:
                    print("    pack search/grab failed (%s); continuing" % exc)

        allrecs, page, picked, total = [], 1, [], 0
        while True:
            data = sonarr.missing_page(page, **params)
            allrecs.extend(r for r in data.get("records", []) if huntable(r))
            total = data.get("totalRecords", 0)
            picked = pick_by_key(
                drop_pack_pending(
                    build_tv_candidates(allrecs, cfg.hunt_season_threshold,
                                        series_lookup),
                    pending),
                hunted, now, limit, cfg.hunt_cooldown_days)
            if len(picked) >= limit or page * 200 >= total or not data.get("records"):
                break
            page += 1
        print("%d missing episodes; %d searches this run (cooldown %gd):"
              % (total, len(picked), cfg.hunt_cooldown_days))
        if not picked:
            print("  nothing outside the cooldown window.")
        search_tv_candidates(sonarr, picked)

        # -- upgrades: below-cutoff episodes. Sonarr only ever grabs a strict
        # quality improvement on these searches, so there is no low-quality
        # downside -- just indexer load, hence the separate small batch.
        if cfg.hunt_upgrade_tv_batch > 0 and not args.no_upgrades:
            allup, page, upicked, utotal = [], 1, [], 0
            while True:
                data = sonarr.cutoff_page(page, **params)
                allup.extend(r for r in data.get("records", []) if huntable(r))
                utotal = data.get("totalRecords", 0)
                upicked = pick_by_key(
                    build_upgrade_candidates(allup, cfg.hunt_season_threshold,
                                             series_lookup),
                    hunted, now, cfg.hunt_upgrade_tv_batch,
                    cfg.hunt_cooldown_days)
                if len(upicked) >= cfg.hunt_upgrade_tv_batch or \
                        page * 200 >= utotal or not data.get("records"):
                    break
                page += 1
            print("%d episodes below quality cutoff; %d upgrade searches this run:"
                  % (utotal, len(upicked)))
            if not upicked:
                print("  nothing outside the cooldown window.")
            search_tv_candidates(sonarr, upicked)

    if do_movies:
        print("\n== Movies (Radarr) ==")
        if not cfg.radarr_url:
            print("  no [radarr] section in the config -- skipping movies.")
        else:
            radarr = Radarr(cfg)
            describe_movie = lambda m: "%s (%s)" % (m.get("title", "?"),
                                                    m.get("year", "?"))
            run(radarr, "movie", args.max or cfg.hunt_movie_batch,
                dict(sortKey="title", sortDirection="ascending"),
                "MoviesSearch", "movieIds", describe_movie)
            if cfg.hunt_upgrade_movie_batch > 0 and not args.no_upgrades:
                run(radarr, "up-movie", cfg.hunt_upgrade_movie_batch,
                    dict(sortKey="title", sortDirection="ascending"),
                    "MoviesSearch", "movieIds", describe_movie,
                    pager=radarr.cutoff_page, noun="movies below quality cutoff")

    if not args.dry:
        # keep the state file from growing forever
        cutoff = now - cfg.hunt_cooldown_days * 86400.0 * 8
        for k in [k for k, v in hunted.items() if v < cutoff]:
            del hunted[k]
        save_state(state)


def cmd_import(cfg, args):
    sonarr = Sonarr(cfg)
    state = load_state()

    if args.folder:
        series = pick_series(sonarr, args.query)
        folder = args.folder
        torrent_hash = None
    else:
        series = pick_series(sonarr, args.query)
        qb = QBit(cfg)
        candidates = []
        for t in qb.torrents(cfg.qb_category):
            meta = state["grabs"].get(t["hash"])
            if meta and meta["seriesId"] == series["id"] and is_complete(t):
                candidates.append(t)
        if not candidates:
            sys.exit(
                "No completed download in category '%s' is tracked for %s.\n"
                "Check `status`, or pass --folder to import a path directly."
                % (cfg.qb_category, series["title"])
            )
        t = candidates[0]
        torrent_hash = t["hash"]
        raw = t.get("content_path") or os.path.join(t.get("save_path", ""), t.get("name", ""))
        folder = translate_path(raw, cfg.path_maps)
        if raw != folder:
            print("Path translated for Sonarr:\n  qbit:   %s\n  sonarr: %s" % (raw, folder))

    print("\nSeries: %s (id %d)" % (series["title"], series["id"]))
    print("Folder: %s" % folder)

    try:
        items = sonarr.manual_import(folder)
    except requests.HTTPError as exc:
        sys.exit(
            "Sonarr couldn't scan that folder (%s).\n"
            "Usually this means the path isn't reachable from the Sonarr VM. "
            "Check your [paths] mappings." % exc
        )

    if not items:
        sys.exit("Sonarr found no importable files there. Is the path right, and readable by Sonarr?")

    ep_idx = episode_lookup(sonarr, series["id"])
    planned, skipped, unmapped = map_files(items, series["id"], ep_idx, cfg)
    planned = apply_renumber(planned, unmapped,
                             parse_remap_args(args.remap),
                             parse_offset_args(args.offset), ep_idx)
    planned = apply_skips(planned, skipped, parse_skip_args(args.skip))
    print_plan(planned, skipped, unmapped)

    if not planned:
        sys.exit("\nNothing mappable. Aborting.")

    dupes = check_conflicts(planned)
    if dupes:
        print("\n[!] Two files claim the same episode:")
        for a, b in dupes[:10]:
            print("    %s  <->  %s" % (a[:45], b[:45]))

    total = len(planned) + len(unmapped)
    ratio = len(unmapped) / float(total) if total else 0
    print("\n%d files to import, %d unmapped (%.0f%%), %d skipped."
          % (len(planned), len(unmapped), ratio * 100, len(skipped)))

    if not args.confirm:
        print("\nDRY RUN -- nothing was imported. Re-run with --confirm to commit.")
        return

    if ratio > args.max_unmapped and not args.force:
        sys.exit(
            "\nAborting: %.0f%% of files are unmapped (limit %.0f%%). "
            "Fix the mapping or pass --force." % (ratio * 100, args.max_unmapped * 100)
        )
    if dupes and not args.force:
        sys.exit("\nAborting: duplicate episode mappings. Pass --force to override.")

    payload = {
        "name": "ManualImport",
        "files": [p["payload"] for p in planned],
        "importMode": cfg.import_mode,
    }
    cmd = sonarr.command(payload)
    print("\nSubmitted ManualImport (command %s), mode=%s. Waiting..."
          % (cmd.get("id"), cfg.import_mode))
    result = sonarr.wait_command(sonarr_cmd_id(cmd))
    print("Result: %s" % result.get("status"))
    if result.get("status") == "completed" and torrent_hash:
        # done-list keeps hunt's adoption from re-claiming the seeding torrent
        _mark_pack_done(state, torrent_hash)
    if result.get("status") == "failed":
        print("Message: %s" % (result.get("exception") or result.get("message") or "(none)"))


def sonarr_cmd_id(cmd):
    try:
        return int(cmd.get("id"))
    except (TypeError, ValueError):
        sys.exit("Sonarr didn't return a command id: %r" % cmd)


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def main():
    safe_console()
    ap = argparse.ArgumentParser(
        description="Grab complete-series torrents and import them into Sonarr.",
    )
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="path to seriespack.ini")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("series", help="list Sonarr series and their ids")
    p.add_argument("query", nargs="?", default="")
    p.set_defaults(fn=cmd_series)

    p = sub.add_parser("search", help="find multi-season releases for a series")
    p.add_argument("query")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("grab", help="send a release to qBittorrent")
    p.add_argument("query")
    p.add_argument("--pick", type=int, default=None, help="index from the search table")
    p.add_argument("--auto", action="store_true", help="take the top-scored release")
    p.set_defaults(fn=cmd_grab)

    p = sub.add_parser("status", help="show tracked downloads")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("sync", help="pull completed seedbox items to the NAS (staging + verified atomic move)")
    p.add_argument("--dry", action="store_true", help="show what would transfer, transfer nothing")
    p.add_argument("--limit", type=int, default=None, help="max items to pull per mapping this run")
    p.add_argument("--no-prune", action="store_true",
                   help="skip deleting old delivered items no *arr queue references")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("clean", help="flag failed/stalled downloads; --remove deletes them (Cleanuparr-style)")
    p.add_argument("--category", default=None,
                   help="qBittorrent category to inspect (default: the seriespacks category)")
    p.add_argument("--remove", action="store_true",
                   help="delete flagged torrents AND their data (default is a dry run)")
    p.add_argument("--seeded", action="store_true",
                   help="also flag finished seeds that met [clean] seed_ratio or seed_hours")
    p.add_argument("--orphans", action="store_true",
                   help="also flag downloads nothing references (hardlink check; needs "
                        "filesystem access to the download paths)")
    p.add_argument("--research", action="store_true",
                   help="with --remove: trigger Sonarr/Radarr replacement searches "
                        "for whatever the removed torrents contained")
    p.add_argument("--queues", action="store_true",
                   help="also clear *arr queue items stuck unable to import "
                        "(> [clean] queue_stuck_days); acts only with --remove")
    p.set_defaults(fn=cmd_clean)

    p = sub.add_parser("hunt", help="trigger searches for a few missing episodes/movies (Huntarr-style)")
    p.add_argument("--tv", action="store_true", help="only hunt Sonarr episodes")
    p.add_argument("--movies", action="store_true", help="only hunt Radarr movies")
    p.add_argument("--max", type=int, default=None, help="override the batch size for this run")
    p.add_argument("--dry", action="store_true", help="show what would be searched, search nothing")
    p.add_argument("--no-packs", action="store_true",
                   help="skip the series-pack tier this run (no Jackett search/grab; "
                        "finished pack imports still complete)")
    p.add_argument("--no-upgrades", action="store_true",
                   help="skip cutoff-unmet (quality upgrade) searches this run")
    p.set_defaults(fn=cmd_hunt)

    p = sub.add_parser("import", help="map files to episodes and import")
    p.add_argument("query")
    p.add_argument("--folder", help="import this path instead of a tracked download")
    p.add_argument("--confirm", action="store_true", help="actually import (default is a dry run)")
    p.add_argument("--skip", action="append", default=[], metavar="SxxEyy",
                   help="exclude this episode's file (repeatable), e.g. --skip S03E02")
    p.add_argument("--remap", action="append", default=[], metavar="SxxEyy=SxxEyy",
                   help="file mapped to the left episode goes to the right one instead "
                        "(repeatable); overrides Sonarr's own mapping")
    p.add_argument("--offset", action="append", default=[], metavar="Sxx:+N",
                   help="shift all episode numbers in a season by N (repeatable), e.g. S02:+1")
    p.add_argument("--force", action="store_true", help="override safety checks")
    p.add_argument("--max-unmapped", type=float, default=0.15,
                   help="abort if more than this fraction is unmapped (default 0.15)")
    p.set_defaults(fn=cmd_import)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return

    cfg = Config(args.config)
    try:
        args.fn(cfg, args)
    except requests.HTTPError as exc:
        sys.exit("API error: %s" % exc)
    except requests.ConnectionError as exc:
        sys.exit("Could not reach a service: %s" % exc)
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")


if __name__ == "__main__":
    main()
