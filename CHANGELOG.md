# Changelog

Changes since each release are listed here. Before 1.0, command and state formats may change between releases.

## Unreleased

- Avoid a 150-second quota wait when a newly restored file was not yet included in the cloud usage reading before deletion.
- At download startup, offer to clear leftover items in the selected account's `Pack From Shared` folder, with explicit confirmation.
- Respond to Ctrl+C during the between-file delay without starting another restore. Keep the current cloud copy and `.part` on an interrupted transfer, and skip quota polling and end-of-round cloud requests while exiting.
- Report `--status` against the last full share inventory instead of only the file records written so far; find `.part` downloads inside nested folders. Make `--doctor` inspect `Pack From Shared` and describe the on-demand cleanup rule.
- Keep the shared folder tree in the local library, including empty folders. Shares with only root files still put them directly in the selected series folder. Existing files in the previous flat layout are left untouched.
- Added multiple accounts with separate sessions and device IDs. A download uses one unlocked account at a time and tries another when the current account reaches its downstream traffic limit.
- Restored files use the selected account's cloud `Pack From Shared` folder. Only when cloud space is insufficient does the tool offer to clear that folder; it waits for explicit confirmation and never clears other cloud folders or the account trash. Local progress and the library lock are stored in `<dest>/.pikpakget/`.
- Removed old single-account state migration and the `--no-sweep` option. Old state formats are rejected with an error.
- Changed the default to one download connection. `--repeat 0` now means one pass; unfinished work produces a nonzero exit status.
- Fixed cases where a dry run changed cloud files, a same-size local file was accepted without a content check, a truncated share listing was marked complete, or the same share in two local folders shared one progress record.
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
