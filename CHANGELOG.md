# Changelog

Changes since each release are listed here. Before 1.0, command and state formats may change between releases.

## Unreleased

- Smooth the download ETA over a 60-second average-speed window while the displayed speed keeps the recent-seconds window, so brief throughput dips no longer double the remaining time.

- Decode the obfuscated folder ids carried by share URL paths (XOR against the share id, mirroring the web app) before listing the subtree; the raw path ids previously failed with 404.

- Ctrl+C now ends network back-off waits and in-flight API requests promptly: the retry ladder and restore polling observe the stop flag, and each request has a 120s total wall-clock deadline, so a drip-feeding connection can no longer stall a run for minutes.

- Share links with a folder path (`/s/<share id>/<folder id>/…`) now list and download only that subtree, treating the linked folder as the root. Plain links keep whole-share behavior.

- Accept share URLs directly as positional arguments, mixed with link files in argument order, with the same `?password=` and tab-folder syntax. Direct URLs have no input file name, so their folder falls back to `--default-folder` or the fixed `未分组` directory.

- Add `--skip-ext` to skip files by extension (case-insensitive, comma-separated, optional leading dot) at listing time. Dry runs, inventories, audits and downloads all plan against the filtered list; skipped files consume no file budget and do not keep a link unfinished.

- Use share URLs as task keys inside each series journal, without the redundant series name. Rename file records' `url` field to `link_key` and bump the journal to v5; older journals are rejected without migration.

- Default to at most three download passes, including the first. A pass with no completed files still stops retries early; explicit `--repeat 0` keeps the single-pass behavior.

- Default links without an explicit or mapped folder to their input text file's name without the extension. `--default-folder` can override this fallback.

- Show live per-file transfer progress, speed and ETA for single-stream and segmented downloads. Report processing stages and use sparse plain-text progress when output is redirected.

- Accept multiple link text files in one command and process links in argument order, then line order. Preserve each link's chosen series directory and apply `--limit` to the combined list.

- Discard a file's `.part` and segment fragments before downloading when no matching file progress record exists. Other fragments remain untouched.

- Store progress, logs and locks under each series directory, with relative local paths. The old root journal is no longer read or migrated.
- Adopt existing local files by relative path and exact size. `--verify` additionally checks available content hashes during downloads; `--verify --dry-run` performs a read-only audit. Removed the old standalone `--verify` mode.

- Persist each account's confirmed downstream traffic cap time and retry after one hour. If all accounts are cooling, wait for the earliest retry; Ctrl+C interrupts the wait.
- Avoid a 150-second quota wait when a newly restored file was not yet included in the cloud usage reading before deletion.
- At download startup, offer to clear leftover items in the selected account's `Pack From Shared` folder, with explicit confirmation.
- Respond to Ctrl+C during the between-file delay without starting another restore. Keep the current cloud copy and `.part` on an interrupted transfer, and skip quota polling and end-of-round cloud requests while exiting.
- Report `--status` against the last full share inventory instead of only the file records written so far; find `.part` downloads inside nested folders. Make `--doctor` inspect `Pack From Shared` and describe the on-demand cleanup rule.
- Keep the shared folder tree in the local library, including empty folders. Shares with only root files still put them directly in the selected series folder. Existing files in the previous flat layout are left untouched.
- Added multiple accounts with separate sessions and device IDs. A download uses one unlocked account at a time and tries another when the current account reaches its downstream traffic limit.
- Restored files use the selected account's cloud `Pack From Shared` folder. Only when cloud space is insufficient does the tool offer to clear that folder; it waits for explicit confirmation and never clears other cloud folders or the account trash.
- Removed old single-account state migration and the `--no-sweep` option. Old state formats are rejected with an error.
- Changed the default to one download connection. `--repeat 0` now means one pass; unfinished work produces a nonzero exit status.
- Fixed cases where a dry run changed cloud files, a truncated share listing was marked complete, or the same share in two local folders shared one progress record.
- Paginate files directly in a share's root, so a share with more than one root page is fully listed.
- Reject a missing or empty `--folder-map` with a useful error instead of ignoring it or crashing.
- Read `--status` from the local library without acquiring an account lock or contacting the cloud.
- Use an empty parent ID when creating a cloud root folder, avoiding "Parent folder is not found" on a new account.
- Treat non-OK share states such as `PROHIBITED` and unexpectedly empty listings as errors, so a run cannot report unavailable shares as completed downloads.
- Identify newly restored files by ID in `Pack From Shared`, and avoid blind retries of a restore request whose result is unknown.
- Added `--doctor`, clearer status and error output, and local HTTP transfer tests.

## 0.1.3

- Added content-hash verification and `--verify` for downloaded files.
- Discarded segments that exceeded their assigned byte range instead of trusting a file with the right total length but shifted content.

## 0.1.2

- Corrected completion tracking so missing files do not make a share appear finished.
- Kept an inaccessible share from stopping unrelated links.
- Improved path sanitization and single-stream interruption handling.

## 0.1.1

- Preserved user files when a download name conflicts with them.
- Made missing local downloads retryable instead of crashing the run.
- Improved retry, session-error, and interrupt handling.
- Added real transfer tests with a local HTTP server.

## 0.1.0

- Initial CLI: sign in, inspect shares, restore and download one file at a time, resume partial transfers, and track progress.
- Added folder mapping, optional segmented downloads, status, and logging.
