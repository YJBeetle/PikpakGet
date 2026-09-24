# PikpakGet

Sequentially grab PikPak share links with a small cloud quota. Pure standard
library, no browser, no GUI automation, no official desktop client required.

The problem this solves: PikPak free accounts have **6 GB** of cloud space, and a
shared folder is routinely 100–40 000 GB. You cannot "save everything then download"
— so this tool works one *file* at a time:

```
restore one file into your drive  →  download it  →  verify the byte size
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
| `--limit N` / `--max-files N` | `0` (off) | stop after N links / N files |
| `--inventory` | off | size up every link (count, bytes, largest file), no downloads |
| `--dry-run` | off | no restores, no writes, no deletes |
| `--no-delete` | off | keep cloud copies after downloading (fills the quota fast) |
| `--purge-trash` | off | allow emptying the whole trash if the quota is the blocker |
| `--folder-map FILE` | none | `url,folder` mapping for lines without a folder column |

## What the quota actually behaves like

Measured on a free account, and worth knowing before you plan a large run:

- **Trash counts against the quota.** `usage_in_trash` is part of `usage`, so
  `batchTrash` alone frees nothing; only a permanent `batchDelete` gives the space
  back — and that lags a few seconds behind the call, which is why the tool polls
  `quota` before starting the next file.
- **A share can only be restored in pieces.** Restoring a folder id copies its
  whole subtree and fails when it does not fit, so files are restored one id at a
  time and the listing is walked first (listing is free and instant).
- **Files larger than the quota cannot be fetched at all.** A 7.6 GB file on a 6 GB
  drive has no path through this tool: it is marked `too_big`, skipped, and reported
  in `--inventory` as `unfetchable` rather than attempted and left half-restored.
- **Offline task slots are limited** (`quota.cloud_download`, 3 on free). Sequential
  single-file work stays well under it.
- **Throughput is shaped per account, not per connection.** A single connection
  measured 0.13–0.7 MB/s over the course of a long run (it drifts down with time of
  day). Four ranged segments measured ~0.25 MB/s in aggregate, with two of the four
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

Budget accordingly: 400 GB at 0.13–0.6 MB/s is roughly one to four weeks of
continuous running.

## Being a good citizen (avoiding risk control)

Nothing here is stealthy or intended to bypass billing: it is the same HTTP API and
the same public application client that the official mobile app and published
open-source SDKs use, and it still goes through the shield captcha the server asks
for. On top of that the tool deliberately slows itself down:

- when `--connections > 1`, lanes are opened one at a time (3 s apart) instead of
  in a burst, and a refusal downgrades the whole run to one connection;
- a refused or stalled segment is retried after 60 s → 300 s → 900 s, not instantly;
- `429/503/slow down` responses back off for 30 s → 900 s and drop the cached
  captcha tokens;
- three consecutive throttle-grade failures stop the whole run and tell you to come
  back in an hour, instead of hammering until the account gets flagged;
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

Downloaded files are verified by **byte count** against the drive listing. This is
a real limitation worth stating: PikPak's `hash` field is *not* the file's SHA-1
(measured: a downloaded file's SHA-1 and the reported `hash` differ), so there is no
upstream checksum to compare against. Integrity therefore rests on TLS plus the
exact size check, and `.part` files are renamed into place only after the full byte
count arrives.

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
pikpakget/stream.py    single resumable stream + ranged concurrent segments
pikpakget/pipeline.py  link parsing, state journal, quota logic, status/inventory
pikpakget/cli.py       argument parsing, single-instance lock, signal handling
tests/test_pure.py     42 tests on the pure logic; no account, no network
```

## Development

```bash
python3 -m unittest discover -s tests -t . -v
```

The tests deliberately avoid any account or share data; the fixtures are synthetic
ids. Please keep it that way if you add cases.

## Known limitations

Stated plainly, because each one has bitten someone at some point:

- **Integrity is byte count only.** PikPak exposes no trustworthy checksum for a
  downloaded file (see Verification), so a bit-flip that preserves length would go
  unnoticed. TLS covers transit; nothing covers a bad server-side copy.
- **macOS is the only platform this has been run on.** Linux is expected to work
  (`fcntl`, `curl`, POSIX paths) but is untested; Windows is not supported.
- **The refusal path is untested in the wild.** Dropping to one connection after an
  HTTP 4xx/5xx is covered by unit tests, but no real refusal has been observed yet,
  so the reaction is designed from the response shape rather than experience.
- **A single file larger than your cloud quota cannot be fetched**, by any tool:
  it has to fit in the drive before it can be downloaded. These are skipped and
  counted as `unfetchable` in `--inventory`.
- **Long-run stability is unproven.** Multi-day runs are the design target and the
  resume path is journalled, but the first real multi-day run is happening right now.
- **Speed figures here are one evening's observations** and drift by time of day;
  treat them as orders of magnitude, not capacity.
- **Not published to PyPI**; install from the repository (`pip install -e .`).

## Platform notes

Written against macOS and uses `fcntl` for the single-instance lock and POSIX path
semantics; it has not been run on Windows. Long paths are clamped to 200 UTF-8 bytes
per component to stay inside `NAME_MAX`.

## License

MIT
