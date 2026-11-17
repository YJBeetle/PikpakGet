# PikpakGet

English | [中文](README.cn.md)

PikpakGet downloads files from PikPak share links in sequence. It restores one file to an account's drive, downloads and checks it, removes the cloud copy, then moves to the next file. A share may be larger than the account's free drive space, but **each individual file must still fit**.

You can save multiple accounts. When the server reports that one account reached its downstream traffic limit, the tool records the time and tries another account that is not locked by a local process. That account becomes eligible again after one hour. Downloads remain sequential; it does not download through several accounts at once.

## Install

Requires Python 3.10+ on macOS or Linux. The default single connection uses no extra package. Segmented downloads with `--connections` require `curl`.

```bash
git clone https://github.com/YJBeetle/PikpakGet.git
cd PikpakGet
python3 -m pikpakget --help
# Or install the command: python3 -m pip install -e .
```

## Download a share

```bash
# Sign in to one or more accounts. Sessions are separate; passwords are not stored.
python3 -m pikpakget --login first@example.com
python3 -m pikpakget --login second@example.com
python3 -m pikpakget --accounts

# Inspect the plan, then download.
python3 -m pikpakget links.txt --dry-run --dest /path/to/library
python3 -m pikpakget links.txt --dest /path/to/library

# Process several link files in argument order, then line order.
python3 -m pikpakget catalogue/嗷大喵.txt catalogue/FXX.txt --dest /path/to/library

# Pass share URLs directly, mixed with link files.
python3 -m pikpakget https://mypikpak.com/s/SHARE_ID --dest /path/to/library
```

Put one share link on each line of `links.txt`. Blank lines and lines starting with `#` are ignored. Add a folder name after a tab to choose where the link's files land locally:

```text
https://mypikpak.com/s/SHARE_ID_1	Movies
https://mypikpak.com/s/SHARE_ID_2	Series
```

Links without a folder name default to their input file's name without its extension. For example, unnamed rows in `Studio.txt` go to `DIR/Studio/`. Folder selection uses this priority: the name after the row's tab, a `--folder-map` entry, an explicit `--default-folder`, then the input file's name. Each input file gets its own fallback folder. `--limit` applies to the first N links across all input files.

Positional arguments may also be share URLs directly, merged with link files in argument order, with the same `?password=` and tab-folder syntax. A direct URL has no input file name to fall back to, so its final fallback is the fixed folder `未分组`.

A link may carry a folder path (`/s/<share id>/<folder id>/…`). Such a link lists and downloads only that folder's contents, treating the linked folder as the root: its own name adds no extra level, and deeper subfolders are kept.

The local library keeps the share's folder tree beneath the chosen series folder. For example, a file in the share's `Studio/Unnumbered` folders goes to `DIR/Series/Studio/Unnumbered/`. A share containing one root file, or several root files without a folder, puts those files directly in `DIR/Series/`. The share title is not used as a directory name. Empty share folders are created, too. The tool does not move files from the previous flat layout; if an old path differs from the new path, it downloads to the new location.

The cloud staging folder is `Pack From Shared`. At the start of a download, the tool lists leftover items and asks whether to clear them; it asks again if a later file cannot fit. The prompt is `[Y/n]`: Enter, `y`, or `yes` (case-insensitive) permanently deletes all listed items, including those with unknown ownership. Entering `n`, other text, or interrupting keeps the items. `-y` skips the prompt and confirms the same full cleanup. Without an interactive terminal or `-y`, items are kept. If a file still cannot fit and the folder is empty or clearing it does not free enough space, that file is marked failed. Cleanup is limited to `Pack From Shared`.

## Common commands

| Command | Purpose |
|---|---|
| `python3 -m pikpakget --accounts` | List accounts in selection order with local usage and traffic cooldown |
| `python3 -m pikpakget --logout --account NAME` | Remove one account's local session and device ID |
| `python3 -m pikpakget --whoami --account NAME` | Show drive usage and subscription expiry |
| `python3 -m pikpakget links.txt --inventory` | Count files, total bytes, and files too large for the drive |
| `python3 -m pikpakget links.txt --dry-run` | List the download plan without restores, downloads, or cloud cleanup |
| `python3 -m pikpakget --status` | Show local progress for this library, even while an account is busy |
| `python3 -m pikpakget links.txt --verify` | Hash-check existing files while downloading |
| `python3 -m pikpakget links.txt --verify --dry-run` | Audit existing files without downloads or changes |
| `python3 -m pikpakget --doctor` | Check directories, locks, session, and drive state |

| Option | Default | Effect |
|---|---|---|
| `--dest DIR` | `~/Downloads/PikPak` | Local library; share folders are kept beneath `DIR/<series>/` |
| `--set-config dest=DIR` | unset | Remember a default library; `dest=` clears it |
| `--account NAME` | automatic | Use only this account, by label or the ID shown by `--accounts`; wait an hour after a traffic cap before retrying |
| `--connections N` | `1` | Connections per file; values above 1 use `curl` segments |
| `--max-files N` | unlimited | Limit file processing; failed attempts may also use a slot |
| `--skip-ext EXT[,EXT...]` | empty | Skip files with matching extensions (case-insensitive, optional leading dot, e.g. `txt,.nfo`); not restored, downloaded, or counted |
| `--limit N` | unlimited | Process only the first N links |
| `--repeat N` | `3` | Maximum passes, including the first; zero-progress passes stop early unless a temporary request failed; 0 means one pass |
| `--gap SEC` | `20` | Seconds to wait between files |
| `--no-delete` | off | Keep this run's cloud copies; low space may later prompt to clear them |
| `--log -` | log file | Print only to the terminal |
| `-v` | off | Show stage detail (restore, verify, cleanup) at debug level |
| `-y` | off | Skip the cleanup prompt; delete all `Pack From Shared` items, including unknown ones |
| `--skip-bad-lines` | off | Ignore unparsable link lines |

See `python3 -m pikpakget --help` for all options.

`--skip-ext` applies at listing time: `--dry-run`, `--inventory`, `--verify` audits, and downloads all plan against the filtered list. Skipped files use no `--max-files` slot and do not keep a link unfinished. Existing local files with a skipped extension are left untouched.

In a terminal, downloads show a single-file progress bar with bytes, recent speed, and estimated time remaining, refreshed every second. Speed reflects the last few seconds of throughput; the ETA uses the last minute's average speed, so brief dips do not double the remaining time. Each finished file logs one summary line (duration, average speed, hash piece); stage detail (restore, verification, cleanup) is hidden by default and shown with `-v`; quota waits are reported only past 30s or on timeout. Redirected output uses plain progress lines about every 30 seconds; in terminal mode the log file still gets a plain progress line every 5 minutes. `--quiet` hides progress. Log files contain no terminal redraw codes.

## Progress and accounts

Each series keeps its progress, lock, and logs in `DIR/<series>/.pikpakget/`. Only one process can write to a series at a time. Different series can run concurrently, while the local account lock still gives each account to one process. Local paths in the journal are relative to the series root, so the whole series can move to another download root. Account data stays on the computer running the tool:

```text
~/.pikpakget/
├── accounts.json                  # labels and selection order
└── accounts/<account ID>/
    ├── device_id                 # specific to this account
    ├── session.json              # login tokens
    ├── lock                      # local account lock
    └── traffic_capped_at         # latest confirmed downstream cap time, if any
```

The account ID is derived from the server user ID. Signing in again to the same account keeps its device ID. Account locks only coordinate processes on the same computer; they cannot coordinate another computer using the same account or cloud folder.

`--accounts` works offline while downloads are running. It shows whether the local account lock is held, plus any recorded traffic cooldown with its remaining duration and retry time. Expired cooldowns are shown as eligible for retry; unreadable states are shown as unknown without hiding other accounts. This is a local snapshot, not a login check or a measurement of remaining server traffic. Cooldown expiry does not guarantee that traffic has recovered.

Rerun the same command to continue unfinished files. Completed shares are listed again so newly added files can be found. If the server reports a downstream traffic cap, the tool tries the next available account. If all accounts are cooling, it waits for the earliest one-hour retry time; another cap restarts that account's timer. An hour is a retry interval, not a guarantee that the server quota has reset. Ctrl+C interrupts the wait. If all accounts are occupied by local processes, it reports “no available account.” Session and workspace failures do not start traffic cooldowns.

## Verification and limits

Network failures share one transient-error policy and do not imply invalid credentials, an unavailable share, or an account traffic cap. Replay-safe API requests, including refresh, shield credentials, listing and cleanup by ID, make at most five attempts with interruptible backoff. Exhaustion in a nested call propagates without multiplying retries. Interrupted or short downloads keep their fragments and identified cloud copies for later passes without spending the file failure budget. Temporary failures during setup, listing, downloading or cleanup can use the remaining `--repeat` passes even if no file completed; the default is three passes. Exhaustion still exits with progress preserved. The zero-progress guard remains for ordinary logic failures.

If a cloud write such as a restore or folder creation loses its reply after submission, it may already have taken effect. The tool reports an unknown write outcome and stops replaying it; the next run checks the cloud state. Disk errors, explicit credential rejection and confirmed downstream caps remain separate from ordinary network retries.

Downloads go to a `.part` file first. When complete, the tool tries to verify them against the share's content hash. The hash's block format is inferred from samples; failure to match a known block size does not prove that the bytes are corrupt. After retries are exhausted, the bytes are kept as `.unverified` and are not treated as verified.

The `.part` file and any segment fragments sit beside the destination file. A matching file progress record allows a later run to resume them. If `.pikpakget` is lost, or a file has no progress record, the tool deletes that file's fragments when it reaches the file and downloads it from the beginning. It does not scan or clear fragments belonging to other files.

On a new scan, an existing file is accepted by its relative path and exact size without another download, including when the series journal was lost. This fast check can accept different bytes of the same length. Add `--verify` to check existing files against the share's content hash; a mismatch preserves the old file under a conflict name and downloads a new copy. Files verified before are not hashed again while the source hash, size and mtime all agree, and a freshly verified download records that state on the spot. When the share supplies no hash, the tool warns and falls back to path and size. `--verify --dry-run` scans and reports missing files, wrong sizes, and hash mismatches without downloading, renaming, or cloud cleanup. Newly downloaded files still receive their usual hash check. The previous standalone `--verify` audit mode has been removed.

The old `DIR/.pikpakget/` journal is not read or migrated. Move existing downloads into their series folders yourself; a later run will recognize them using the rules above.

The current journal format is v5. Each share URL identifies a task within its series; file records refer to it through `link_key`, without appending the series name. Older journals are rejected without migration. Move the old `state.json` aside before rerunning. Completed files can be recognized again; fragments without a matching progress record are discarded when their file is processed.

- A single file larger than the account's available cloud space is skipped.
- One connection is the default; segments can be slower when the server limits throughput.
- PikPak's API, quotas, and rate controls may change. Account rotation cannot guarantee that every account will download successfully.
- If the service returns `PROHIBITED` (for example, because sharing is unavailable in the current region), the tool reports an error instead of marking an empty listing complete.
- Native Windows lacks the POSIX file locks used here. Use Linux, macOS, or WSL.
- Tests cover local logic and downloads from a local HTTP server. Multi-account cloud cleanup and rotation have not yet had an end-to-end test against real accounts.

Logs and progress files contain share links, filenames, and local paths. Session files contain usable login tokens. Do not upload them unchanged. See [SECURITY.md](SECURITY.md).

## Development

```bash
python3 -m unittest discover -s tests -t . -v
```

Licensed under MIT; see [LICENSE](LICENSE).
