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
```

Put one share link on each line of `links.txt`. Blank lines and lines starting with `#` are ignored. Add a folder name after a tab to choose where the link's files land locally:

```text
https://mypikpak.com/s/SHARE_ID_1	Movies
https://mypikpak.com/s/SHARE_ID_2	Series
```

Links without a folder name go to `(unfiled)`. You can also pass a CSV or TSV file containing `url,folder` pairs with `--folder-map`.

The local library keeps the share's folder tree beneath the chosen series folder. For example, a file in the share's `Studio/Unnumbered` folders goes to `DIR/Series/Studio/Unnumbered/`. A share containing one root file, or several root files without a folder, puts those files directly in `DIR/Series/`. The share title is not used as a directory name. Empty share folders are created, too. The tool does not move files from the previous flat layout; if an old path differs from the new path, it downloads to the new location.

The cloud staging folder is `Pack From Shared`. At the start of a download, the tool lists any leftover items there and asks whether to clear them. It asks again if a later file cannot fit in the available cloud space. Only typing `y` (or `Y`) permanently deletes them; Enter, `n`, declining, or running without an interactive terminal keeps them and continues. If a file still cannot fit and the folder is empty or clearing it does not free enough space, that file is marked failed. Cleanup is limited to `Pack From Shared`.

## Common commands

| Command | Purpose |
|---|---|
| `python3 -m pikpakget --accounts` | List accounts in selection order |
| `python3 -m pikpakget --logout --account NAME` | Remove one account's local session and device ID |
| `python3 -m pikpakget --whoami --account NAME` | Show drive usage and subscription expiry |
| `python3 -m pikpakget links.txt --inventory` | Count files, total bytes, and files too large for the drive |
| `python3 -m pikpakget links.txt --dry-run` | List the download plan without restores, downloads, or cloud cleanup |
| `python3 -m pikpakget --status` | Show local progress for this library, even while an account is busy |
| `python3 -m pikpakget links.txt --verify` | Recheck downloaded files against their content hashes |
| `python3 -m pikpakget --doctor` | Check directories, locks, session, and drive state |

| Option | Default | Effect |
|---|---|---|
| `--dest DIR` | `~/Downloads/PikPak` | Local library; share folders are kept beneath `DIR/<series>/` |
| `--set-config dest=DIR` | unset | Remember a default library; `dest=` clears it |
| `--account NAME` | automatic | Use only this account, by label or the ID shown by `--accounts`; wait an hour after a traffic cap before retrying |
| `--connections N` | `1` | Connections per file; values above 1 use `curl` segments |
| `--max-files N` | unlimited | Limit file processing; failed attempts may also use a slot |
| `--limit N` | unlimited | Process only the first N links |
| `--repeat N` | `0` | Maximum list passes; 0 means one pass |
| `--gap SEC` | `20` | Seconds to wait between files |
| `--no-delete` | off | Keep this run's cloud copies; low space may later prompt to clear them |
| `--log -` | log file | Print only to the terminal |

See `python3 -m pikpakget --help` for all options.

## Progress and accounts

A library keeps its progress, lock, and logs in `DIR/.pikpakget/`. Only one download process can write to a library at a time. Account data stays on the computer running the tool:

```text
~/.pikpakget/
├── accounts.json                  # labels and selection order
└── accounts/<account ID>/
    ├── device_id                 # specific to this account
    ├── session.json              # login tokens
    ├── lock                      # local account lock
    └── traffic_capped_at         # latest confirmed downstream cap time, if any
```

The account ID is derived from the server user ID. Signing in again to the same account keeps its device ID. Changing `--dest` selects a separate progress journal. Account locks only coordinate processes on the same computer; they cannot coordinate another computer using the same account or cloud folder.

Rerun the same command to continue unfinished files. Completed shares are listed again so newly added files can be found. If the server reports a downstream traffic cap, the tool tries the next available account. If all accounts are cooling, it waits for the earliest one-hour retry time; another cap restarts that account's timer. An hour is a retry interval, not a guarantee that the server quota has reset. Ctrl+C interrupts the wait. If all accounts are occupied by local processes, it reports “no available account.” Session and workspace failures do not start traffic cooldowns.

## Verification and limits

Downloads go to a `.part` file first. When complete, the tool tries to verify them against the share's content hash. The hash's block format is inferred from samples; failure to match a known block size does not prove that the bytes are corrupt. After retries are exhausted, the bytes are kept as `.unverified` and are not treated as verified.

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
