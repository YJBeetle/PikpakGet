# PikpakGet

English | [中文](README.cn.md)

PikpakGet downloads files from PikPak share links in sequence. It restores one file to an account's drive, downloads and checks it, removes the cloud copy, then moves to the next file. A share may be larger than the account's free drive space, but **each individual file must still fit**.

You can save multiple accounts. When one account reaches its downstream traffic limit, the tool tries another account that is not locked by a local process. Downloads remain sequential; it does not download through several accounts at once.

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

**Before the first download, reserve the `.pikpakget` folder at the root of each account's cloud drive for this tool.** Whenever a download run starts using an account, it permanently deletes everything inside that account's folder. `--dry-run`, `--inventory`, `--status`, `--verify`, and `--doctor` do not perform this cleanup. `--purge-trash` separately allows the tool to empty the account's entire trash when space is insufficient.

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
| `--dest DIR` | `~/Downloads/PikPak` | Local library; each link lands in `DIR/<folder>/` |
| `--set-config dest=DIR` | unset | Remember a default library; `dest=` clears it |
| `--account NAME` | automatic | Use only this account, by label or the ID shown by `--accounts` |
| `--connections N` | `1` | Connections per file; values above 1 use `curl` segments |
| `--max-files N` | unlimited | Limit file processing; failed attempts may also use a slot |
| `--limit N` | unlimited | Process only the first N links |
| `--repeat N` | `0` | Maximum list passes; 0 means one pass |
| `--gap SEC` | `20` | Seconds to wait between files |
| `--no-delete` | off | Keep this run's cloud copies; the next download run still clears the staging folder |
| `--purge-trash` | off | Allow emptying the entire account trash when space runs out |
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
    └── lock                      # local account lock
```

The account ID is derived from the server user ID. Signing in again to the same account keeps its device ID. Changing `--dest` selects a separate progress journal. Account locks only coordinate processes on the same computer; they cannot coordinate another computer using the same account or cloud folder.

Rerun the same command to continue unfinished files. Completed shares are listed again so newly added files can be found. If an account reaches its daily downstream limit, the tool tries the next available account. If all accounts are occupied by local processes, it reports “no available account.”

## Verification and limits

Downloads go to a `.part` file first. When complete, the tool tries to verify them against the share's content hash. The hash's block format is inferred from samples; failure to match a known block size does not prove that the bytes are corrupt. After retries are exhausted, the bytes are kept as `.unverified` and are not treated as verified.

- A single file larger than the account's available cloud space is skipped.
- One connection is the default; segments can be slower when the server limits throughput.
- PikPak's API, quotas, and rate controls may change. Account rotation cannot guarantee that every account will download successfully.
- Native Windows lacks the POSIX file locks used here. Use Linux, macOS, or WSL.
- Tests cover local logic and downloads from a local HTTP server. Multi-account cloud cleanup and rotation have not yet had an end-to-end test against real accounts.

Logs and progress files contain share links, filenames, and local paths. Session files contain usable login tokens. Do not upload them unchanged. See [SECURITY.md](SECURITY.md).

## Development

```bash
python3 -m unittest discover -s tests -t . -v
```

Licensed under MIT; see [LICENSE](LICENSE).
