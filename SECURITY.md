# Security policy

## What is sensitive on your machine

Credentials live in the device's `~/.pikpakget/`; each library keeps progress in
`<library>/.pikpakget/`. Both directory names are covered by `.gitignore`:

| file | what it holds | why it matters |
|---|---|---|
| `accounts/<account ID>/session.json` | `access_token`, the single-use rotating `refresh_token`, user id | written with `0600`; a refresh token is enough to act as you |
| `accounts.json` | account labels and rotation order | no passwords or tokens, but labels may identify you |
| `accounts/<account ID>/device_id` | that account's device fingerprint sent with its requests | not a secret, but replacing it changes how the account looks to PikPak |
| `grab-<date>.log` | every line printed: **share URLs and downloaded filenames** | this is the file people accidentally paste |
| `state.json` | the same, plus per-file progress and local paths | ditto |

**Before attaching any log or state file to a bug report, remove the identifying
lines** — share URLs and filenames name other people's libraries, and yours:

```sh
sed -E 's#(mypikpak\.(com|net)/s/)[A-Za-z0-9_-]{6,}#\1<REDACTED>#g; s#/[^ ]*/<[^ ]*\.mp4>#<PATH>/<REDACTED>.mp4#g' \
  <library>/.pikpakget/grab-*.log > /tmp/redacted.log
```

Do not put your session file anywhere near an issue. If a bug can only be reproduced
with it, say so and we will work through a private channel.

## What is *not* a secret here

`pikpakget/api.py` contains a client id and client secret. They are the constants
PikPak's own desktop application ships with and sends on every request — reproducing
them is what makes the public REST API reachable at all, and revoking them would break
every legitimate client, so they are not user credentials and reporting them is not a
vulnerability. User credentials are the tokens in the table above, which never leave
your machine.

The tool deliberately mimics the desktop client (headers, client version, captcha
signature). That is a documented compatibility choice, not an exploit: it holds no
account, no quota and no rate limit that the official client does not.

## Reporting a vulnerability

Open a [GitHub issue](https://github.com/YJBeetle/PikpakGet/issues) asking for a
private channel, or use private vulnerability reporting from the repository's Security
tab. Versions below 0.1.x are pre-release; the CLI and state format are not frozen yet,
so please include `pikpakget --version` and what you ran.
