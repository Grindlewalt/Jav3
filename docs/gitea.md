# Gitea on the Jav3 host

Jav3 can run a private [Gitea](https://gitea.com) next to itself so agents
propose work as pull requests and the operator reviews and merges them.

## Setting it up

The installer does it by default (`scripts/install.sh`; skip it with
`--no-gitea`, preview it with `--gitea-dry-run`). On an existing install:

    .venv/bin/python -m backend.cli gitea-setup [--dry-run] [--user NAME] [--port N]
    systemctl --user restart jarvis

What it does, each step skipped when already done (a re-run is safe):

1. Downloads the pinned static binary for the host arch (amd64/arm64) into
   `<state_dir>/gitea/bin/` and checks its sha256. The version and checksums
   are pinned in one place: `backend/gitea_setup.py` (`GITEA_VERSION`,
   `GITEA_SHA256`). It never uses a package manager.
2. Writes `<state_dir>/gitea/custom/conf/app.ini` once: SQLite, HTTP on
   `0.0.0.0:<port>` (default 3000), SSH off, `INSTALL_LOCK`,
   `DISABLE_REGISTRATION`, `REQUIRE_SIGNIN_VIEW`, `OFFLINE_MODE`.
3. Creates the operator's admin account (the same username as the first Jav3
   login) and the `jav3-agent` bot. The password is prompted (type your Jav3
   password to reuse it) or read from `--password-stdin`. Without a terminal, a
   random one goes 0600 to `~/.config/jarvis/gitea-admin.password` and Gitea
   asks for a new one at first sign-in.
4. Mints an API token for each: `~/.config/jarvis/gitea-admin.token` and
   `gitea-bot.token`, both 0600. A token that still works is kept.
5. Installs and starts `jav3-gitea-<port>.service` (systemd --user).
6. Writes `JARVIS_GITEA_ENABLED=true`, `JARVIS_GITEA_OWNER`,
   `JARVIS_GITEA_PORT` to `~/.config/jarvis/env`, then creates a repo for each
   project.

Settings (`backend/config.py`): `gitea_enabled`, `gitea_port`, `gitea_url`
(the address for browser links; empty means `http://<first LAN IP>:<port>`),
`gitea_owner`, `gitea_bot_user`, `gitea_dir`, and the two token paths.

## How an agent's work reaches main

1. The agent calls `git_push_request(title, description)`. It has no branch
   or ref argument.
2. The host commits the project's live files on top of main in a throwaway
   index. Main, the index and the files stay as they were.
3. The host pushes that commit as `jav3-agent` to `agent/<8 hex>`, a name the
   host builds, and opens a pull request into `main`.
4. A `git_requests` row of kind `push` shows up in the Review Center, with the
   diff stat and a link to the PR.
5. Approve merges the PR as the operator, then moves the host's main forward
   to the merge commit with `reset --mixed`, which leaves the files alone.
   Reject closes the PR and deletes the branch. The operator can also merge or
   close in Gitea directly: the request follows whenever the list is next
   read.

Approved commit requests (`git_commit_request`) also push main to Gitea as the
operator.

## Boundaries

- Main is protected. Only the operator can push or merge, and the bot is on
  neither whitelist.
- The bot can push branches in Gitea's model. Jav3 only ever pushes
  `agent/*` as the bot, and checks that before running git.
- Boxes hold no credentials and their git state is thrown away. The egress
  proxy refuses Gitea's name on any port, and loopback or host addresses on
  Gitea's port, before any policy runs.
- Tokens are never written to `.git/config`, argv, the DB or a tool result.
  Git gets them as a per-command `http.extraheader` in the environment.
- Accounts: Settings > Gitea lists users and can create a user, reset a
  password, or disable a user. It is operator-only (the password session, not
  device tokens). For anything else, use Gitea's own site admin.

## Off

With `gitea_enabled` false, or no tokens, nothing changes: `git_push_request`
answers that Gitea isn't set up and points to `git_commit_request`.
