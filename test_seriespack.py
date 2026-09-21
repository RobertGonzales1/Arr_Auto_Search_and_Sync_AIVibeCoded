#!/usr/bin/env python3
"""
Offline tests for seriespack.py. No network, no config needed.

    python test_seriespack.py

Covers the parts where a silent mistake would put episodes in the wrong season:
season-range detection, release filtering, cross-VM path translation, the
filename -> episode regex, conflict detection, and the dry-run gate.

When a real release name or path trips the script up, add it here first.
"""

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seriespack as sp  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILS.append(label)
    print("  %s %-52s got=%r" % ("ok  " if ok else "FAIL", label[:52], got))


def section(name):
    print("\n== %s ==" % name)


class Cfg(object):
    """Minimal stand-in for seriespack.Config."""
    min_seeders = 3
    max_size_gb = 400
    min_file_mb = 50
    quality_order = ["1080p", "720p", "2160p"]


# ---------------------------------------------------------------- season names

section("season detection")
for title, want in [
    ("Babylon.5.S01-S05.1080p.BluRay.x264-GROUP", (set(range(1, 6)), "range")),
    ("The.Wire.Complete.Series.1080p.WEB-DL", (None, "complete")),
    ("Alias Seasons 1-5 DVDRip", (set(range(1, 6)), "range")),
    ("Show.S01.S02.S03.S04.720p", ({1, 2, 3, 4}, "tokens")),
    ("Frasier S01 - S11 COMPLETE 720p", (set(range(1, 12)), "range")),
    ("Firefly.The.Complete.Collection.1080p", (None, "complete")),
    ("Show.All.Seasons.1080p", (None, "complete")),
    # must NOT be treated as multi-season
    ("Breaking.Bad.S03.1080p.BluRay", ({3}, "single")),
    ("Breaking.Bad.S03E07.1080p.WEB", (set(), "single")),
    ("Some.Show.2019.1080p.WEB", (set(), "unknown")),
]:
    check(title, sp.detect_seasons(title), want)


# ------------------------------------------------------------------ filtering

section("releases that must be rejected")
for title, seeders, size_gb, why in [
    ("Breaking.Bad.S03E07.1080p.WEB", 50, 20, "single episode"),
    ("Breaking.Bad.S03.1080p.BluRay", 50, 20, "single season"),
    ("Random.Movie.2020.1080p", 50, 20, "not a series"),
    ("The.Wire.S01-S05.1080p", 1, 10, "too few seeders"),
    ("The.Wire.S01-S05.2160p", 50, 900, "absurd size"),
]:
    rel = {"title": title, "seeders": seeders, "size": int(size_gb * 1024 ** 3)}
    rel["seasons"], rel["kind"] = sp.detect_seasons(title)
    rel["quality"] = sp.quality_of(title)
    check("reject (%s): %s" % (why, title), sp.score_release(rel, {1, 2, 3}, Cfg), None)

section("a good release survives and scores")
rel = {"title": "The.Wire.S01-S05.COMPLETE.1080p.BluRay", "seeders": 80,
       "size": 150 * 1024 ** 3}
rel["seasons"], rel["kind"] = sp.detect_seasons(rel["title"])
rel["quality"] = sp.quality_of(rel["title"])
check("scored", sp.score_release(rel, {1, 2, 3, 4, 5}, Cfg) is not None, True)
check("coverage reported", rel["coverage"], "5/5 seasons")
check("quality detected", rel["quality"], "1080p")


# ----------------------------------------------------------- config + paths

section("config parsing (drive letters and % must survive)")
INI = """[sonarr]
url = http://sonarr-vm:8989
api_key = abc123
[jackett]
url = http://jackett-vm:9117
api_key = def456
[qbittorrent]
url = http://qbit-vm:8080
username = admin
password = p@ss%word
[paths]
D:\\Torrents = T:\\(0)Media\\Torrents
E:\\dl\\tv = \\\\QBITVM\\dl\\tv
[preferences]
min_seeders = 3
"""
tmp = os.path.join(tempfile.gettempdir(), "_seriespack_test.ini")
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(INI)
cfg = sp.Config(tmp)
check("drive-letter key intact", cfg.path_maps[0][0], "D:\\Torrents")
check("parens in value intact", cfg.path_maps[0][1], "T:\\(0)Media\\Torrents")
check("percent in password intact", cfg.qb_pass, "p@ss%word")
os.remove(tmp)

section("path translation (qBittorrent VM -> Sonarr VM)")
MAPS = [("D:\\Torrents", "T:\\(0)Media\\Torrents"),
        ("E:\\dl\\tv", "\\\\QBITVM\\dl\\tv")]
for src, want, why in [
    ("D:\\Torrents\\The.Wire.S01-S05", "T:\\(0)Media\\Torrents\\The.Wire.S01-S05", "subfolder"),
    ("D:\\Torrents\\Show\\Season 1\\ep.mkv", "T:\\(0)Media\\Torrents\\Show\\Season 1\\ep.mkv", "nested + spaces"),
    ("D:\\Torrents", "T:\\(0)Media\\Torrents", "exact match"),
    ("d:\\torrents\\x", "T:\\(0)Media\\Torrents\\x", "case insensitive"),
    ("D:/Torrents/x", "T:\\(0)Media\\Torrents\\x", "forward slashes"),
    ("E:\\dl\\tv\\Pack", "\\\\QBITVM\\dl\\tv\\Pack", "UNC destination"),
    ("D:\\Torrents2\\x", "D:\\Torrents2\\x", "no prefix bleed"),
    ("F:\\other\\x", "F:\\other\\x", "unmapped passes through"),
]:
    check(why, sp.translate_path(src, MAPS), want)

check("linux destination",
      sp.translate_path("D:\\downloads\\a\\b", [("D:\\downloads", "/mnt/dl")]),
      "/mnt/dl/a/b")


# ------------------------------------------------------------ episode parsing

section("filename -> episode")


def parse(name):
    m = sp.FILE_EP_RE.search(name)
    if m:
        last = int(m.group(3)) if m.group(3) else int(m.group(2))
        return (int(m.group(1)), int(m.group(2)), last)
    m = sp.ALT_EP_RE.search(name)
    return (int(m.group(1)), int(m.group(2)), int(m.group(2))) if m else None


for name, want in [
    ("Show.S02E05.1080p.mkv", (2, 5, 5)),
    ("show.s02e05.mkv", (2, 5, 5)),
    ("Show.S01E01E02.mkv", (1, 1, 2)),
    ("Show.S01E01-E02.mkv", (1, 1, 2)),
    ("Show - 2x07 - Title.mkv", (2, 7, 7)),
    ("Show.S1E3.mkv", (1, 3, 3)),
    ("Show.Extras.Featurette.mkv", None),
    ("Show.Season.720p.mkv", None),
]:
    check(name, parse(name), want)


# ---------------------------------------------------------------- mapping

section("map_files")
EP_IDX = {
    (1, 1): {"id": 101, "seasonNumber": 1, "episodeNumber": 1},
    (1, 2): {"id": 102, "seasonNumber": 1, "episodeNumber": 2},
    (2, 1): {"id": 201, "seasonNumber": 2, "episodeNumber": 1},
}
ITEMS = [
    {"path": "/x/a.mkv", "relativePath": "Show.S01E01.mkv", "size": 2 * 1024 ** 3,
     "episodes": [{"id": 101, "seasonNumber": 1, "episodeNumber": 1}], "quality": {"q": 1}},
    {"path": "/x/b.mkv", "relativePath": "Show.S01E02.mkv", "size": 2 * 1024 ** 3,
     "episodes": [], "quality": {"q": 1}},
    {"path": "/x/sample.mkv", "relativePath": "sample.mkv", "size": 2 * 1024 ** 3,
     "episodes": []},
    {"path": "/x/tiny.mkv", "relativePath": "Show.S02E01.mkv", "size": 1024,
     "episodes": []},
    {"path": "/x/junk.mkv", "relativePath": "Behind.The.Scenes.mkv", "size": 2 * 1024 ** 3,
     "episodes": [], "rejections": [{"reason": "Unable to parse"}]},
    # Since the scan runs without a seriesId filter, Sonarr may attribute a
    # stray file to some other show entirely. Never import those, and never
    # regex-guess them into OUR series -- even when the name would match.
    {"path": "/x/other.mkv", "relativePath": "Other.Show.S01E02.mkv", "size": 2 * 1024 ** 3,
     "episodes": [{"id": 9901, "seasonNumber": 1, "episodeNumber": 2}],
     "series": {"id": 999, "title": "Other Show"}, "quality": {"q": 1}},
    {"path": "/x/other2.mkv", "relativePath": "Other.Show.S02E01.mkv", "size": 2 * 1024 ** 3,
     "episodes": [], "series": {"id": 999, "title": "Other Show"}},
]
planned, skipped, unmapped = sp.map_files(ITEMS, 7, EP_IDX, Cfg)
check("planned count", len(planned), 2)
check("sonarr-mapped file", planned[0]["source"], "sonarr")
check("regex-mapped file", planned[1]["source"], "regex")
check("regex resolved episode id", planned[1]["payload"]["episodeIds"], [102])
check("seriesId on payload", planned[1]["payload"]["seriesId"], 7)
# Behind.The.Scenes counts as an extra now (skipped, not unmapped)
check("sample + tiny + extra skipped", len(skipped), 3)
check("foreign-series files left unmapped", len(unmapped), 2)

section("map_files accepts matching / absent series on items")
OK_ITEMS = [
    {"path": "/x/a.mkv", "relativePath": "Show.S01E01.mkv", "size": 2 * 1024 ** 3,
     "episodes": [{"id": 101, "seasonNumber": 1, "episodeNumber": 1}],
     "series": {"id": 7, "title": "Show"}, "quality": {"q": 1}},
]
planned2, _, unmapped2 = sp.map_files(OK_ITEMS, 7, EP_IDX, Cfg)
check("matching series imported", len(planned2), 1)
check("nothing unmapped", len(unmapped2), 0)

section("map_files: season folders + 'NNN Title' names (21 Jump Street, live)")
# The real pack that tripped the gate: seasons 3/4 use bare '318 Partners
# Part 1.avi' naming. Sonarr mapped some to S01 (wrong) and left the rest
# unmapped. The Season folder + leading number is unambiguous.
EP_IDX_JS = {
    (1, 1): {"id": 101, "seasonNumber": 1, "episodeNumber": 1, "hasFile": True},
    (3, 1): {"id": 301, "seasonNumber": 3, "episodeNumber": 1, "hasFile": False},
    (3, 18): {"id": 318, "seasonNumber": 3, "episodeNumber": 18, "hasFile": False},
    (4, 10): {"id": 410, "seasonNumber": 4, "episodeNumber": 10, "hasFile": False},
}
BIG = 2 * 1024 ** 3
JS_ITEMS = [
    # Sonarr said S01E01; the folder says Season 3; the name says 3x18 -> veto
    {"path": "/x/1.avi", "relativePath": "21 Jump street Season 3\\318 Partners Part 1.avi",
     "size": BIG, "episodes": [{"id": 101, "seasonNumber": 1, "episodeNumber": 1}]},
    # Sonarr found nothing; folder + number resolve it
    {"path": "/x/2.avi", "relativePath": "21 Jump street Season 3\\301 Fun With Animals.avi",
     "size": BIG, "episodes": []},
    {"path": "/x/3.avi", "relativePath": "21 Jump Street Season 4\\410 Wheels and Deals Part 1.avi",
     "size": BIG, "episodes": []},
    # DVD-extra interviews are skipped, not blockers
    {"path": "/x/4.avi", "relativePath": "Season 1\\21 Jump Street - 1xInterview 1 - Stephen J. Cannell.avi",
     "size": BIG, "episodes": []},
    # no folder hint -> Sonarr's mapping stands (no veto without evidence)
    {"path": "/x/5.avi", "relativePath": "flat-file no folder.avi",
     "size": BIG, "episodes": [{"id": 101, "seasonNumber": 1, "episodeNumber": 1}]},
    # explicit SxxEyy that contradicts its folder -> never guess
    {"path": "/x/6.avi", "relativePath": "Season 2\\Show.S05E03.avi",
     "size": BIG, "episodes": []},
    # NNN whose season digit doesn't match the folder -> never guess
    {"path": "/x/7.avi", "relativePath": "Season 3\\501 Something.avi",
     "size": BIG, "episodes": []},
]
pl, sk, um = sp.map_files(JS_ITEMS, 7, EP_IDX_JS, Cfg)
by_base = {p["base"]: p for p in pl}
check("folder vetoes Sonarr's cross-season guess (318 -> S03E18)",
      by_base.get("318 Partners Part 1.avi", {}).get("payload", {}).get("episodeIds"),
      [318])
check("veto result is marked regex (a ~ row, checkable)",
      by_base.get("318 Partners Part 1.avi", {}).get("source"), "regex")
check("bare NNN resolves via folder (301 -> S03E01)",
      by_base.get("301 Fun With Animals.avi", {}).get("payload", {}).get("episodeIds"),
      [301])
check("bare NNN resolves via folder (410 -> S04E10)",
      by_base.get("410 Wheels and Deals Part 1.avi", {}).get("payload", {}).get("episodeIds"),
      [410])
check("interview extra skipped, not unmapped",
      ("21 Jump Street - 1xInterview 1 - Stephen J. Cannell.avi", "extra") in sk, True)
check("no folder hint -> Sonarr's mapping kept",
      by_base.get("flat-file no folder.avi", {}).get("payload", {}).get("episodeIds"),
      [101])
check("planned total (vetoed+NNNx2+kept-sonarr)", len(pl), 4)
check("contradictions stay unmapped, never guessed",
      sorted(b for b, _ in um), ["501 Something.avi", "Show.S05E03.avi"])

section("folder_season")
for path, want in [
    ("21 Jump street Season 3\\318.avi", 3),
    ("Pack\\Season.04\\file.avi", 4),
    ("Pack/S02/file.avi", 2),
    ("Show.S01-S05.COMPLETE\\file.avi", None),      # range root is not a season
    ("The.Wire.Complete.Series\\file.avi", None),
    ("Season 2\\subdir\\file.avi", 2),
    ("file.avi", None),
]:
    check("folder_season %r" % path, sp.folder_season(path), want)

section("conflict detection")
P = [{"base": "a.mkv", "payload": {"episodeIds": [1, 2]}},
     {"base": "b.mkv", "payload": {"episodeIds": [2]}},
     {"base": "c.mkv", "payload": {"episodeIds": [3]}}]
check("duplicate episode caught", sp.check_conflicts(P), [("a.mkv", "b.mkv")])
check("no false positive", sp.check_conflicts(P[2:]), [])


# ------------------------------------------------------- download completion

section("is_complete vs qBittorrent states (v5.1 verified live)")
# While qBittorrent relocates a finished download from the temp path to the
# real save path, progress is already 1.0 and amount_left is 0, but the files
# are mid-copy -- importing then would hand Sonarr partial files.
for state, progress, left, want, why in [
    ("downloading", 0.003, 20455392097, False, "still downloading"),
    ("moving",      1.0,   0,           False, "mid-move from temp path"),
    ("uploading",   1.0,   0,           True,  "seeding"),
    ("stalledUP",   1.0,   0,           True,  "seeding, no peers"),
    ("stoppedUP",   1.0,   0,           True,  "finished, stopped (qbt 5.x name)"),
    ("pausedUP",    1.0,   0,           True,  "finished, paused (qbt 4.x name)"),
    ("checkingUP",  1.0,   0,           False, "rechecking, files may be suspect"),
]:
    t = {"state": state, "progress": progress, "amount_left": left}
    check("%s -> %s (%s)" % (state, want, why), sp.is_complete(t), want)

section("manual_import request (shape verified against Sonarr 4.0.19)")


class _CapturedGet(Exception):
    pass


class _FakeSession(object):
    def __init__(self):
        self.headers = {}

    def get(self, url, **kw):
        self.url, self.kw = url, kw
        raise _CapturedGet()


class _SonarrCfg(object):
    sonarr_url = "http://sonarr-vm:8989"
    sonarr_key = "k"


_s = sp.Sonarr(_SonarrCfg)
_s.s = _FakeSession()
try:
    _s.manual_import("T:\\pack")
except _CapturedGet:
    pass
check("manualimport endpoint", _s.s.url.endswith("/api/v3/manualimport"), True)
check("filters existing files", _s.s.kw["params"].get("filterExistingFiles"), "true")
# Verified live on 4.0.19: passing seriesId can return 0 items for a folder
# the same scan finds 48 files in without it. We filter by series client-side.
check("no seriesId param", "seriesId" in _s.s.kw["params"], False)
# scanning a multi-season pack on a network share takes minutes, not seconds
check("scan timeout >= 300s", _s.s.kw.get("timeout", 0) >= 300, True)


# ------------------------------------------------------------------- --skip

section("--skip SxxEyy (exclude a bad file from an otherwise good pack)")
check("parses S03E02", sp.parse_skip_args(["S03E02"]), {(3, 2)})
check("case/zero-pad insensitive", sp.parse_skip_args(["s3e2"]), {(3, 2)})
check("multiple values", sp.parse_skip_args(["S01E01", "S02E10"]), {(1, 1), (2, 10)})

SKIP_PLANNED = [
    {"base": "a.mkv", "episodes": [{"seasonNumber": 1, "episodeNumber": 1}],
     "payload": {"episodeIds": [101]}},
    {"base": "bad.mkv", "episodes": [{"seasonNumber": 3, "episodeNumber": 2}],
     "payload": {"episodeIds": [302]}},
    # a double-episode file is excluded if ANY of its episodes is skipped
    {"base": "double.mkv", "episodes": [{"seasonNumber": 3, "episodeNumber": 1},
                                        {"seasonNumber": 3, "episodeNumber": 2}],
     "payload": {"episodeIds": [301, 302]}},
]
_skipped = []
_kept = sp.apply_skips(SKIP_PLANNED, _skipped, {(3, 2)})
check("kept only the clean file", [p["base"] for p in _kept], ["a.mkv"])
check("skip reasons recorded", _skipped,
      [("bad.mkv", "excluded by --skip"), ("double.mkv", "excluded by --skip")])
_skipped2 = []
check("no skips is a no-op", sp.apply_skips(SKIP_PLANNED, _skipped2, set()), SKIP_PLANNED)
check("no-op records nothing", _skipped2, [])


# -------------------------------------------------------------------- hunt

section("hunt config (radarr and [hunt] are optional)")
cfg_nohunt = cfg  # the ini parsed above has neither [radarr] nor [hunt]
check("radarr off when unconfigured", cfg_nohunt.radarr_url, None)
check("tv batch default", cfg_nohunt.hunt_tv_batch, 5)
check("movie batch default", cfg_nohunt.hunt_movie_batch, 3)
check("cooldown default (days)", cfg_nohunt.hunt_cooldown_days, 7.0)
check("delay default (s)", cfg_nohunt.hunt_delay, 5.0)
check("season threshold default", cfg_nohunt.hunt_season_threshold, 0.8)

INI_HUNT = INI + """[radarr]
url = http://radarr-vm:7878
api_key = ghi789
[hunt]
tv_batch = 2
movie_batch = 1
cooldown_days = 3
delay_seconds = 1
"""
tmp2 = os.path.join(tempfile.gettempdir(), "_seriespack_test_hunt.ini")
with open(tmp2, "w", encoding="utf-8") as fh:
    fh.write(INI_HUNT)
cfg_hunt = sp.Config(tmp2)
check("radarr url parsed", cfg_hunt.radarr_url, "http://radarr-vm:7878")
check("tv batch parsed", cfg_hunt.hunt_tv_batch, 2)
check("cooldown parsed", cfg_hunt.hunt_cooldown_days, 3.0)
os.remove(tmp2)

section("hunt only touches monitored items (episode AND its series)")
for r, want, why in [
    ({"monitored": True, "series": {"monitored": True}}, True, "both monitored"),
    ({"monitored": True, "series": {"monitored": False}}, False, "series unmonitored"),
    ({"monitored": False, "series": {"monitored": True}}, False, "episode unmonitored"),
    ({"monitored": True}, True, "no series key (radarr movie)"),
    ({"monitored": False}, False, "unmonitored movie"),
]:
    check(why, sp.huntable(r), want)

section("season-tier hunting: mostly-missing seasons collapse to one search")
SER = {"id": 42, "title": "Empty Show", "monitored": True, "seasons": [
    {"seasonNumber": 1, "statistics": {"episodeCount": 10, "episodeFileCount": 0}},
    {"seasonNumber": 2, "statistics": {"episodeCount": 10, "episodeFileCount": 9}},
    {"seasonNumber": 3, "statistics": {"episodeCount": 0, "episodeFileCount": 0}},
]}
check("fully missing season -> 1.0", sp.season_missing_fraction(SER, 1), 1.0)
check("one-gap season -> 0.1", round(sp.season_missing_fraction(SER, 2), 3), 0.1)
check("zero-episode season -> None", sp.season_missing_fraction(SER, 3), None)
check("unknown season -> None", sp.season_missing_fraction(SER, 9), None)

MISSING_RECS = [
    {"id": 501, "seasonNumber": 1, "series": SER},   # S1 empty -> season tier
    {"id": 502, "seasonNumber": 1, "series": SER},   # same season -> deduped
    {"id": 503, "seasonNumber": 2, "series": SER},   # S2 has 9/10 -> episode tier
]
cands = sp.build_tv_candidates(MISSING_RECS, 0.8)
check("collapsed to 2 candidates", len(cands), 2)
check("season candidate first", (cands[0]["kind"], cands[0]["key"]),
      ("season", "season:42:1"))
check("season candidate carries series/season",
      (cands[0]["seriesId"], cands[0]["seasonNumber"]), (42, 1))
check("gap stays episode tier", (cands[1]["kind"], cands[1]["key"]),
      ("episode", "ep:503"))
check("threshold 2.0 disables season tier",
      [c["kind"] for c in sp.build_tv_candidates(MISSING_RECS, 2.0)],
      ["episode", "episode", "episode"])

# cooldown applies to the season key exactly like an episode key
_NOW = 2000 * 86400.0
picked = sp.pick_by_key(cands, {"season:42:1": _NOW - 3600}, _NOW, 5, 7)
check("cooled-down season skipped", [c["key"] for c in picked], ["ep:503"])

# wanted/missing embeds series WITHOUT season statistics (verified live on
# 4.0.19); a full-series lookup must supply them
SER_BARE = {"id": 42, "title": "Empty Show",
            "seasons": [{"seasonNumber": 1, "monitored": True}]}
cands2 = sp.build_tv_candidates(
    [{"id": 501, "seasonNumber": 1, "series": SER_BARE}], 0.8,
    series_lookup={42: SER})
check("lookup supplies missing stats", (cands2[0]["kind"], cands2[0]["key"]),
      ("season", "season:42:1"))
check("no lookup + no stats -> episode tier",
      sp.build_tv_candidates(
          [{"id": 501, "seasonNumber": 1, "series": SER_BARE}], 0.8)[0]["kind"],
      "episode")

section("hunt candidate selection (cooldown + stalest-first)")
DAY = 86400.0
NOW = 1000 * DAY
RECS = [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
HUNTED = {
    "ep:1": NOW - 1 * DAY,    # hunted yesterday -> cooling down
    "ep:2": NOW - 30 * DAY,   # hunted a month ago -> eligible, stalest
    # ep:3, ep:4 never hunted -> eligible, first priority
}
check("cooldown filters recent, never-hunted first, then stalest",
      [r["id"] for r in sp.pick_hunt_candidates(RECS, HUNTED, NOW, 3, 7, "ep")],
      [3, 4, 2])
check("limit respected",
      [r["id"] for r in sp.pick_hunt_candidates(RECS, HUNTED, NOW, 1, 7, "ep")],
      [3])
check("prefix isolates tv from movies",
      [r["id"] for r in sp.pick_hunt_candidates(RECS, HUNTED, NOW, 4, 7, "movie")],
      [1, 2, 3, 4])
check("everything cooled down -> nothing",
      sp.pick_hunt_candidates(RECS[:1], {"ep:1": NOW}, NOW, 5, 7, "ep"), [])

section("upgrade hunting: cutoff-unmet items re-searched (Avatar purple, live)")
check("upgrade batch defaults",
      (cfg_nohunt.hunt_upgrade_tv_batch, cfg_nohunt.hunt_upgrade_movie_batch),
      (2, 1))
# a whole season below cutoff (one inferior release filled it) collapses to
# a single SeasonSearch; a lone below-cutoff episode stays individual
UP_SER = {"id": 9, "title": "Avatar TLA", "monitored": True, "seasons": [
    {"seasonNumber": 1, "monitored": True,
     "statistics": {"episodeCount": 8, "episodeFileCount": 8}},
    {"seasonNumber": 2, "monitored": True,
     "statistics": {"episodeCount": 7, "episodeFileCount": 7}},
]}
UP_RECS = (
    [{"id": 700 + i, "seasonNumber": 1, "series": UP_SER} for i in range(8)] +
    [{"id": 801, "seasonNumber": 2, "series": UP_SER}]
)
ucands = sp.build_upgrade_candidates(UP_RECS, 0.8)
check("full season collapses to one up-season candidate",
      (ucands[0]["kind"], ucands[0]["key"]), ("season", "up-season:9:1"))
check("lone below-cutoff episode stays individual",
      [c["key"] for c in ucands if c["kind"] == "episode"], ["up-ep:801"])
check("collapsed count", len(ucands), 2)
check("no season stats -> episode tier",
      sp.build_upgrade_candidates(
          [{"id": 700, "seasonNumber": 1,
            "series": {"id": 9, "title": "X", "seasons": []}}], 0.8)[0]["kind"],
      "episode")
# an upgrade cooldown never collides with the missing-hunt cooldown for the
# same season -- different key prefixes
check("missing-hunt cooldown does not block the upgrade search",
      [c["key"] for c in sp.pick_by_key(ucands, {"season:9:1": 1000.0 * 86400},
                                        1000.0 * 86400, 5, 7)],
      ["up-season:9:1", "up-ep:801"])

section("pack-tier hunting: mostly-missing shows become one series-pack grab")
check("pack tier config defaults",
      (cfg_nohunt.hunt_pack_min_seasons, cfg_nohunt.hunt_pack_batch), (2, 1))
INI_PACK = INI + "[hunt]\npack_min_seasons = 0\npack_batch = 2\n"
tmp3 = os.path.join(tempfile.gettempdir(), "_seriespack_test_pack.ini")
with open(tmp3, "w", encoding="utf-8") as fh:
    fh.write(INI_PACK)
cfg_pack = sp.Config(tmp3)
check("pack settings parsed",
      (cfg_pack.hunt_pack_min_seasons, cfg_pack.hunt_pack_batch), (0, 2))
os.remove(tmp3)

PACK_SER = {"id": 7, "title": "Ghost Town", "monitored": True,
            "statistics": {"episodeCount": 30},
            "seasons": [
    # specials never count, even when fully missing
    {"seasonNumber": 0, "monitored": True,
     "statistics": {"episodeCount": 5, "episodeFileCount": 0}},
    {"seasonNumber": 1, "monitored": True,
     "statistics": {"episodeCount": 10, "episodeFileCount": 0}},   # 1.0 missing
    {"seasonNumber": 2, "monitored": True,
     "statistics": {"episodeCount": 10, "episodeFileCount": 1}},   # 0.9 missing
    {"seasonNumber": 3, "monitored": False,
     "statistics": {"episodeCount": 10, "episodeFileCount": 0}},   # unmonitored
    {"seasonNumber": 4, "monitored": True,
     "statistics": {"episodeCount": 10, "episodeFileCount": 9}},   # 0.1 missing
]}
ONE_GAP = {"id": 8, "title": "Almost Full", "monitored": True, "seasons": [
    {"seasonNumber": 1, "monitored": True,
     "statistics": {"episodeCount": 10, "episodeFileCount": 0}},
]}
UNMON = {"id": 9, "title": "Ignored Show", "monitored": False, "seasons": [
    {"seasonNumber": 1, "monitored": True,
     "statistics": {"episodeCount": 10, "episodeFileCount": 0}},
]}
pk = sp.series_pack_candidates([PACK_SER, ONE_GAP, UNMON], 0.8, 2)
check("only the 2+-bad-season monitored show qualifies",
      [(c["key"], c["seasons"]) for c in pk], [("pack:7", [1, 2])])
check("min_seasons 1 also takes the one-gap show (title order)",
      [c["key"] for c in sp.series_pack_candidates([PACK_SER, ONE_GAP, UNMON], 0.8, 1)],
      ["pack:8", "pack:7"])
check("min_seasons 0 disables the tier",
      sp.series_pack_candidates([PACK_SER], 0.8, 0), [])

GB = 1024 ** 3
REL_NO_LINK = {"seasons": None, "link": "", "magnet": "", "size": 100 * GB}
REL_PARTIAL = {"seasons": {2, 3}, "link": "x", "magnet": "", "size": 100 * GB}
REL_TINY = {"seasons": None, "link": "x", "magnet": "", "size": 1 * GB}
REL_GOOD = {"seasons": {1, 2, 3}, "link": "x", "magnet": "", "size": 60 * GB}
check("skips no-link, wrong-coverage, undersized; takes first viable",
      sp.pick_pack_release([REL_NO_LINK, REL_PARTIAL, REL_TINY, REL_GOOD],
                           [1, 2], 30, Cfg) is REL_GOOD, True)
check("claims-complete (unenumerated) is acceptable",
      sp.pick_pack_release([REL_NO_LINK, {"seasons": None, "link": "x",
                                          "magnet": "", "size": 60 * GB}],
                           [1, 2], 30, Cfg) is not None, True)
check("nothing viable -> None",
      sp.pick_pack_release([REL_TINY, REL_PARTIAL], [1, 2], 30, Cfg), None)
check("unknown episode count skips the size-per-episode guard",
      sp.pick_pack_release([REL_TINY], [1, 2], 0, Cfg) is REL_TINY, True)

PLN, HAV, UNM = [{"b": 1}], [{"b": 2}], [("stray", [])]
check("scan outcome: clean plan imports",
      sp.pack_scan_outcome(PLN, [], [], [], 0.05), "import")
check("scan outcome: conflicts always manual",
      sp.pack_scan_outcome(PLN, [], [], [("x", "y")], 0.5), "manual")
check("scan outcome: 1%% unmapped tolerated",
      sp.pack_scan_outcome(PLN * 99, [], UNM, [], 0.05), "import")
check("scan outcome: 20%% unmapped -> manual",
      sp.pack_scan_outcome(PLN * 8, [], UNM * 2, [], 0.05), "manual")
check("scan outcome: everything already on disk -> done",
      sp.pack_scan_outcome([], HAV, [], [], 0.05), "done")
# the live 21JS re-scan: 103 already-have + the tolerated '523' stray must
# read as done, not park itself for manual attention forever
check("scan outcome: already-on-disk + tolerated stray -> done",
      sp.pack_scan_outcome([], HAV * 103, UNM, [], 0.05), "done")
check("scan outcome: empty scan -> manual",
      sp.pack_scan_outcome([], [], [], [], 0.05), "manual")
check("scan outcome: only unmapped -> manual",
      sp.pack_scan_outcome([], [], UNM, [], 0.5), "manual")
check("pack unmapped tolerance default", cfg_nohunt.hunt_pack_max_unmapped, 0.05)

SUPPRESS = [
    {"kind": "season", "key": "season:42:1", "seriesId": 42,
     "seasonNumber": 1, "title": "x"},
    {"kind": "episode", "key": "ep:9", "record": {"id": 9, "series": {"id": 42}}},
    {"kind": "episode", "key": "ep:10", "record": {"id": 10, "series": {"id": 5}}},
    {"kind": "pack", "key": "pack:42", "series": {"id": 42}},
]
check("pending pack suppresses that show's season/episode candidates",
      [c["key"] for c in sp.drop_pack_pending(SUPPRESS, {42})], ["ep:10"])
check("nothing pending -> candidates untouched",
      len(sp.drop_pack_pending(SUPPRESS, set())), 4)

section("pack adoption: untracked torrents matched to pack-hunted series")
# Seen live: Aladdin's .torrent arrived after grab_release stopped polling,
# leaving a downloaded-but-untracked pack that would never auto-import.
ADOPT_SERIES = [
    {"id": 1, "title": "Aladdin"},
    {"id": 2, "title": "24"},
    {"id": 3, "title": "24: Legacy"},
    {"id": 4, "title": "Never Pack Hunted"},
]
ADOPT_HUNTED = {"pack:1": 1.0, "pack:2": 1.0, "pack:3": 1.0}
for name, want, why in [
    ("Aladdin", 1, "exact title"),
    ("21.Jump.Street.Seasons.1-5.COMPLETE.DVDRip.XviD", None, "not pack-hunted"),
    ("Aaahh!!! Real Monsters Season 1-4 [DVDRip 480p x264]", None, "unknown show"),
    ("24 Legacy S01-S02 1080p", 3, "longest title wins over '24'"),
    ("24 S01-S08 COMPLETE", 2, "prefix match"),
    ("Never Pack Hunted S01-S03", None, "never pack-hunted -> not ours to claim"),
    ("Aladdinia S01", None, "prefix must end at a word boundary"),
]:
    got = sp.match_pack_series(name, ADOPT_SERIES, ADOPT_HUNTED)
    check("adopt: %s" % why, got["id"] if got else None, want)

section("qbittorrent config optional (seedbox-only setups leave url blank)")
INI_NOQB = """[sonarr]
url = http://sonarr-vm:8989
api_key = abc
[jackett]
url = http://jackett-vm:9117
api_key = def
[qbittorrent]
url =
username =
password =
category = seriespacks
"""
tmpq = os.path.join(tempfile.gettempdir(), "_seriespack_test_noqb.ini")
with open(tmpq, "w", encoding="utf-8") as fh:
    fh.write(INI_NOQB)
cfg_noqb = sp.Config(tmpq)
check("blank url -> None, config still loads", cfg_noqb.qb_url, None)
check("category default survives", cfg_noqb.qb_category, "seriespacks")
os.remove(tmpq)
INI_NOSEC = INI_NOQB.replace("[qbittorrent]\nurl =\nusername =\npassword =\ncategory = seriespacks\n", "")
with open(tmpq, "w", encoding="utf-8") as fh:
    fh.write(INI_NOSEC)
check("missing section entirely -> None", sp.Config(tmpq).qb_url, None)
os.remove(tmpq)
check("configured url still parses", cfg.qb_url, "http://qbit-vm:8080")

section("seedbox pack delivery: .torrent parsing and release requirements")
check("pack delivery config defaults",
      (cfg_nohunt.hunt_pack_delivery, cfg_nohunt.hunt_pack_watch,
       cfg_nohunt.hunt_pack_import_dir),
      ("qbittorrent", "rwatch/packs", ""))
check("pack in-flight cap default", cfg_nohunt.hunt_pack_pending_max, 5)
check("pack grab expiry default (days)", cfg_nohunt.hunt_pack_grab_days, 3.0)
# minimal but real bencode: d4:infod6:lengthi5e4:name7:My.Packee
TORRENT = b"d8:announce13:http://tr/ann4:infod6:lengthi5e4:name19:My.Pack.S01-S03.XyZee"
check("torrent_name reads info.name", sp.torrent_name(TORRENT),
      "My.Pack.S01-S03.XyZ")
check("garbage bytes -> None", sp.torrent_name(b"<html>not a torrent</html>"), None)
check("truncated torrent -> None", sp.torrent_name(TORRENT[:20]), None)
check("empty -> None", sp.torrent_name(b""), None)
GB = 1024 ** 3
REL_MAGNET = {"seasons": None, "link": "", "magnet": "magnet:?xt=x", "size": 60 * GB}
REL_LINKED = {"seasons": None, "link": "http://jackett/dl", "magnet": "", "size": 60 * GB}
check("magnet-only ok for qbittorrent delivery",
      sp.pick_pack_release([REL_MAGNET], [1, 2], 30, Cfg) is REL_MAGNET, True)
check("magnet-only skipped for seedbox delivery (watch dirs need a file)",
      sp.pick_pack_release([REL_MAGNET, REL_LINKED], [1, 2], 30, Cfg,
                           require_link=True) is REL_LINKED, True)
# a delivered-but-not-yet-imported pack must survive sync's prune
check("pending pack name survives pruning",
      sp.prune_candidates([("My.Pack.S01-S03.XyZ", 0.0)],
                          {"My.Pack.S01-S03.XyZ"}, 5000 * 86400.0, 3), [])

section("seedbox pack recovery: adoption of delivered strays + grab expiry")
check("age from explicit timestamp",
      round(sp.grab_age_days({"grabbedAtTs": 1000.0}, 1000.0 + 3 * 86400), 3), 3.0)
_ts0 = 1600000000.0
_s = __import__("time").strftime("%Y-%m-%d %H:%M:%S", __import__("time").localtime(_ts0))
check("age parsed from legacy string",
      round(sp.grab_age_days({"grabbedAt": _s}, _ts0 + 86400), 3), 1.0)
check("garbage timestamp -> None", sp.grab_age_days({"grabbedAt": "??"}, 0), None)
check("missing timestamp -> None", sp.grab_age_days({}, 0), None)

_ad = tempfile.mkdtemp(prefix="_seriespack_adopt")
for d in ("Aladdin", "Some.Random.Thing", "Tracked.Pack.S01-S02"):
    os.makedirs(os.path.join(_ad, d))


class _AdoptCfg(object):
    hunt_pack_import_dir = _ad


_astate = {"grabs": {"sb:Tracked.Pack.S01-S02": {"name": "Tracked.Pack.S01-S02"}},
           "packs_done": {}}
_saves = []
_orig_save = sp.save_state
sp.save_state = _saves.append
try:
    sp.adopt_delivered_packs(_AdoptCfg, _astate,
                             [{"id": 1, "title": "Aladdin"}],
                             {"pack:1": 1.0}, dry=False)
finally:
    sp.save_state = _orig_save
check("delivered stray adopted",
      _astate["grabs"].get("sb:Aladdin", {}).get("seriesId"), 1)
check("adopted entry is a seedbox delivery",
      _astate["grabs"]["sb:Aladdin"].get("delivery"), "seedbox")
check("tracked folder untouched, unknown folder ignored",
      sorted(_astate["grabs"]), ["sb:Aladdin", "sb:Tracked.Pack.S01-S02"])
check("state saved once", len(_saves), 1)
# a done pack is never re-adopted
_astate2 = {"grabs": {}, "packs_done": {"sb:Aladdin": 1.0}}
sp.save_state = _saves.append
try:
    sp.adopt_delivered_packs(_AdoptCfg, _astate2,
                             [{"id": 1, "title": "Aladdin"}], {"pack:1": 1.0},
                             dry=False)
finally:
    sp.save_state = _orig_save
check("done pack not re-adopted", _astate2["grabs"], {})
import shutil as _shx
_shx.rmtree(_ad)

section("stale .tmp upload cleanup (crashed upload remnant, live)")
_TNOW = 1000 * 86400.0
check("old dot-tmp flagged, fresh and real files kept",
      sp.stale_tmp_uploads([(".Myth.torrent.tmp", _TNOW - 2 * 86400),
                            (".Fresh.torrent.tmp", _TNOW - 60),
                            ("Real.torrent", _TNOW - 9 * 86400)], _TNOW),
      [".Myth.torrent.tmp"])

section("adoption skips tracked and completed packs (re-adopt loop, live)")
ADOPT_T = [
    {"hash": "h-tracked", "name": "Aladdin"},
    {"hash": "h-done", "name": "Aladdin"},
    {"hash": "h-new", "name": "Aladdin"},
    {"hash": "h-foreign", "name": "Some Manual Grab"},
]
_adopt, _stale = sp.pick_adoptions(
    ADOPT_T,
    grabs={"h-tracked": {}},
    done={"h-done": 1.0, "h-gone": 1.0},
    series_list=[{"id": 1, "title": "Aladdin"}],
    hunted={"pack:1": 1.0})
check("only the untracked, not-done match is adopted",
      [(h, s["id"]) for h, _n, s in _adopt], [("h-new", 1)])
check("done hash whose torrent left the category is stale", _stale, ["h-gone"])

section("console output survives exotic characters (U+200E crashed hunt live)")
import subprocess as _proc
_snippet = ("import sys; sys.path.insert(0, %r); import seriespack as s; "
            "s.safe_console(); print('title with mark: \\u200e end')"
            % os.path.dirname(os.path.abspath(__file__)))
_penv = dict(os.environ, PYTHONIOENCODING="cp1252")
_pr = _proc.run([sys.executable, "-c", _snippet], env=_penv,
                stdout=_proc.PIPE, stderr=_proc.PIPE)
check("no crash printing U+200E on a cp1252 console", _pr.returncode, 0)

section("auto-import never downgrades episodes that already have files")
HAVE_PLAN = [
    {"base": "s01e01.avi", "episodes": [{"id": 1, "hasFile": True}]},
    {"base": "s02e01.avi", "episodes": [{"id": 2, "hasFile": False}]},
    {"base": "multi.avi", "episodes": [{"id": 3, "hasFile": True},
                                       {"id": 4, "hasFile": False}]},
]
keep, have = sp.split_already_have(HAVE_PLAN)
check("already-have file set aside", [p["base"] for p in have], ["s01e01.avi"])
check("missing + partly-missing kept", [p["base"] for p in keep],
      ["s02e01.avi", "multi.avi"])


# --------------------------------------------------------------- renumbering

section("renumber args")
check("remap parses", sp.parse_remap_args(["S02E05=S02E06"]), {(2, 5): (2, 6)})
check("remap case insensitive", sp.parse_remap_args(["s2e5=s2e6"]), {(2, 5): (2, 6)})
check("offset parses", sp.parse_offset_args(["S02:+1"]), {2: 1})
check("negative offset", sp.parse_offset_args(["S02:-1", "S03:+2"]), {2: -1, 3: 2})

section("apply_renumber")
EP_IDX_RN = {
    (1, 1): {"id": 101, "seasonNumber": 1, "episodeNumber": 1},
    (1, 2): {"id": 102, "seasonNumber": 1, "episodeNumber": 2},
    (1, 3): {"id": 103, "seasonNumber": 1, "episodeNumber": 3},
    (2, 6): {"id": 206, "seasonNumber": 2, "episodeNumber": 6},
}


def mk(base, *eps):
    return {"base": base, "source": "sonarr",
            "episodes": [EP_IDX_RN[e] for e in eps],
            "payload": {"episodeIds": [EP_IDX_RN[e]["id"] for e in eps]}}


# remap overrides even a sonarr-sourced mapping: the user says Sonarr is wrong
_un = []
out = sp.apply_renumber([mk("a.mkv", (1, 1))], _un, {(1, 1): (2, 6)}, {}, EP_IDX_RN)
check("remap re-targets the file", out[0]["payload"]["episodeIds"], [206])
check("remapped rows are marked", out[0]["source"], "renumber")
check("no collateral unmapped", _un, [])

# offset shifts a whole season
_un = []
out = sp.apply_renumber([mk("a.mkv", (1, 1)), mk("b.mkv", (1, 2))],
                        _un, {}, {1: 1}, EP_IDX_RN)
check("offset shifts +1", [p["payload"]["episodeIds"] for p in out], [[102], [103]])

# a target that does not exist in Sonarr drops the file to unmapped
_un = []
out = sp.apply_renumber([mk("a.mkv", (1, 3))], _un, {}, {1: 1}, EP_IDX_RN)
check("missing target not planned", out, [])
check("missing target reported", _un,
      [("a.mkv", ["renumber target S01E04 does not exist in Sonarr"])])

# untouched files pass through unchanged
_un = []
out = sp.apply_renumber([mk("a.mkv", (1, 1))], _un, {(2, 6): (1, 2)}, {2: 1}, EP_IDX_RN)
check("untouched file keeps source", out[0]["source"], "sonarr")
check("untouched ids unchanged", out[0]["payload"]["episodeIds"], [101])


# --------------------------------------------------------------------- clean

section("clean: flagging failed/stalled torrents")


class CleanCfg(object):
    clean_stalled_hours = 24.0
    clean_meta_hours = 1.0
    clean_seed_ratio = 2.0
    clean_seed_hours = 336.0


H = 3600.0
NOW2 = 1000000.0
for t, want, why in [
    ({"state": "error"}, "errored", "error state"),
    ({"state": "missingFiles"}, "errored", "files gone"),
    ({"state": "metaDL", "added_on": NOW2 - 2 * H}, "meta-stalled", "metadata for 2h"),
    ({"state": "metaDL", "added_on": NOW2 - 0.5 * H}, None, "metadata, still fresh"),
    ({"state": "stalledDL", "last_activity": NOW2 - 25 * H}, "stalled", "no activity 25h"),
    ({"state": "stalledDL", "last_activity": NOW2 - 1 * H}, None, "stalled but recent"),
    ({"state": "downloading", "last_activity": NOW2 - 48 * H}, None, "actively downloading"),
    ({"state": "stalledUP", "last_activity": NOW2 - 999 * H}, None, "seeding, never flag"),
    ({"state": "stoppedDL", "last_activity": NOW2 - 999 * H}, None, "user-paused, leave alone"),
    ({"state": "uploading"}, None, "healthy seed"),
]:
    check("%s (%s)" % (t["state"], why), sp.assess_torrent(t, NOW2, CleanCfg), want)

section("clean config defaults")
check("stalled hours default", cfg.clean_stalled_hours, 24.0)
check("meta hours default", cfg.clean_meta_hours, 1.0)
check("seed ratio default", cfg.clean_seed_ratio, 2.0)
check("seed hours default (14d)", cfg.clean_seed_hours, 336.0)

section("clean --seeded: ratio / seeding-time rules")
for t, want, why in [
    ({"state": "stalledUP", "progress": 1.0, "ratio": 2.5, "seeding_time": 0},
     "seeded-ratio", "ratio met"),
    ({"state": "uploading", "progress": 1.0, "ratio": 0.1,
      "seeding_time": 400 * 3600}, "seeded-time", "time met"),
    ({"state": "stoppedUP", "progress": 1.0, "ratio": 2.5, "seeding_time": 0},
     "seeded-ratio", "finished+stopped counts"),
    ({"state": "stalledUP", "progress": 1.0, "ratio": 1.0, "seeding_time": 3600},
     None, "neither met"),
    ({"state": "downloading", "progress": 0.5, "ratio": 3.0, "seeding_time": 0},
     None, "not complete"),
    ({"state": "error", "progress": 1.0, "ratio": 9.0, "seeding_time": 0},
     None, "not a seeding state"),
]:
    check("%s (%s)" % (t["state"], why), sp.assess_seeded(t, CleanCfg), want)

section("orphan hardlink check")
_od = tempfile.mkdtemp(prefix="_seriespack_orphan")
_f1 = os.path.join(_od, "a.mkv")
with open(_f1, "w") as fh:
    fh.write("x")
check("lone file -> unreferenced", sp.unreferenced_on_disk(_od), True)
_link = os.path.join(_od, "a.link.mkv")
try:
    os.link(_f1, _link)
    check("hardlinked file -> referenced", sp.unreferenced_on_disk(_od), False)
    os.remove(_link)
except OSError:
    print("  (hardlinks unsupported here; skipping that case)")
check("missing path -> unverifiable", sp.unreferenced_on_disk(_od + "_nope"), None)
check("empty path -> unverifiable", sp.unreferenced_on_disk(""), None)
os.remove(_f1)
os.rmdir(_od)

section("attribute_release (for --research after removal)")
for name, want in [
    ("Show.Name.S02E05.1080p.WEB.mkv", ("tv", "Show Name", [(2, 5)])),
    ("Show.Name.S01E01E02.720p", ("tv", "Show Name", [(1, 1), (1, 2)])),
    ("The.Wire.S01-S05.1080p.BluRay", ("tv-pack", "The Wire", None)),
    ("Some.Show.Complete.Series.1080p", ("tv-pack", "Some Show", None)),
    ("Cool.Movie.2018.1080p.BluRay.x264", ("movie", "Cool Movie", 2018)),
    ("totally-unparseable-blob", (None, "totally-unparseable-blob", None)),
]:
    check(name, sp.attribute_release(name), want)


# --------------------------------------------------------------------- sync

section("sync config ([seedbox]/[sync] optional)")
check("seedbox off when unconfigured", cfg.seedbox_host, None)
check("no sync mappings by default", cfg.sync_maps, [])

INI_SYNC = INI + """[seedbox]
host = seed.example.net
username = me
password = pw
staging = T:\\(0)Filezilla\\(1)SyncStaging
[sync]
/home/me/completed/tv = T:\\(0)Filezilla\\(2)Import\\(0)TV
/home/me/completed/movies = T:\\(0)Filezilla\\(2)Import\\(0)Movies
"""
tmp3 = os.path.join(tempfile.gettempdir(), "_seriespack_test_sync.ini")
with open(tmp3, "w", encoding="utf-8") as fh:
    fh.write(INI_SYNC)
cfg_sync = sp.Config(tmp3)
check("host parsed", cfg_sync.seedbox_host, "seed.example.net")
check("port default", cfg_sync.seedbox_port, 22)
check("staging parsed", cfg_sync.seedbox_staging, "T:\\(0)Filezilla\\(1)SyncStaging")
check("mappings keep case and drive letters", cfg_sync.sync_maps,
      [("/home/me/completed/tv", "T:\\(0)Filezilla\\(2)Import\\(0)TV"),
       ("/home/me/completed/movies", "T:\\(0)Filezilla\\(2)Import\\(0)Movies")])
os.remove(tmp3)

section("sync item planning")
REMOTE = [("Show.S01.Pack", 100), ("Movie.2020", 200), ("Old.Thing", 50),
          ("Grew.Since", 75)]
plan = sp.plan_sync_items(
    REMOTE,
    synced={"Old.Thing": 50, "Grew.Since": 60},   # Grew.Since changed size
    import_existing={"Movie.2020"})
check("pulls new + changed, skips delivered + already-imported",
      [n for n, _ in plan], ["Show.S01.Pack", "Grew.Since"])

section("sync sample exclusion")
MB = 1024 ** 2
for rel, size, want, why in [
    ("Sample/movie-sample.mkv", 50 * MB, True, "sample folder"),
    ("Samples/whatever.mkv", 50 * MB, True, "samples folder"),
    ("Show.S01E01.sample.mkv", 60 * MB, True, "sample token in name"),
    ("sample.mkv", 40 * MB, True, "bare sample name"),
    ("Show.S01E01.sample.mkv", 900 * MB, False, "big file: token alone not enough"),
    ("Show.S01E01.mkv", 30 * MB, False, "small but no sample token"),
    ("Ensemble.Cast.S01E01.mkv", 50 * MB, False, "'sample' inside a word doesn't count"),
    ("Show.S01E01.mkv", 900 * MB, False, "normal episode"),
]:
    check("%s (%s)" % (rel, why), sp.is_sample(rel, size, 200 * MB), want)
check("config default skip_samples", cfg_sync.seedbox_skip_samples, True)
check("config default sample_max_mb", cfg_sync.seedbox_sample_max_mb, 200.0)

section("sync completeness verification")
A = [("a.mkv", 10), ("Sub/b.mkv", 20)]
check("matching manifests", sp.manifest_matches(A, list(reversed(A))), True)
check("size mismatch caught", sp.manifest_matches(A, [("a.mkv", 10), ("Sub/b.mkv", 19)]), False)
check("missing file caught", sp.manifest_matches(A, A[:1]), False)
check("extra local file caught", sp.manifest_matches(A[:1], A), False)

section("sync handles single-file and folder torrents (fake SFTP)")
import shutil as _sh
import stat as _st


class _Attr(object):
    def __init__(self, name, mode, size):
        self.filename, self.st_mode, self.st_size = name, mode, size


class FakeSFTP(object):
    """path -> size map; directories are implied by deeper paths."""
    def __init__(self, files):
        self.files = files

    def listdir_attr(self, path):
        pfx = path.rstrip("/") + "/"
        kids = {}
        for p, s in self.files.items():
            if not p.startswith(pfx):
                continue
            rest = p[len(pfx):]
            head = rest.split("/", 1)[0]
            if "/" in rest:
                kids.setdefault(head, ("dir", 0))
            else:
                kids[head] = ("file", s)
        if not kids:
            raise IOError(2, "No such file")
        return [_Attr(n, _st.S_IFDIR if k == "dir" else _st.S_IFREG, s)
                for n, (k, s) in sorted(kids.items())]

    def get(self, remote, local):
        with open(local, "wb") as fh:
            fh.write(b"x" * self.files[remote])


FAKE = FakeSFTP({
    "c/tv/Single.Episode.mkv": 11,
    "c/tv/Pack/ep1.mkv": 7,
    "c/tv/Pack/Sample/junk-sample.mkv": 3,
})
check("top-level listing types",
      sp.list_remote_items(FAKE, "c/tv"),
      [("Pack", 10, "dir"), ("Single.Episode.mkv", 11, "file")])


class _SyncCfg(object):
    seedbox_skip_samples = True
    seedbox_sample_max_mb = 0.001   # ~1KB: the 3-byte sample is under it
    seedbox_extract = False
    seedbox_unrar = ""


_sy = tempfile.mkdtemp(prefix="_seriespack_pull")
_SyncCfg.seedbox_staging = os.path.join(_sy, "staging")
_impd = os.path.join(_sy, "import")
os.makedirs(_SyncCfg.seedbox_staging)
os.makedirs(_impd)

check("single-file item delivered",
      sp.pull_item(FAKE, _SyncCfg, "c/tv", "Single.Episode.mkv", "file", _impd, 11), True)
check("file content arrived",
      os.path.getsize(os.path.join(_impd, "Single.Episode.mkv")), 11)
check("folder item delivered",
      sp.pull_item(FAKE, _SyncCfg, "c/tv", "Pack", "dir", _impd, 10), True)
check("episode arrived", os.path.getsize(os.path.join(_impd, "Pack", "ep1.mkv")), 7)
check("sample excluded from delivery",
      os.path.exists(os.path.join(_impd, "Pack", "Sample")), False)
_sh.rmtree(_sy)

section("sync extraction: archives unpack in staging, never in the import dir")
check("rar volume set grouped",
      sp.archive_sets(["x.rar", "x.r00", "x.r01", "notes.nfo"]),
      [("x.rar", ["x.r00", "x.r01"])])
check("partN set: part1 is primary",
      sp.archive_sets(["y.part2.rar", "y.part1.rar", "y.part3.rar"]),
      [("y.part1.rar", ["y.part2.rar", "y.part3.rar"])])
check("zip is its own set", sp.archive_sets(["a.zip"]), [("a.zip", [])])
check("case-insensitive", sp.archive_sets(["A.RAR"]), [("A.RAR", [])])
check("orphan volumes (no primary) never touched",
      sp.archive_sets(["x.r00", "x.r01"]), [])
check("plain video is not an archive", sp.archive_sets(["a.mkv", "b.avi"]), [])
check("7z cmd shape", sp.extractor_cmd(r"C:\Program Files\7-Zip\7z.exe", "a.rar", "D:\\out")[:3],
      [r"C:\Program Files\7-Zip\7z.exe", "x", "-y"])
check("unrar cmd ends with dest + sep",
      sp.extractor_cmd(r"C:\x\UnRAR.exe", "a.rar", "D:\\out")[-1], "D:\\out" + os.sep)

# end-to-end with a real zip through pull_item (stdlib, no tool needed)
import zipfile as _zf
_xd = tempfile.mkdtemp(prefix="_seriespack_extract")
_xstage = os.path.join(_xd, "staging")
_ximp = os.path.join(_xd, "import")
os.makedirs(_xstage)
os.makedirs(_ximp)
_zsrc = os.path.join(_xd, "payload.zip")
with _zf.ZipFile(_zsrc, "w") as z:
    z.writestr("Show.S01E01.mkv", "v" * 4096)
_zbytes = open(_zsrc, "rb").read()


class _ZipSFTP(object):
    def listdir_attr(self, path):
        raise IOError(2, "flat file only")

    def get(self, remote, local):
        with open(local, "wb") as fh:
            fh.write(_zbytes)


class _XCfg(object):
    seedbox_skip_samples = False
    seedbox_sample_max_mb = 200
    seedbox_extract = True
    seedbox_unrar = ""
    seedbox_staging = _xstage


check("single-file zip item delivered",
      sp.pull_item(_ZipSFTP(), _XCfg, "c/tv", "Pack.zip", "file", _ximp, len(_zbytes)),
      True)
check("delivered as a dir named after the item (queue-matchable)",
      os.path.isdir(os.path.join(_ximp, "Pack.zip")), True)
check("video extracted inside",
      os.path.getsize(os.path.join(_ximp, "Pack.zip", "Show.S01E01.mkv")), 4096)
check("archive itself removed after extraction",
      os.path.exists(os.path.join(_ximp, "Pack.zip", "Pack.zip")), False)

# a rar with no tool available: reported, delivered as-is, volumes kept
_rd = os.path.join(_xd, "RarItem")
os.makedirs(_rd)
with open(os.path.join(_rd, "x.rar"), "wb") as fh:
    fh.write(b"Rar!junk")
with open(os.path.join(_rd, "x.r00"), "wb") as fh:
    fh.write(b"junk")
_n, _errs = sp.extract_archives(_rd, None)
check("rar without tool -> error, nothing extracted", (_n, len(_errs)), (0, 1))
check("volumes preserved on failure",
      sorted(os.listdir(_rd)), ["x.r00", "x.rar"])

# a corrupt zip: reported, volume preserved
with open(os.path.join(_rd, "bad.zip"), "wb") as fh:
    fh.write(b"PK\x03\x04 not really a zip")
_n2, _errs2 = sp.extract_archives(_rd, None)
check("corrupt zip -> error, file kept",
      (_n2, len(_errs2) >= 1, os.path.exists(os.path.join(_rd, "bad.zip"))),
      (0, True, True))
_sh.rmtree(_xd)

check("extract config default on", cfg_sync.seedbox_extract, True)
check("unrar_tool default empty", cfg_sync.seedbox_unrar, "")

section("sync pull progress (throttled lines for the task window)")
check("eta seconds", sp.eta_str(45), "45s")
check("eta minutes", sp.eta_str(150), "2m30s")
check("eta hours", sp.eta_str(7500), "2h05m")
_clockv = [0.0]
_lines = []
_pp = sp.PullProgress("Show.S01E05.mkv", 200 * 1024 ** 2, interval=10.0,
                      clock=lambda: _clockv[0], sink=_lines.append)
_clockv[0] = 1.0
_pp(10 * 1024 ** 2)
check("quiet inside the interval", _lines, [])
_clockv[0] = 12.0
_pp(50 * 1024 ** 2)
check("one line after the interval", len(_lines), 1)
check("line carries percent and filename",
      ("25%" in _lines[0], "Show.S01E05.mkv" in _lines[0], "left" in _lines[0]),
      (True, True, True))
_clockv[0] = 13.0
_pp(60 * 1024 ** 2)
check("throttled again after printing", len(_lines), 1)
_clockv[0] = 30.0
_pp(200 * 1024 ** 2)
check("completion prints nothing (the delivered line covers it)",
      len(_lines), 1)

section("sync atomic delivery")
_sd = tempfile.mkdtemp(prefix="_seriespack_sync")
_stage = os.path.join(_sd, "staging", "Item")
_imp = os.path.join(_sd, "import")
os.makedirs(_stage)
os.makedirs(_imp)
with open(os.path.join(_stage, "f.mkv"), "w") as fh:
    fh.write("x")
sp.atomic_deliver(_stage, _imp, "Item")
check("item delivered", os.path.exists(os.path.join(_imp, "Item", "f.mkv")), True)
check("staging emptied", os.path.exists(_stage), False)
try:
    os.makedirs(_stage)
    sp.atomic_deliver(_stage, _imp, "Item")
    check("refuses to overwrite existing delivery", False, True)
except RuntimeError:
    check("refuses to overwrite existing delivery", True, True)
import shutil as _sh
_sh.rmtree(_sd)


# ------------------------------------------------------------------ pruning

section("sync prune: delivered, old, and not referenced by any queue")
DAY = 86400.0
_PNOW = 5000 * DAY
ITEMS_P = [
    ("Old.Unreferenced.Pack", _PNOW - 5 * DAY),
    ("Old.But.Queued.Pack", _PNOW - 5 * DAY),
    ("Fresh.Pack", _PNOW - 0.5 * DAY),
]
check("prunes only old + unreferenced",
      sp.prune_candidates(ITEMS_P, {"Old.But.Queued.Pack"}, _PNOW, 3),
      ["Old.Unreferenced.Pack"])
check("prune_days=0 disables", sp.prune_candidates(ITEMS_P, set(), _PNOW, 0), [])
check("prune config default", cfg_sync.sync_prune_days, 3.0)

section("clean --queues: stuck import-pending items")
check("iso parse", sp.parse_iso_utc("2026-09-02T05:41:00Z") is not None, True)
check("iso parse fractional", sp.parse_iso_utc("2026-09-02T05:41:00.123Z") is not None, True)
check("iso parse garbage", sp.parse_iso_utc("not-a-date"), None)
check("iso parse empty", sp.parse_iso_utc(""), None)

_t0 = sp.parse_iso_utc("2026-09-01T00:00:00Z")
_qnow = _t0 + 5 * DAY
QRECS = [
    # one pack = many per-episode rows sharing a downloadId
    {"id": 1, "downloadId": "AAA", "title": "Stuck.Pack", "added": "2026-09-01T00:00:00Z",
     "trackedDownloadState": "importPending"},
    {"id": 2, "downloadId": "AAA", "title": "Stuck.Pack", "added": "2026-09-01T00:00:00Z",
     "trackedDownloadState": "importPending"},
    {"id": 3, "downloadId": "BBB", "title": "Still.Downloading", "added": "2026-09-01T00:00:00Z",
     "trackedDownloadState": "downloading"},
    {"id": 4, "downloadId": "CCC", "title": "Recently.Finished", "added": "2026-09-05T23:00:00Z",
     "trackedDownloadState": "importPending"},
    {"id": 5, "downloadId": "DDD", "title": "No.Date", "trackedDownloadState": "importPending"},
]
stuck = sp.pick_stuck_queue(QRECS, _qnow, 3)
check("one stuck download found", sorted(stuck.keys()), ["AAA"])
check("all its episode rows grouped", sorted(stuck["AAA"]["ids"]), [1, 2])
check("downloading never touched", "BBB" in stuck, False)
check("young importPending left alone", "CCC" in stuck, False)
check("missing added-date left alone (safe)", "DDD" in stuck, False)
check("queue stuck-days config default", cfg.clean_queue_stuck_days, 3.0)


# ---------------------------------------------------------------- safety gate

section("dry-run gate")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "seriespack.py"), encoding="utf-8") as fh:
    SRC = fh.read()
body = SRC.split("def cmd_import")[1].split("def sonarr_cmd_id")[0]
before, after = body.split("if not args.confirm:")
check("no ManualImport before the gate", "ManualImport" in before, False)
check("ManualImport only after the gate", "ManualImport" in after, True)
check("gate returns early", bool(re.search(r"\breturn\b", after.split("\n")[2])), True)


if FAILS:
    print("\n%d FAILURE(S):" % len(FAILS))
    for f in FAILS:
        print("  - %s" % f)
    sys.exit(1)

print("\nALL CHECKS PASSED")
sys.exit(0)
