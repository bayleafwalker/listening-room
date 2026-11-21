# Security policy

Listening Room keeps provider tokens in private files and never sends data to
the maintainers (see "Privacy" in the README). If you find a way to leak a
token, read another user's state, or make the app start playback or contact a
host the user did not choose, please report it privately.

**How to report:** open the **Security** tab of this repository and choose
**Report a vulnerability** (GitHub private vulnerability reporting). Only the
maintainers can see the report. Please include the version or commit, what you
did, what you expected and what happened. Do not include real tokens.

Please do not open a public issue for a vulnerability. For anything that is not
a vulnerability, a normal issue is welcome.

You can expect an acknowledgement within a week. This is a hobby project, so
fixes follow as time allows; critical problems come first. Only the latest
release is supported.

## What the app trusts

These are deliberate, not oversights. A report that one of them can be abused
across a trust boundary (for example by a stream or provider reply) is welcome.

- **Stream links.** `valid_url` accepts any `http` or `https` link with a host and
  no embedded credentials, including loopback (`127.0.0.1`, `localhost`) and
  local-network addresses, because home radio and library servers live there.
  It is a shape check, not a network policy.
- **Catalogue apps.** An `app` entry in the catalogue runs its `command` list
  (a program and arguments, never through a shell) when the user chooses it.
  The catalogue is trusted configuration: only use one you wrote or reviewed.
- **Catalogue path.** `LISTENING_HUB_CATALOG` replaces the catalogue the app
  loads. Anyone who controls that environment variable controls which commands
  an `app` entry can run, so treat the variable like `PATH`.
