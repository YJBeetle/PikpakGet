# Changelog

This project aims to follow Keep a Changelog and SemVer. Below 1.0.0 the command
line and module APIs may change between releases.

## 0.1.1

Fixes from a line-by-line review of the code shipped in 0.1.0. Four of them were
found in code paths the test suite did not cover, so the main download loop now has a
test harness of its own.

### Fixed
- **A file you put in the library yourself could be deleted without asking.** If a
  share contained the same filename with a different size, `fetch_to` removed your
  copy and wrote the download over it, leaving one warn line in the log. Anything the
  tool did not download is now renamed to `<name>.conflict-<timestamp>` and kept.
- **A moved download crashed every later run.** The `state == downloaded` branch
  called `os.path.getsize` on a path that the user is free to move away (that is the
  point of a library), raising `FileNotFoundError` past `run_link`/`run`/`main` and
  wedging on the same record forever. Size lookup is now total and the record simply
  falls back to being downloaded again.
- **The startup sweep waited for the wrong thing.** It passed the reclaimed byte count
  as the "usage before the delete" baseline, making the floor 0: with any other file in
  the drive, every startup polled to the 150 s timeout and logged a false
  "space did not come back" warning. The baseline is now read before the delete.
- **Three different failures were treated as rate limiting.** An expired CDN link
  (`下载被拒: HTTP 404`) and an expired session (403) both counted towards the
  "three consecutive throttles stop the run" rule, and the counter never reset, so the
  documented behaviour was really "three times per run". Rate limit, refused lane and
  dead session are now distinguished, an expired session says to re-run `--login`, and
  a completed file resets the counter.
- **Ctrl-C did not interrupt a single-stream download.** `download_segments` polls for
  a stop every 2 s; `download_stream` never checked, even though `--connections 1` is
  what README recommends and what the tool falls back to after a refusal or starvation.
  For a 7 GiB file, "finishing the current file" meant hours.
- **The same share could only be filed into one folder.** Link state was keyed by URL
  alone, so listing one share twice with two destination folders silently skipped the
  second. Links are now keyed by URL plus folder, and unfinished work is measured
  against this run's list rather than every link state has ever seen (which turned a
  fully successful run into an extra pass plus exit code 1).
- **`attempts` accumulated for the life of a file**, so three transient hiccups over
  any number of runs made later runs give up on the first glitch. Failed and retried
  files now start each run with a fresh budget; completed and over-quota records are
  untouched.
- **A capped share listing was silent.** `walk_share` stopped at `max_nodes` without a
  word, so `--inventory` under-reported and a link could still be marked done. A
  `truncated` flag is now returned, logged, and stored on the link.
- **The session file had a readable window.** It was created under the default umask
  and chmodded afterwards; it is now created `0600` and stays that way across the
  atomic replace.
- **Dotfiles were renamed by the sanitizer.** `safe_name` stripped leading dots, so
  `.bashrc` lost its dot. Path traversal is still broken up at any position, and
  leading dots are preserved.
- `--quiet` no longer prints info chatter (warnings still do), and the corrupt-state
  notice goes to stderr so `--status` output stays machine-readable.
- Stopped tracking `pikpakget.egg-info/`, and fixed ignore patterns that never matched
  the real `<file>.part.segs4` segment directories.

### Added
- Test harness that drives the real `run_link` loop with the network and cloud cleanup
  replaced, plus regression tests for every fix above (91 tests).
- CI gained a pyflakes gate (zero warnings in the package), a `pip install -e .` plus
  console-script smoke test, and Python 3.14 in the matrix.
- README: an options table that actually lists every flag, and a corrections pass on
  two English sentences broken by the unit switch.

## 0.1.0

First usable release, validated by running continuously against a real account.

### Added
- Same HTTP API the official desktop client uses: standard library only, no browser,
  no GUI automation, no desktop client required.
- One password sign-in creates an independently refreshable session (`--login`, stored
  `0600`, `--logout` removes it).
- One file at a time: restore, download, verify the byte count, permanently delete the
  cloud copy, wait for the quota to come back, continue. This is the only shape that
  works under a 6 GiB quota.
- Read-only `--inventory` to size links up without touching cloud space.
- Resumable by design: state is journalled before every side effect, so `Ctrl-C` or a
  reboot costs at most the file in flight, and re-running resumes instead of duplicating.
- Folder-map and collision-safe naming inside `--dest/<folder>/`.
- Segmented downloading (`--connections`), sublinear in practice because the account
  is shaped; refuses downgrade it to one connection.
- `--status`, single-instance lock, log file, and unit labels in binary (GiB).

### Known limitations
- Integrity is byte count only; PikPak's `hash` field is not a file SHA-1.
- Only macOS has run the real download path; Windows is not supported.
- The refusal downgrade path is unit tested but has not been observed in the wild.
- Files larger than the cloud quota cannot be fetched at all.
- Multi-day stability was unproven at release time.
