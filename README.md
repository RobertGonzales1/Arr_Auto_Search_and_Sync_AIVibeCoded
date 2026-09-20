# Arr Auto Search and Sync

Grabs complete-series torrents and imports them into Sonarr — the thing Sonarr
won't do on its own.

## Why this exists

Sonarr's automatic pipeline rejects multi-season releases. Force one through
interactive search and the import typically lands only season 1. That limitation
lives in the *automatic* import path, not in the parser: Sonarr's manual-import
API happily accepts an explicit file → episode mapping spanning any number of
seasons. This script builds that mapping and submits it.

## Setup

1. Put `seriespack.py` and `seriespack.ini` in the same folder on whichever VM
   you like — everything is HTTP, so it doesn't need to be the Sonarr box.
2. `pip install requests`
3. `copy seriespack.ini.example seriespack.ini` and fill in the three API keys.

### The one thing that will bite you

Sonarr and qBittorrent are on different VMs, so the path qBittorrent reports
means nothing to Sonarr on its own machine.

**You already have this mapping.** Sonarr → Settings → Download Clients →
Remote Path Mappings holds the exact pair Sonarr uses today. Copy those two
values straight into `[paths]` — Remote Path on the left, Local Path on the
right:

```ini
[paths]
D:\Torrents = T:\(0)Media\Torrents
```

The left side is whatever qBittorrent calls that folder on its own VM; run
`status` and compare against the `content_path` if you're unsure. The right side
is `T:\(0)Media\Torrents`, which Sonarr already proves it can reach every time
it imports normally.

Since `T:` is a mapped drive it's per-user — but it evidently resolves for
whatever account Sonarr runs under, or normal imports would already be failing.
Don't "fix" it to a UNC path unless you see Sonarr fail to scan the folder.

Two syntax notes for that section: only `=` splits the two sides, so drive
letters are safe. Don't quote the paths, and don't leave a trailing backslash.

### The other thing

`category = seriespacks` in the config must **not** be the category Sonarr
watches. That isolation is what keeps Sonarr's completed-download handling from
seeing the torrent and mangling it before you import. Don't point this at
`tv-sonarr`.

## Use

```bash
# find the series id / confirm the title Sonarr knows
python seriespack.py series "babylon"

# see what packs exist
python seriespack.py search "Babylon 5"

# grab one (index from the search table)
python seriespack.py grab "Babylon 5" --pick 0

# check on it
python seriespack.py status

# dry run the import — prints the full mapping, changes nothing
python seriespack.py import "Babylon 5"

# commit
python seriespack.py import "Babylon 5" --confirm

# Huntarr-style backlog hunt: search a few missing items, then stop
python seriespack.py hunt            # episodes + movies (movies need [radarr])
python seriespack.py hunt --tv --dry # preview without searching
```

## clean: handling failed and stalled downloads

`clean` inspects a qBittorrent category and flags torrents that are errored,
stuck fetching metadata (default >1h), or stalled with no activity (default
>24h) — thresholds in `[clean]`. Seeding, downloading, and user-paused
torrents are never flagged. Listing is the default; `--remove` deletes the
flagged torrents **and their data** and forgets them from tracking:

```bash
python seriespack.py clean                       # the seriespacks category
python seriespack.py clean --category tv-sonarr # any other category
python seriespack.py clean --remove             # actually delete the flagged
python seriespack.py clean --remove --research  # ...and search for replacements
python seriespack.py clean --seeded             # also flag seeds past ratio/time
python seriespack.py clean --orphans            # also flag unreferenced downloads
```

- `--seeded` flags finished seeds that met `[clean]` `seed_ratio` **or**
  `seed_hours` (defaults 2.0 / 14 days).
- `--orphans` flags downloads whose files nothing hardlinks to (i.e. no
  library import references them). This needs filesystem access to the
  download paths, so run it on a machine that can see them; unverifiable
  torrents are reported and left alone, never guessed at.
- `--research` (with `--remove`) triggers replacement searches for whatever
  each removed torrent contained: tracked packs get a `SeriesSearch`, releases
  with parseable `SxxEyy` get an `EpisodeSearch`, movie-shaped names get a
  Radarr `MoviesSearch`. Anything it can't attribute it says so and leaves to
  you.
- A pack that is downloaded but not yet imported shows `pending-import` and is
  never flagged by `--seeded`/`--orphans`.
- `--queues` also clears Sonarr/Radarr queue items that finished downloading
  but sat unable to import for `queue_stuck_days` (default 3) — e.g. a season
  pack whose leftover episodes Sonarr keeps re-checking forever. With
  `--remove` they're removed from the queue (NOT from the torrent client, so
  seeding continues), blocklisted, and a replacement search fires; without
  `--remove` they're just listed.

To clean a different qBittorrent instance (e.g. the one Radarr uses), point a
second config file's `[qbittorrent]` at it and run `seriespack.py -c that.ini
clean --category <its category>`.

## sync: seedbox -> NAS without partial-import accidents

`sync` pulls completed items from a seedbox over SFTP (`pip install paramiko`,
config in `[seedbox]`/`[sync]`). What makes it safe where a plain sync tool
isn't: items download into a **staging** folder nothing watches, every file's
size is verified against the seedbox, and only then is the item **renamed**
into the import folder — an atomic move (staging must be on the same drive),
so Sonarr/Radarr see either nothing or the complete item, never a partial.

```bash
python seriespack.py sync --dry   # list what would transfer
python seriespack.py sync         # pull, verify, deliver
```

Archives (`.zip`, `.rar` + volume sets, `.7z`) are **unpacked in staging**
after verification and before delivery — the *arrs can't import archives,
and this way they only ever see the extracted result. The archive volumes
are deleted once extraction succeeds; a single-file archive torrent
delivers as a folder of the same name so queue references still match.
Zip needs nothing extra; rar/7z auto-detect 7-Zip or UnRAR (or set
`unrar_tool`). A corrupt archive is delivered as-is with a warning — it
will go queue-stuck and `clean --queues` blocklists and re-searches it,
which is exactly right. `extract = false` turns all of this off.

Delivered items are remembered in the state file so they aren't re-pulled
while they keep seeding on the seedbox; a failed transfer leaves its partial
in staging and resumes (file-by-file) next run. Delivered items are **pruned**
from the import dirs once they're `prune_days` old (default 3) and no *arr
queue references them — imported or abandoned, never pending; sync only ever
deletes items it delivered itself, and skips pruning entirely if an *arr
can't be reached. `--no-prune` skips it for one run; `prune_days = 0`
disables it. One seedbox serves both TV
and movies: give each its own rtorrent label -> completed dir -> `[sync]`
mapping. Schedule it with `run_sync.ps1` (see `setup_tasks.cmd`).

## hunt: replacing Huntarr

`hunt` asks Sonarr (and Radarr, if `[radarr]` is configured) for missing
monitored items and triggers its native search for a small batch — default 5
searches and 3 movies per run. A season that's mostly missing (default ≥80%
of its aired episodes, `season_threshold`) is hunted as a single
`SeasonSearch` — one indexer query that can land a whole single-season pack,
which Sonarr's automatic pipeline happily accepts — while scattered gaps get
individual `EpisodeSearch` calls. Only monitored episodes of monitored series
are ever searched. Three things keep it from overloading anything:

- the batch cap (paging the *local* wanted/missing list is free; only actual
  searches hit Jackett and the indexers),
- a per-item cooldown (default 7 days, tracked in the state file) so repeated
  runs walk the whole backlog instead of re-searching the same items,
- a pause between search commands (default 5s).

Run it from Task Scheduler every 30–60 minutes and it quietly chews through
the backlog. Radarr's download client is its own business — a separate
qBittorrent instance is fine, the script never contacts it.

Hunt also walks the **quality-upgrade backlog** (Huntarr's upgrade mode):
items that have a file but sit below the profile cutoff — purple in
Sonarr — get re-searched, `upgrade_tv_batch`/`upgrade_movie_batch` per run
(defaults 2 and 1, own cooldowns). This can't lower quality: the *arrs
reject anything that isn't a strict improvement on an upgrade search. A
season that's mostly below cutoff (one inferior release filled it)
collapses into a single `SeasonSearch`. `--no-upgrades` skips it for a
run; batch 0 disables it.

### The pack tier: shows that are missing nearly everything

A show with `pack_min_seasons` (default 2) or more mostly-missing seasons
would take weeks of season-by-season hunting, so hunt escalates it to the
full seriespack pipeline automatically:

1. It searches Jackett for a complete-series pack and auto-picks the best
   release that passes the `[preferences]` filters **and** covers every
   mostly-missing season **and** has a sane size-per-episode (a single
   season mislabeled "complete" fails that check).
2. The grab lands in the isolated `seriespacks` qBittorrent category and is
   tracked in the state file; while it downloads, hunt stops wasting
   season/episode searches on that show.
3. Each later run checks on it. Once complete, the folder is scanned and
   imported only when the mapping is trustworthy: zero conflicts, and at
   most `pack_max_unmapped` (default 5%) of the files unresolved — those
   are **never imported**, their episodes simply stay with the episode
   hunter. Anything worse is reported and left for you to finish with a
   normal `import` run (reminded each run, re-checked automatically daily).

The mapper knows the ugly pack conventions found in the wild (all of these
came from a real 21 Jump Street grab): `Season 3/318 Partners.avi` style
names resolve via the folder + leading number; a Sonarr guess that
contradicts its Season folder is vetoed and re-parsed rather than trusted;
DVD extras (interviews, featurettes, samples...) are skipped, not
blockers. Two more autopilot rules: a pack file whose episode already has
a library file is skipped — auto-import fills gaps, it never downgrades —
and a grab qBittorrent reports late (slow VPN) is *adopted* into tracking
on the next run instead of being lost — finished packs are remembered by
hash so the still-seeding torrent is never re-adopted and re-scanned.

Packs can download through the seedbox instead of qBittorrent
(`pack_delivery = seedbox`): the `.torrent` is dropped into a
`pack_watch_dir` subfolder of the rtorrent watch dir over SFTP — rutorrent
labels, seeds (ratio!), and AutoMoves it; `sync` delivers the verified item
into `pack_import_dir`; auto-import takes it from there ("the item exists"
IS the completion signal, since sync only ever delivers complete items).
Needs a `[sync]` mapping for the packs label and a `[paths]` entry that
translates `pack_import_dir` into a path Sonarr can open. Pending packs are
protected from sync's pruning until they import; magnet-only releases are
skipped in this mode (a watch folder needs a real file).

If no qualifying pack exists, the show simply falls back to season-tier
hunting, and the pack search itself goes on cooldown like any other hunt.
`--no-packs` skips the tier for one run (pending imports still complete);
`pack_min_seasons = 0` turns it off entirely. `pack_batch` (default 1)
caps grabs per run — deliberate, since each is a multi-GB download nobody
eyeballed first.

## Reading the import plan

```
   S01E01                 Babylon.5.S01E01.1080p.mkv
 ~ S01E02                 Babylon.5.S01E02.1080p.mkv
   S02E01                 Babylon.5.S02E01.1080p.mkv
```

- No marker — Sonarr's own parser mapped the file. Trust it.
- `~` — Sonarr couldn't map it, so the script matched `SxxExx` from the
  filename against Sonarr's episode list. **Check these rows.** This is where a
  wrong guess would file an episode in the wrong season.
- `!` — renumbered by `--remap`/`--offset` (below). Double-check these rows.
- `?` under UNMAPPED — nothing matched; these are left alone, not imported.

When Sonarr's episode numbering disagrees with a release (TVDB vs scene
numbering, off-by-one seasons), override it — these win even over Sonarr's own
mapping, and a target that doesn't exist in Sonarr drops the file to UNMAPPED
rather than guessing:

```bash
# this file belongs one episode over
python seriespack.py import "Show" --remap S02E05=S02E06

# the whole season is shifted by one
python seriespack.py import "Show" --offset S02:+1
```

If one file in an otherwise good pack is junk (undersized, flagged `Sample`),
exclude it and let Sonarr's normal pipeline fetch that episode properly:

```bash
python seriespack.py import "Lie to Me" --skip S03E02 --confirm
```

Safety rails, all of which you can override with `--force`:

- Dry run is the default. `--confirm` is required to write anything.
- Aborts if more than 15% of files are unmapped (`--max-unmapped`).
- Aborts if two files claim the same episode.
- `import_mode = copy` leaves the torrent in place so seeding continues.

## Automating it further

Once you trust the mapping on a few shows, `import --confirm` is safe to run on
a schedule. A Task Scheduler entry every 30 minutes that walks tracked
downloads and imports the finished ones closes the loop. Use PowerShell rather
than a `.bat` — your `(0)Media` folder name has parentheses in it, and those
break `cmd.exe` parsing inside `for` and `if` blocks:

```powershell
Get-Content watchlist.txt | ForEach-Object {
    python C:\tools\seriespack\seriespack.py import "$_" --confirm
}
```

I'd leave `grab` manual, though. Release quality on series packs varies enough
that picking by hand is worth the ten seconds.

## Limits

- Absolute episode numbering (common on anime) isn't handled — the regex
  fallback expects `SxxExx` or `1x01`. Sonarr's own parser handles a lot of it,
  so those files usually map without the fallback.
- Specials (season 0) are skipped unless Sonarr maps them itself.
- Jackett's aggregate `all` endpoint can be slow and will time out individual
  indexers silently. Point `indexer` at a specific one if results look thin.
