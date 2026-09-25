# Changelog

This project aims to follow Keep a Changelog and SemVer. Below 1.0.0 the command
line and module APIs may change between releases.

## Unreleased

### Added
- **`--doctor`**, a preflight for a machine nobody has tested on: the Python floor,
  `curl` (only the segmented path needs it), whether the interpreter allows SHA-1 at all
  (FIPS builds refuse it), the destination and state directories (existence, writability,
  free space, case folding), whether another instance holds the lock — probed with a real
  non-blocking `flock`, since a dead process provably holds none — how long the session
  has left, the quota, and any cloud copies that this tool's state does not account for.
  It needs no links file, spends no quota, is safe to run during a download, and exits 1
  when something blocks a long run.

### Fixed
- **`--doctor` crashed on the exact problem it exists to report.** The first real
  invocation pointed `--dest`/`--state-dir` at a path that could not be created, and the
  startup path raised `PermissionError` from `os.makedirs` — twice over, since the state
  directory is created by the CLI, by `Client` for the device id, and by `run()` for the
  destination. All three now report one clear line and exit (1 for the preflight, 2 for a
  run); the preflight additionally creates a directory a run would have created anyway,
  and says it did.
- **Constructing the pipeline died on any state that had finished files.** The
  case-folding flag was initialised after the loop that reads it, so a resumed run —
  i.e. every real run after the first — raised `AttributeError` before doing anything.
  No fixture had a state with a finished file in it; now one does.

### Fixed
- **Two files whose names differed only by letter case could overwrite each other.**
  macOS's default APFS and most SMB/CIFS mounts fold case in filenames, so
  `Movie.mp4` and `movie.mp4` are one path there while ext4 sees two, and the
  destination-name dedupe compared them exactly: the second download replaced the
  first and state recorded both as finished. The destination volume is probed once and
  case variants now collide the way identical names already did.

### Added
- **The daily 20 GB downstream traffic cap is recognised.** It arrives as an ordinary
  `HTTP 400` with an upsell body (`error_code` 3, "Today's downstream traffic 20.1 G
  has exceeded the limit 20 G"), which nothing here classified as anything but a failed
  file — observed ending a run at file 40/238 after every remaining file's download
  request failed the same way. `api.TrafficCapped` now names it, the first occurrence
  stops the round with advice to re-run later, and the file's attempt is handed back so
  its budget is full next time.

### Fixed
- **"No candidate block size reproduced the hash" no longer means "the file is
  corrupt".** The rule is reverse engineered and the uploader's block size has no
  documented set — a library of 26 files used three of them. A file that matches none
  is now refetched while attempts remain, and on the last attempt the bytes are kept
  as `<name>.unverified` and recorded as unverified instead of being deleted. The run
  reports it, `--status` counts it, and deleting the quarantined file is what asks for
  another fetch.
- **`--verify` now fixes what it finds.** It previously reported a suspect file and
  left the record at `done`, so the next download run skipped it and the only repair
  path was editing `state.json` by hand. A suspect file is now quarantined and put back
  in the queue, and a quarantined file that hashes out at a later sweep is moved back
  under its original name. Every path keeps the doomed bytes referenced by the record
  (they were being renamed and then dropped, which let a second sweep report "all
  green" over an orphan nobody could trace), and a copy that verifies replaces the
  quarantined one it superseded.
- **One unreadable share ended the whole `--verify` sweep.** A link whose listing
  fails is counted and skipped now, so the rest of the library still gets checked.
- **A 5xx from the auth endpoint was treated as a dead session**, which stopped a
  multi-day run and told the user to re-login over a server-side hiccup. Only a 4xx
  refusal marks the session dead now.
- `hashlib.sha1` is requested with `usedforsecurity=False` where the interpreter
  supports it, so a FIPS build of Python can still compute the content hash.

### Changed
- The block size that verified a file is remembered per file *and* used first for the
  next file in the run: one share is usually one client with one size, and trying the
  list from 1 MiB every time costs a full extra pass over 500 MiB.
- `README.md`'s flow diagram had a duplicated step, and the block-size figures claimed
  one size that turned out to be three.
- The three comments about segments written past their range now state the rule in one
  place, together with the two guards (`reap` waits, `--max-filesize`) that keep it
  from recurring.

## 0.1.3

### Added
- **Downloads are now verified by content hash, not just by byte count.** PikPak's
  `hash` field was reverse engineered from real files: it is the SHA-1 of the
  concatenated SHA-1s of each fixed-size block, the block size being whatever the
  uploader's client used (1 MiB on most files here, 512 KiB on another). Every file is
  folded and compared before its `.part` is renamed into place, and a file that no
  candidate block size reproduces is deleted and re-fetched.
- `--verify` re-checks every file the state records as downloaded against the hash the
  share still reports. It deletes nothing and exits 1 when something is off.

### Fixed
- **A segment written past its own range was truncated and trusted, which is how a
  file lands with the right size and a shifted middle.** Two writers on one segment
  leave a duplicated stretch followed by content that is short by exactly the
  overlap — so the total comes out correct and every length check passes. Such a
  segment is now discarded and refetched whole. Found by auditing an already
  downloaded library: one 399 MB file matched the server's bytes in 7 of 9 sampled
  windows and not in the other two, all inside one segment.

## 0.1.2

### Fixed
- **A link could be declared finished while files were still missing.** 0.1.1 started
  keying links by URL *and* destination folder but kept reading progress records
  written under the old bare-URL key, so the end-of-link check found nothing pending
  and a link with two files left on the cloud was marked done and skipped for good.
  The state file now carries a version, v1 state is migrated once on load, and pending
  work is decided from the ids in the current listing rather than from any key.
- **One revoked share stopped a run that had days of scheduling left.** PikPak answers
  403 both for a dead session and for a single share that has since been removed, and
  the two are indistinguishable at the call site; the first one used to end everything.
  Only a session the server refuses to refresh stops the run now — a plain 403 is
  counted, skips that link, and a listing that answers clears the suspicion. A socket
  error during a refresh no longer looks like a dead session either.
- **Names containing an inner pair of dots were rewritten** (`movie..2.mp4` came out as
  `movie._2.mp4`) to guard against traversal that is already impossible once separators
  are flattened. Only a name that *is* `.` or `..` — the directory itself — is neutralised.
- **Ctrl-C could still wait minutes on the single-stream path**, because the socket
  timeout that bounds a stalled read was 300 s. It now shares the stall window the
  segmented lanes use.

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
