# PikpakGet

English | [中文](README.cn.md)

Sequentially grab PikPak share links with a small cloud quota. Pure standard
library, no browser, no GUI automation, no official desktop client required.

The problem this solves: PikPak free accounts have **6 GiB** of cloud space (6 442 450 944 bytes),
and a shared folder is routinely hundreds of GB to tens of TB. You cannot "save everything then download"
— so this tool works one *file* at a time:

```
restore one file into your drive  →  download it  →  verify its content hash
      →  permanently delete the cloud copy  →  wait for the quota to drop  →  next
```

Everything is journalled to `state.json` before each side effect, so `Ctrl-C`,
a dropped connection or a reboot costs you at most the file in flight: rerun the
same command and it picks up where it stopped.

## Install

Requires Python 3.10+, macOS or Linux, and `curl` on PATH (used for ranged parallel
segments; the single-stream default does not need it). No third-party Python
packages.

```bash
git clone git@github.com:YJBeetle/PikpakGet.git
cd PikpakGet
python3 -m pikpakget --help          # run it in place
pip install -e . && pikpakget --help  # or get a console script

# or install a published release straight from its assets, no clone needed (assets are
# attached from v0.1.4 on, since that is the first tag carrying the build workflow):
# pip install https://github.com/YJBeetle/PikpakGet/releases/download/v<版本>/pikpakget-<版本>-py3-none-any.whl
```

## Use

```bash
# 1. one-time sign-in; stores a refreshable session in .pikpakget/session.json (0600)
python3 -m pikpakget --login you@example.com

# 2. optional: see what a link list contains, using zero cloud space
python3 -m pikpakget links.txt --inventory --inventory-out inventory.csv

# 3. grab it
python3 -m pikpakget links.txt --dest /path/to/library
```

`links.txt` is one share link per line, processed top to bottom. Lines starting
with `#` are ignored. An optional tab and second column sets the destination
folder, so files can be filed however you like:

```
https://mypikpak.com/s/SOME_SHARE_ID	OneSeries
https://mypikpak.com/s/ANOTHER_ID
```

Instead of a second column, pass a `url,folder` CSV/TSV with `--folder-map`; lines
that match neither go to `--default-folder`.

Other useful entry points:

```bash
python3 -m pikpakget links.txt --status         # progress, measured speed, ETA
python3 -m pikpakget --version
python3 -m pikpakget --whoami                  # identity, quota, offline task slots
python3 -m pikpakget links.txt --dry-run       # plan the work, change nothing
python3 -m pikpakget links.txt --max-files 5   # dip a toe in
```

## Options

| flag | default | meaning |
|---|---|---|
| `--dest DIR` | `./downloads` | root of the library; each folder becomes `DIR/<folder>/` |
| `--state-dir DIR` | `./.pikpakget` | session, device id, `state.json`, single-instance lock |
| `--connections N` | `4` | ranged connections per file; `1` = plain single stream |
| `--gap SEC` | `20` | rest between files, keeps request density low |
| `--log PATH` | `.pikpakget/grab-<date>.log` | `-` for stdout only |
| `--repeat N` | `0` (one pass) | re-pass the list to finish links that failed transiently; stops when a whole pass lands nothing |
| `--limit N` / `--max-files N` | `0` (off) | stop after N links / N files |
| `--inventory` | off | size up every link (count, bytes, largest file), no downloads |
| `--doctor` | off | preflight this machine (python, curl, FIPS SHA-1, volumes, session, quota, stale cloud copies); no cloud space, exits 1 on anything blocking |
| `--verify` | off | re-check every already-downloaded file against the server's content hash; quarantines what fails as `<name>.unverified` (never deletes), exits 1 on a mismatch |
| `--dry-run` | off | no restores, no writes, no deletes |
| `--no-delete` | off | keep cloud copies after downloading (fills the quota fast) |
| `--purge-trash` | off | allow emptying the whole trash if the quota is the blocker |
| `--no-sweep` | off | don't reclaim cloud copies an interrupted run left behind at startup |
| `--folder-map FILE` | none | `url,folder` mapping for lines without a folder column |
| `--default-folder NAME` | `(unfiled)` | folder for lines that carry no name |
| `--inventory-out FILE` | `inventory.csv` | where `--inventory` writes |
| `--log PATH` | `.pikpakget/grab-<date>.log` | `-` for stdout only |
| `--login USERNAME` / `--password-stdin` | — | sign in once; read the password from stdin instead of a prompt |
| `--yes` | off | skip unparsable lines in the links file instead of refusing to start |
| `--quiet` | off | hide info chatter, warnings still shown |

## What the quota actually behaves like

Measured on a free account, and worth knowing before you plan a large run:

- **Trash counts against the quota.** `usage_in_trash` is part of `usage`, so
  `batchTrash` alone frees nothing; only a permanent `batchDelete` gives the space
  back — and that lags a few seconds behind the call, which is why the tool polls
  `quota` before starting the next file.
- **A share can only be restored in pieces.** Restoring a folder id copies its
  whole subtree and fails when it does not fit, so files are restored one id at a
  time and the listing is walked first (listing is free and instant).
- **Files larger than the quota cannot be fetched at all.** A 7.1 GiB file does not fit
  a 6 GiB drive, and no tool can change that: it is marked `too_big`, skipped, and
  reported by `--inventory` as `unfetchable` rather than attempted and left half-restored.
- **Offline task slots are limited** (`quota.cloud_download`, 3 on free). Sequential
  single-file work stays well under it.
- **A daily downstream traffic cap bites before the storage quota does.** The free tier
  stops serving bytes at 20 GB/day, and the request that trips it arrives as an
  ordinary `HTTP 400` with an upsell body — it looks like a bad file but is a fact
  about the account. The tool names that error, ends the run on the first one, and
  gives back the attempt the file had just spent, so the next run has its full budget.
  Re-run the same command later: an hour is enough to be polite, past midnight is
  enough to be sure.
- **Throughput is shaped per account, not per connection.** A single connection
  measured 0.13–0.7 MiB/s over the course of a long run (it drifts down with time of
  day). Four ranged segments measured ~0.25 MiB/s in aggregate, with two of the four
  receiving *nothing* — so segmentation is worth ~1.5× at best and extra lanes are
  routinely starved. The default is 4 because that still beat one stream in these
  measurements, but `--connections 1` is the right answer if you would rather be
  left alone by the CDN.

When a round makes no progress, the tool probes the CDN with a one-byte range
request to tell the two failure modes apart, because the correct reaction is
opposite: a **refusal** (HTTP 4xx/5xx) is a policy signal, so the run drops to a
single connection and stays there; a **starved** lane (2xx, no bytes) is just
unlucky bandwidth shaping, so segments are retried with back-off instead of being
abandoned file after file.

Budget accordingly: the 405 GiB that one author's main folder holds costs roughly one to
four weeks of continuous running.

## Being a good citizen (avoiding risk control)

Nothing here is stealthy or intended to bypass billing: it is the same HTTP API and
the same public application client that the official mobile app and published
open-source SDKs use, and it still goes through the shield captcha the server asks
for. On top of that the tool deliberately slows itself down:

- when `--connections > 1`, lanes are opened one at a time (3 s apart) instead of
  in a burst, and a refusal downgrades the whole run to one connection;
  two files whose segments are merely *starved* also end segmentation for the run,
  because 60+300+900 s of back-off per file is slower than one honest connection;
- a refused or stalled segment is retried after 60 s → 300 s → 900 s, not instantly;
- `429/503/slow down` responses back off for 30 s → 900 s and drop the cached
  captcha tokens;
- three consecutive throttle-grade failures stop the whole run and tell you to come
  back in an hour, instead of hammering until the account gets flagged;
- a daily downstream traffic cap stops the run on the first occurrence — no back-off
  inside a run can clear a limit that resets per day, so retrying file after file
  would only spend attempts on a wall;
- `--gap` rests 20 s between files, and `--limit` / `--max-files` let you work in
  deliberate batches.

If you are rate limited, lower `--connections` to `1` and raise `--gap`.

## Deletes

The tool only deletes drive ids that it created itself (tracked in `state.json`,
seeded on reload for interrupted runs). `--purge-trash` is opt-in precisely because
emptying the trash is account-wide and cannot be undone; when it runs, the number
and size of what it is about to remove is logged first.

## File names

Everything from one link lands flat in `--dest/<folder>/`, where `<folder>` comes
from the links file. Remote names are somebody else's folder layout, so two files
in different sub-folders can share a name: the second is stored as
`<父目录名> - <文件名>`, then `... (2)`, and the choice is remembered in `state.json`
so a resumed run re-writes the same path instead of creating a second copy.

## Logging

The same lines printed to the terminal are appended to
`.pikpakget/grab-<date>.log`, because a multi-day run outlives the terminal.
`--log -` disables it. `.pikpakget/` also holds `state.json`, the session and the
device id, and is gitignored as a whole.

## Verification

Every file is checked against PikPak's own content hash before its `.part` is
renamed into place. That `hash` is *not* the file's SHA-1 — it is the SHA-1 of the
concatenated SHA-1s of each fixed-size block, and the block size is whatever the
uploader's client chose: a library of 26 files came back as 1 MiB (15), 2 MiB (8) and
512 KiB (1). `stream.verify_content` therefore tries a list of sizes, and the one that
worked is remembered and tried first for the next file.

This is the check that catches what a byte count cannot: two writers appending one
segment leave a file of exactly the right length with a shifted middle.

When no candidate size reproduces the hash the tool does not conclude the file is
bad — the rule is reverse engineered and the uploader's block size is unbounded. It
refetches while attempts remain, and on the last one it **keeps the bytes** as
`<name>.unverified`, records the file as unverified, and says so in the log. Delete
that file to ask for another fetch. `--verify` re-checks the whole library: a
quarantined file that now hashes out is moved back into place, and a healthy file that
no longer does is quarantined and put back in the queue, so re-running the download
command really is the fix. Once the fresh copy verifies, the superseded quarantined
bytes are deleted; until then the record points at both.

## Security notes

- `.pikpakget/` holds your session (`access_token`, single-use rotating
  `refresh_token`) and is in `.gitignore`; the session file is written `0600`.
  `--logout` deletes it.
- `CLIENT_ID` / `CLIENT_SECRET` in `pikpakget/api.py` are the public application
  constants of the official app (they ship in the client and in published SDKs).
  They are not your credentials, and no account data is sent anywhere except
  `user.mypikpak.com` / `api-drive.mypikpak.com`.
- No telemetry, no third-party HTTP client, no network calls beyond PikPak itself.

## Layout

```
pikpakget/api.py       HTTP client: session, captcha sign, share/drive/trash endpoints
pikpakget/stream.py    single resumable stream, ranged segments, the hash rule
pikpakget/pipeline.py  link parsing, state journal, quota logic, status/inventory/verify
pikpakget/cli.py       argument parsing, single-instance lock, signal handling
tests/test_pure.py     143 on the pure logic; no account, no network
tests/test_transfer.py   5 real transfers over local HTTP, with real curl
```

## Development

```bash
python3 -m unittest discover -s tests -t . -v
```

The tests deliberately avoid any account or share data; the fixtures are synthetic
ids. Please keep it that way if you add cases.

## Known limitations

Stated plainly, because each one has bitten someone at some point:

- **The hash rule is reverse engineered, not documented.** Verification trusts an
  inferred reading of PikPak's `hash` (SHA-1 over per-block SHA-1s, block size taken
  from a candidate list that has already had to grow once). A file no candidate
  reproduces costs up to three refetches before it is parked as `.unverified` — the
  bytes are never deleted over a guess, but a server-side hash that is simply stale
  costs that bandwidth. TLS covers transit; nothing covers a bad server-side copy that
  hashes to itself.
- **PikPak itself has only been talked to from macOS.** The byte transfer — four
  ranged `curl` segments, splicing, resume, the overshoot rule, the content hash —
  is covered by real downloads against a local Range server on every CI run, and CI is
  Linux (Python 3.10–3.14), so those paths hold on Linux too; but the API side
  (login, captcha, restore, quota) has only been exercised against the service from
  macOS. Windows is not supported: the CLI loads (`--version`, `--help`) and then
  refuses to start with an explanation, because the single-instance lock is POSIX
  `fcntl`.
- **The refusal path is untested in the wild.** Dropping to one connection after an
  HTTP 4xx/5xx is covered by unit tests, but everything observed so far was
  starvation (a lane that gets no bytes) rather than refusal, so that reaction is
  designed from the response shape rather than from experience. Starvation handling is
  exercised in practice: two starved files end segmentation for the rest of the run.
- **A single file larger than your cloud quota cannot be fetched**, by any tool:
  it has to fit in the drive before it can be downloaded. These are skipped and
  counted as `unfetchable` in `--inventory`.
- **Long-run stability is unproven.** Multi-day runs are the design target and the
  resume path is journalled, but the first real multi-day run is happening right now.
- **Speed figures here are one evening's observations in binary units (MiB/s)** and drift by time of day;
  treat them as orders of magnitude, not capacity.
- **Not published to PyPI.** Every release attaches a built wheel and sdist to
  its own assets (see `.github/workflows/release-assets.yml`), so `pip install
  <release asset url>` works; from a clone it is `pip install -e .`.

## Moving to another machine

`python3 -m pikpakget --doctor --dest <你打算存放的目录> --state-dir <你打算放状态的目录>`
answers "will this run here?" before you commit to a multi-day job: it checks the Python
floor, `curl` (only the segmented path needs it — `--connections 1` does not), whether
the interpreter's SHA-1 is usable (FIPS builds refuse it), that both directories exist
and are writable with enough free space, whether another instance holds the lock, how
long the session has left, and what the quota and any leftover cloud copies look like.
It needs no links file, takes no quota, and is safe to run while a download is in
flight. Exit code 1 means something has to be fixed first, so a cron wrapper can use it
as a gate.

## Platform notes

Written against macOS and uses `fcntl` for the single-instance lock and POSIX path
semantics; Windows refuses to run it with an explanation rather than an ImportError.
Long paths are clamped to 200 UTF-8 bytes per component to stay inside `NAME_MAX`.

**Case-folding volumes are handled.** macOS's default APFS and most SMB/CIFS mounts —
the usual way a NAS share reaches a desktop — treat `Movie.mp4` and `movie.mp4` as one
path, while ext4 treats them as two. The destination volume is probed once, and on a
folding volume a case variant of an already-chosen filename gets renamed like any other
collision instead of overwriting it.

## License

MIT
