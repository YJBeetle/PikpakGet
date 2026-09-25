# Security

## Protect local data

| Path | Contents |
|---|---|
| `~/.pikpakget/accounts/<account ID>/session.json` | Access and refresh tokens. Anyone with these may be able to use the account. |
| `~/.pikpakget/accounts/<account ID>/device_id` | The device ID sent with that account's requests. It is not a password. |
| `~/.pikpakget/accounts.json` | Account labels and selection order. |
| `<library>/.pikpakget/state.json` | Share links, filenames, local paths, and progress. |
| `<library>/.pikpakget/grab-*.log` | Output from the run, including share links and filenames. |

Sessions and device IDs are written with `0600` permissions. Library progress and log files currently use the process's default permissions; use a restrictive `umask` or protect the library directory if other local users must not read them. Do not attach raw session, state, or log files to an issue; remove identifying links, paths, and tokens first. A simple URL replacement is not enough to sanitize an entire log.

The constants named `CLIENT_ID` and `CLIENT_SECRET` in `pikpakget/api.py` identify the application, not your account. Your account credentials are the password you enter at login and the tokens saved in `session.json`.

## Cloud deletion

An actual download run permanently deletes **all contents** of the selected account's cloud root `.pikpakget` folder before restoring files. Keep personal files out of that folder. `--purge-trash` is an additional option that can empty the account's entire trash.

Account locks only protect processes on the same computer. Do not run separate installations against the same account's `.pikpakget` folder at the same time.

## Report a vulnerability

Use the repository's [private vulnerability reporting](https://github.com/YJBeetle/PikpakGet/security/advisories/new), or open an [issue](https://github.com/YJBeetle/PikpakGet/issues) asking for a private contact method. Include the version and a description of the behavior without posting credentials, share links, or raw logs.
