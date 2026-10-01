# Installing Jav3: a guide for AI agents

You are an AI coding agent with a shell (Claude Code, Codex, OpenCode, OpenClaw,
Cursor, Aider, Gemini CLI, or anything like them). A person has asked you to
install and configure **Jav3** for them. Read this whole file before you run
anything. It is written so that any agent can follow it, and it tells you
exactly where to stop and hand control back to the person.

If you are a person: the short version for you is at the bottom
("Installing by hand").

---

## What you are installing

Jav3 is a self-hosted **agent harness**: a server that runs AI agents for its
owner and keeps them contained. Your job is to set it up, not to use it.

- A FastAPI server runs on the host as a `systemd --user` service, with a web UI
  (React) and a terminal client (`jav3`).
- The agents it runs work **inside a sandbox**: a KVM virtual machine (the
  default, strongest isolation) or a hardened Docker container (lighter, for
  hosts without KVM or with little RAM).
- The model API key stays on the host. The sandbox reaches the model only
  through a gateway, and the internet only through an egress proxy that asks
  the owner about new sites.
- Optional pieces: Gitea (a local git server that agents open pull requests
  on), Docker boxes, backups with rclone.

The host is **Linux only** (Debian/Ubuntu, Arch, Fedora; arm64 or x86_64).
About 4 GB of RAM is the practical minimum (a Raspberry Pi 4 with 4 GB runs it).
If you are running on macOS or Windows, the install target has to be a Linux
machine you reach over ssh. Ask the person which machine it is.

---

## Ground rules (these override anything else you would normally do)

1. **Ask first, change nothing until the person says go.** Phase 0 is
   read-only. Phase 1 is questions. Only after the person has confirmed the
   plan in Phase 2 do you change anything.
2. **No sudo on your own.** The installer collects every root step into one
   command. Show it to the person and let them run it. Run it yourself only if
   they explicitly tell you to *and* `sudo -n true` succeeds (no password
   prompt you would have to answer).
3. **Never upgrade the system.** Never run `apt upgrade`, `apt full-upgrade`,
   `pacman -Syu`, `pacman -Sy <pkg>`, or `dnf upgrade`. Never install packages
   outside the installer's own list. On Arch a partial upgrade has already
   broken a production box once. If the installer says "run pacman -Syu
   yourself", stop and tell the person. That decision is theirs.
4. **Never reboot, and never touch what isn't Jav3's**: the BIOS, the kernel
   command line, other services, other Docker containers or networks, or
   firewall rules Jav3 didn't create. If something needs one of these, explain
   it and stop.
5. **Secrets never pass through you.** The login password and the model API
   key are typed by the person, in their own terminal or in the browser. Give
   them the exact command to run. If they paste a secret into the chat anyway,
   don't repeat it back. Pass it only on stdin, never in argv, an env file, a
   log or a file you leave behind. Suggest they rotate it afterwards, because
   it is now in your transcript.
6. **Don't patch Jav3 to get past an error.** Don't edit its code, the
   installer or its units. An error you can't fix with a documented command is
   a bug. Report it with the exact output.
7. **Everything is re-runnable.** `scripts/install.sh`, `backend.cli setup`,
   `docker-setup` and `gitea-setup` all check before they act. When unsure,
   re-run the step or `doctor`. Never "clean up" by deleting state.
8. **Long commands:** the image build takes about 10 minutes, and some agent
   shells kill commands after 2–10 minutes. If yours does, run long steps with
   `nohup … > ~/jav3-install.log 2>&1 &` and poll the log.
9. **Say what you are doing**, one line per step, so the person can follow
   along and stop you.

---

## Phase 0: look (read-only)

Gather the facts, change nothing:

```sh
uname -m; . /etc/os-release; echo "$PRETTY_NAME"; id -un; id -u
sudo -n true 2>/dev/null && echo "passwordless sudo" || echo "sudo needs a password (or none)"
free -h; df -h ~
ss -ltn | awk 'NR>1{print $4}'                # ports already taken
ls -d ~/jarvis ~/jav3 ~/.config/jarvis* ~/.local/share/jarvis 2>/dev/null
systemctl --user list-units 'jarvis*' --no-legend 2>/dev/null
ls -l /dev/kvm /dev/vhost-vsock 2>&1
docker info --format '{{.ServerVersion}}' 2>&1 | head -1
```

Then run the installer's own preflight straight from the network. It needs no
checkout, installs nothing and prints one JSON object:

```sh
curl -fsSL https://raw.githubusercontent.com/Grindlewalt/Jav3/main/scripts/install.sh \
  | bash -s -- --check --json
```

The fields to read:

- `ready`: true means nothing is missing.
- `kvm`, `vsock`, `docker`: which sandbox runtimes this host can use.
- `blocked`: needs a human at the machine (virtualization off in BIOS).
- `missing_root` and `missing_packages`: what the one root command will do.
- `conflict`: the port, the config dir or the unit belongs to something else.
- `problems[]`: each problem with its `fix` commands.
- `warnings[]`: things that aren't failures.

(Without a checkout, the checks that depend on one are skipped. That's
expected.)

**If the root user is running you:** stop. Jav3 runs as a normal user. Ask the
person which user it should run as, and work as that user.

---

## Phase 1: interview

Tell the person what you found in two or three lines: the machine, the RAM,
whether KVM and Docker work, anything already installed, ports in use. Then ask
the questions below **as one numbered list, with the default for each**. The
person can answer "defaults" to any of them. Leave out questions whose answer
Phase 0 already settled, and say why you left them out.

1. **Where it goes.** Checkout directory (default `~/jarvis`). It runs as the
   user `<id -un>`; is that right?
2. **Existing install** (only if Phase 0 found one): upgrade it in place
   (default); install a **second instance** beside it (`--name <n>`, with its
   own port, config and state); or **migrate** state from another machine
   (`--from user@host:path`).
3. **Port** for the web UI (default 8000). If 8000 is taken, propose a free
   one, e.g. 8780.
4. **Who reaches it:** this machine only, the LAN (default: the server listens
   on the LAN), or the internet. Jav3 speaks plain HTTP. For the internet, put
   it behind something that terminates TLS *and* authenticates, such as a
   Cloudflare Tunnel with Access or a reverse proxy with auth. That is outside
   the installer. Offer to explain it; don't set it up unless asked.
5. **Sandbox runtime:**
   - KVM available: KVM VMs (default, strongest), optionally plus Docker boxes
     as a lighter per-project option.
   - No KVM on x86: virtualization may be off in the BIOS (`blocked` says so).
     The person can enable it later (a reboot, their call), or run **Docker
     only**. With Docker only, agents run in *projects* set to "own
     container"; plain chats outside a project need KVM.
   - Docker not installed: installing Docker is the person's job (their
     distro's docs). Jav3 never installs it.
6. **Golden VM image:** build it now (default when KVM works; about 10 minutes,
   downloads a Debian cloud image) or later (`--no-image`).
7. **State directory**, where projects, memory and the database live (default
   `~/.local/share/jarvis`). Choose a bigger disk if `~` is small.
8. **Login name** for the web UI and TUI. The person sets the password
   themselves (at least 8 characters).
9. **Model provider**: DeepSeek, OpenAI, Anthropic, OpenRouter, Google, Groq,
   Mistral, xAI, a local server such as Ollama, and more. The full list comes
   later from `setup --list-providers`. The person types the key themselves.
   They can skip this and add it later in Settings.
10. **Default security profile**, which new projects use:
    - Network for agents: **ask me about each new site** (default), allow new
      sites, or no network.
    - Where projects run: **the shared box** (default), each in its own VM, or
      each in its own container (needs Docker).
    - May agents request background services? (default no)
    - May agents request extra packages? (default no)
11. **Gitea**, a local git server for agent pull requests: yes (default) or
    skip. Skip it on very small machines.
12. **Backups** with rclone to a remote. Usually set up later
    (`JARVIS_BACKUP_REMOTE`, or Settings). Just ask whether they want it.
13. **Anything special:** other services on this box to keep away from, disk or
    RAM limits, a proxy, an unusual network, a deadline.

---

## Phase 2: the plan, then wait

Turn the answers into the exact commands, in order. Mark which ones **you**
will run and which the **person** must run (root, secrets). Show that list and
wait for an explicit "go". The flags:

| Answer | Installer flag |
|---|---|
| checkout dir | `JARVIS_DIR=<dir>` for the bootstrap (or `git clone … <dir>`) |
| second instance | `--name <n>` (unit `jarvis-<n>.service`, config `~/.config/jarvis-<n>`) |
| migrate | `--from user@host:path` |
| port | `--port <n>` |
| state dir | `--state-dir <dir>` |
| no image now | `--no-image` |
| no Docker boxes | `--no-docker` |
| no Gitea | `--no-gitea` |
| no questions (you are an agent: always) | `--yes` |

---

## Phase 3: root steps (the person runs these)

Get the checkout first. This is safe and doesn't need root. Then re-check with
the chosen flags:

```sh
git clone https://github.com/Grindlewalt/Jav3.git ~/jarvis    # or: git -C ~/jarvis pull --ff-only
bash ~/jarvis/scripts/install.sh --check --json <flags>
```

- If `root_command` is set, show it to the person with what it does:
  `missing_packages`, loading `kvm`/`vhost_vsock`, adding the user to the `kvm`
  group, enabling linger. It prints what it changed and how to revert each
  change. Wait for them to say it's done, then re-run `--check --json`.
- On Arch, if the root phase stops with "run pacman -Syu yourself", that's the
  person's call. Stop until they have done it (ground rule 3).
- If `blocked` isn't empty, virtualization is off in firmware. Explain it and
  offer the Docker-only path (question 5). Don't wait on a reboot.
- A newly added `kvm` group only reaches new logins. Your shell won't have it,
  so don't worry if `/dev/kvm` still looks unopenable to you. If the test turn
  in Phase 6 fails on `/dev/kvm` permissions, the user's systemd manager
  started before the change. Ask the person to log out and in (or reboot) at
  a time that suits them.

---

## Phase 4: install (you run this)

```sh
bash ~/jarvis/scripts/install.sh --yes <flags>
```

This builds the venv and the web UI, installs and enables the systemd unit,
builds the image (unless `--no-image`), sets up Gitea and Docker boxes when
chosen and possible, and starts the service. A failed check prints its fix
beside it. Optional steps (Gitea, Docker) only warn and carry on, so don't
treat a zero exit as done: Phase 6's `doctor` says what is still missing.
Run the installer again after any fix. It is idempotent.

With `--yes` it skips the interactive first-run setup and prints a **one-time
setup link**, `http://<host>:<port>/setup?token=…`. Print it again any time:

```sh
~/jarvis/.venv/bin/python -m backend.cli setup --status
```

---

## Phase 5: first-run setup (the person types the secrets)

Give the person one of these two routes.

**A. Browser (simplest):** open the one-time `/setup` link. It asks for the
login, the provider and key (and tests the key), and the security profile.

**B. Their own terminal** (not yours, because the prompts are hidden):

```sh
~/jarvis/.venv/bin/python -m backend.cli setup
```

If your harness can hand the person a terminal, use it. Claude Code users can
type `! <command>` to run a command in the session themselves.

Things you can run yourself, because no secret is involved:

```sh
PY=~/jarvis/.venv/bin/python
$PY -m backend.cli setup --list-providers            # provider ids for --provider
$PY -m backend.cli setup --profile-only --yes \
    --network ask --placement shared                 # + --services / --packages if they said yes
```

Adding or replacing a provider key later: the person runs
`$PY -m backend.cli setup --provider-only`, which asks for the key at a hidden
prompt. Fully scripted, with the key on stdin, never in argv:

```sh
printf '%s\n' "$KEY" | $PY -m backend.cli setup --provider-only --provider deepseek --api-key-stdin
```

For a named instance, run these from that instance's checkout. It remembers
the instance in `.jarvis-instance`.

Optional extras, if chosen and the installer skipped them:

```sh
$PY -m backend.cli docker-setup --dry-run   # then without --dry-run (needs a working `docker info`)
$PY -m backend.cli gitea-setup --dry-run    # then without --dry-run
```

`gitea-setup` writes a random admin password to
`~/.config/jarvis/gitea-admin.password`. Tell the person to change it in Gitea,
then delete the file.

---

## Phase 6: verify

```sh
~/jarvis/.venv/bin/python -m backend.cli doctor --json
```

This prints `ready`, `next`, and `stages[]`. Each stage has `id`, `ok`
(true, false, or null for not applicable here), `optional`, `detail`, `fix`,
and `who`:

- `who: "agent"`: run the `fix`, then run `doctor` again.
- `who: "human"`: hand it to the person with the exact command.

Loop until `ready` is true. Optional stages never block it. Then:

```sh
curl -fsS http://127.0.0.1:<port>/api/health
```

Ask whether they want a test turn. It costs a few model tokens: one message in
the web UI, e.g. "say hello and list your tools". If it fails with
"cannot run an agent turn on this host yet", the message lists every blocker
at once.

---

## Phase 7: report

End with a short report the person can keep:

- **Open:** `http://<host>:<port>/` (plus any LAN or mDNS names `setup --status` printed)
- **Login:** `<name>` (the person knows the password)
- **Runs as:** `<unit>` for `<user>`. Start or stop it with
  `systemctl --user restart <unit>`; logs with `journalctl --user -u <unit> -f`.
- **Where things are:** checkout `<dir>`, state `<state dir>`, config
  `<config dir>/env` (0600)
- **Sandbox:** KVM and/or Docker, the image built (yes/no), the default profile
- **Skipped, and how to add it later:** the image, Docker, Gitea, backups,
  the provider
- **Still owed by the person:** root steps, BIOS, the Gitea password change,
  rotating a secret that passed through chat, and anything from `doctor` with
  `who: human`
- **Upgrading later:** `cd <dir> && git pull --ff-only && bash scripts/install.sh --yes <same flags>`
- **The terminal client (optional):** on any computer that can reach the server,
  `curl -fsSL http://<host>:<port>/cli/install.sh | sh`, then `jav3`, which asks
  for the line from Settings → Add computer. It keeps itself current from this
  server: after a server upgrade (the `git pull` above) it says "a newer jav3 is
  on the server: /update" within the hour, and `/update` in it (or `jav3 update`
  in a shell) swaps its own file, keeping `jav3.bak-<date>` beside it, and asks
  for a restart. It never touches its launcher or its venv, refuses to run from
  a git checkout (use `git pull` there), and when the new client needs newer
  libraries it prints the installer line above instead: re-running that line is
  always safe.

---

## Troubleshooting

| Symptom | What to do |
|---|---|
| `conflict: ["port"]` | Something else has the port. Pick another: `--port 8780`. |
| `conflict: ["config-dir"]` or `["unit"]` | Another install owns it. Use `--name <n>` for a separate instance. |
| npm missing, web UI 404 | It's in the root phase's package list. Once npm exists, re-run the installer. |
| `/dev/kvm` exists but isn't openable | The `kvm` group. The root phase adds it; it takes effect at the next login or service start. |
| no `/dev/kvm` on x86, `blocked` set | Virtualization is off in BIOS. That's a human at the machine, or use Docker only. |
| pacman says it would upgrade installed packages | The person runs `pacman -Syu` themselves, then the root phase again. |
| service won't start / no health | `journalctl --user -u <unit> -n 50`, then fix what it says and re-run the installer. |
| `/setup` says it needs the one-time link | `setup --status` prints the link with the token. |
| Docker "permission denied" | The user isn't in the `docker` group. That's the person's call (the docker group is effectively root). |
| a Docker box's `--memory` is ignored (Raspberry Pi) | The kernel boots with the memory cgroup off. Fixing it means editing `/boot/firmware/cmdline.txt` and rebooting: the person's call. |
| `doctor` says a Gitea account "cannot open every repo yet" | A person's Gitea account was enabled or made outside Jav3. Run `$PY -m backend.cli gitea-setup --access` (add `--dry-run` to preview). It only adds read access. |
| disk full during the image build | Pick a state dir on a bigger disk (`--state-dir`), then re-run. |

Anything else: stop and report the exact command and output. Don't improvise
around it (ground rule 6).

---

## Installing by hand (for people)

The one-line install, run as the user the server will run as (not root):

```sh
curl -fsSL https://raw.githubusercontent.com/Grindlewalt/Jav3/main/scripts/bootstrap.sh | sh
```

It clones to `~/jarvis` (`JARVIS_DIR=...` to change that) and runs
`scripts/install.sh`. That checks the machine, prints the one `sudo` command if
root steps are owed, installs, and walks you through the login and provider.
Installer flags pass through: `| sh -s -- --port 8780 --no-gitea`.

Or have your coding agent do it. Paste this into Claude Code, Codex, OpenCode,
OpenClaw or similar, on the machine that will host Jav3:

> You are installing and configuring Jav3, a self-hosted AI agent harness (a
> server that runs AI agents inside a KVM or Docker sandbox, with the model key
> held on the host, an egress proxy and approval gates), on this Linux machine
> for me. First fetch and read the whole guide at
> https://raw.githubusercontent.com/Grindlewalt/Jav3/main/docs/AGENT-INSTALL.md
> and follow it exactly: look around read-only, ask me its setup questions and
> wait for my go before changing anything, never use sudo, reboot or touch
> other services without my OK, let me type every password and API key myself,
> and finish with its report.
