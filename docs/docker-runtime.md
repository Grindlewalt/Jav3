# Docker box runtime (WP8)

The lighter, weaker alternative to a KVM guest: "a lot lighter, less secure,
but people may not always care". Chosen explicitly per security profile
(`box_runtime: "docker"`), and only while `settings.docker_enabled` is on
(default off). With it off, `boxes.allocate` refuses runtime docker, and
nothing in this document runs.

Code: `backend/vm/docker_runtime.py` (controller, run spec, daemon probe,
availability), `backend/vm/transport_unix.py` (checked AF_UNIX transport,
listeners), `backend/vm/docker_recipe.py` (variant recipe -> Dockerfile
adapter), `vm/docker/` (Dockerfile, baked entrypoint). The guest side of the
transport is WP1's `guest/backend/boxinfo.py` (no second guest module, so the
flag-off guest package is unchanged). Tests: `tests/test_docker_runtime.py`.

## 1. Network isolation: `--network none`, and the proxy over a unix socket

The contract (section L) sketched one internal Docker bridge per box
(`jvbr<cid>`, `--internal`, pinned into the nft sets). The runtime does **not**
do that. Each container runs with `--network none`: its network namespace has
`lo` and nothing else. Its only ways off the box are two sockets the host
listens on, in `<vm_dir>/sock/<cid>/` (mounted at `/run/jav3`):

| socket | host listener | identity |
|---|---|---|
| `gateway.sock` | `gateway.listen_unix(box)` (WP1) | the listener's box |
| `proxy.sock` | `docker_runtime.proxy_handler(box)` -> the box's egress proxy | the listener's box |

Inside the container, the baked entrypoint (`vm/docker/bootstrap.py`) runs a
forwarder on `127.0.0.1:8443`, the container's own loopback, which splices
each connection to `proxy.sock`. `HTTP(S)_PROXY` point at it, so pip, npm,
curl and git work the way they do in a KVM box.

Why this instead of the bridge:

1. **It holds by construction, not by rule-set.** With no interface there is
   no route, so the kernel cannot send a packet out of the namespace. "The
   egress proxy is the only way out" does not depend on nft ordering, on
   Docker's own iptables/nftables rules (which Docker rewrites at will and
   has changed between releases), on `enable_icc`, or on IPv6 defaults.
2. **Rootless Docker breaks the bridge design.** Under rootless Docker the
   bridge lives inside rootlesskit's network namespace. The host's nft never
   sees `jvbrN`, and the container cannot reach the host's `10.201.N.1`, so
   the pin in section L cannot work. Rootless is the hardening we want most.
   `--network none` works the same rootless, userns-remapped or rootful.
3. **Docker's embedded DNS.** On a user-defined network the container
   resolves through 127.0.0.11. On older engines that resolver forwarded
   internal-network queries to the host's upstream resolvers, which is a
   DNS exfiltration path around the proxy. With no network there is no
   resolver. The proxy resolves hostnames itself: CONNECT names a host.
4. **No inter-container traffic, no published ports.** There is nothing to
   publish to and no peer to reach. Inbound goes only through WP3's relay.

What it costs: the container has no DNS (tools that resolve before
connecting fail unless they go through the proxy, the same as the KVM guest's
default-deny), and WP2 must accept a connection handed over from a unix
listener (below).

## 2. Hardening (each one asserted by `validate_spec` on every boot, and tested)

- **Never the docker socket.** No `-v`/`--volume` at all. The only mounts are
  `type=bind,src=<vm_dir>/sock/<cid>,dst=/run/jav3` plus, for service boxes,
  the named volume `jav3-srv-<id>` at `/srv`. Any mount source containing
  `docker.sock`/`containerd.sock` is refused.
- **User namespaces:** rootless or userns-remap is detected from
  `docker info` SecurityOptions. If neither is present, the box still starts
  with a loud warning: a `docker_weak_isolation` security event (warn),
  `status()["isolation"]["weak"] = true`, and `availability()` reports
  `weak`. The requested setting `docker_require_userns` turns that into a
  refusal (`docker_hardening_refused`).
- `--user 10001:10001`: the image's only user. No setuid binaries in the image.
- `--cap-drop ALL`, `--security-opt no-new-privileges=true`.
- **Seccomp:** the daemon's default profile. A daemon without seccomp (or
  with an `unconfined` default) is **always** refused. `seccomp=unconfined`,
  `apparmor=unconfined`, `label=disable` and `systempaths=unconfined` are
  forbidden in the spec. `apparmor=docker-default` is named when the daemon
  has AppArmor.
- `--read-only` rootfs. Capped tmpfs: `/tmp` (`docker_tmpfs_mb`,
  noexec,nosuid,nodev), `/run` (8 MB, noexec), `/home/jav3` (64 MB),
  `/opt/jarvis` (half of the box memory; the pushed package and the workspace
  copy; exec allowed because run_code needs it; nosuid,nodev).
  `--shm-size 16m`.
- `--pids-limit docker_box_pids`, `--memory` = `--memory-swap` (no swap),
  `--cpus docker_box_cpus`, `--ulimit core=0 nofile=1024:4096`,
  `--oom-score-adj 500` (on a 4 GB Pi the box dies before the app does).
  Inside it, what the agent runs (run_code, screenshot) takes oom_score_adj 1000
  (`backend/memguard.py`; the container is not root and its cgroup tree is
  read-only, so it cannot get the KVM guest's capped sub-cgroup), so a command
  that outgrows the box is the OOM killer's pick, not the run-turn server. A
  turn that still loses its guest reports the container's exit code, whether a
  process was OOM-killed, and its last output lines (`DockerBox.death_note`).
- `--ipc private`, `--cgroupns private`, and no `--pid/--uts/--userns host`.
  `--restart no`, json-file logs capped at 2 x 1 MB, `--no-healthcheck`.
- **gVisor:** `--runtime runsc` whenever the daemon has runsc registered.
  `docker_oci_runtime="runsc"` or the requested `docker_require_runsc`
  requires it (refused without it). `docker_oci_runtime="runc"` opts out.
- PID 1 is `tini` from the image, not `--init`, because `--init` bind-mounts a
  host binary.
- Forbidden outright: `--privileged`, `--cap-add`, `--device`, `-p/--publish/-P`,
  `--volumes-from`, `--link`, `--add-host`, `--network-alias`, `--group-add`,
  `--sysctl`, `--gpus`, `--device-cgroup-rule`.

## 3. The socket directory

`<vm_dir>/sock` and `<vm_dir>/sock/<cid>` are 0700 and owned by the service
user. Before each boot the per-box directory is emptied: everything in it is
ephemeral, and anything a previous guest left there is untrusted. The
container's host uid gets exactly one ACL entry, `u:<uid>:rwx`, so it can
create its listeners. The directory's **default** ACL names both uids rw, so
every socket either side creates there (the gateway and `boxinfo.listen`
chmod 0660) is connectable by the other side and by no one else. `setfacl`
(the `acl` package) is required. Without it the box is refused rather than
opening the directory up.

The container's host uid is 10001 when rootful, `subuid_start + 10000` when
rootless, and `dockremap`'s `subuid_start + 10001` under userns-remap. After
`docker run` the value is checked against `/proc/<pid>/status` and the ACL
moved if it was wrong.

The directory is writable by the guest, so the host treats it as hostile:

- The guest can unlink or shadow `gateway.sock`/`proxy.sock`. That only cuts
  itself off: the host never connects to those names, and a symlink the guest
  makes resolves inside the container.
- The guest can replace `5556.sock` with a symlink to a host socket (the
  docker socket, say), which the host's `connect()` would follow.
  `transport_unix.connect_checked` lstat's first (it must be a socket, not a
  symlink), then after connecting and **before sending a byte** it checks
  SO_PEERCRED against the container's uid. dockerd, containerd and the Jav3
  service all fail that check (`docker_socket_refused` event, box failed).

## 4. Lifecycle

`DockerBox` implements GuestVM's interface (`running`, `pid`, `booted_at`,
`inflight`, `idle_since`, `acquire`, `release`, `boot`, `teardown`) plus
`status()`, `async stats()` (`docker stats`: works rootless) and the state
machine `stopped -> starting -> running -> stopping -> stopped` (`failed` from
starting/running; boot or teardown from failed).

`boot`: probe the daemon -> plan isolation (refuse or warn) -> render and
validate the spec -> prepare the socket dir -> gateway + proxy listeners ->
`boxes.box_up` hooks (a raising hook fails the start before any container
exists) -> `docker rm -f` any stale container of that name -> `docker run`.
`acquire` then waits until `5556.sock` accepts a checked connect.
`acquire` on a box marked running checks that the container is still alive,
and restarts it if not. Teardown and failure both do the same cleanup:
`docker rm -f`, stop the listeners, `box_down`, empty the socket dir.
`reap_orphans()` removes `jav3.managed=1` containers that no live box owns.

## 5. Availability (for the UI)

`await docker_runtime.runtimes_json()` returns
`{kvm: {available, reason}, docker: {available, reason, rootless, userns,
gvisor, seccomp, weak, warnings}}`. It costs one `docker info`, cached for
30 s. Intended home: a `runtimes` key on `GET /api/vm/boxes` (WP1 owns
vm_api.py).

## 6. Images

`vm/docker/Dockerfile`: debian trixie-slim (Debian 13, as the KVM image), the same toolchain as the KVM
golden image, `tini`, user 10001, setuid bits stripped, and the baked
entrypoint only. The guest runtime is pushed at start, as in KVM. It builds
with BuildKit (`COPY --chmod`). Variants: `docker_recipe.render_dockerfile(recipe)`
renders from the contract's variant shape `{name, from, packages:[{manager,
package, version}]}`. It is an **adapter**: WP5's `.recipe` file format was not
visible yet, so WP5 only needs to parse its file into that dict.

Nothing builds a Docker image of a variant (`desktop`, `dev`, ...): variants are
KVM layers. A docker box whose image is not `main` checks for the image before it
starts anything and, when there is none, refuses with a message that says to run
the project in a KVM box or use `main` (`no_variant_image_message`). It does not
fall back to `main`: a box that quietly lacks chromium and Xvfb sends the model, and
the `screenshot` tool's own "set the profile's image to desktop", round in a circle.

## 7. Residual risk (the docker runtime is weaker than KVM)

| Risk | Why it is weaker | Mitigation here |
|---|---|---|
| **Shared kernel** | A KVM guest attacks QEMU/KVM's small device and hypercall surface through a separate guest kernel. A container makes syscalls straight into the host kernel. Any reachable kernel bug (a netfilter, io_uring, eBPF, overlayfs or cgroup CVE) is host code execution. | Default seccomp (about 44 syscalls blocked, including `bpf`, `keyctl`, `userfaultfd`, `perf_event_open`, mount and `unshare` without caps), no caps, no-new-privileges, and gVisor when installed (the syscall surface becomes the Sentry). Without gVisor this is the dominant risk and cannot be closed. |
| Escape lands as a host uid | A kernel escape runs as the container's host uid: rootful without userns means uid 10001, or root if it also escalates. | Rootless/userns-remap puts the uid in a subordinate range with no host files. Without either, the `docker_weak_isolation` event is raised on every boot, and `docker_require_userns` refuses. |
| The Docker daemon is in the TCB | Rootful dockerd is root, and a bug in its API or in runc (CVE-2019-5736, CVE-2024-21626 "leaky vessels") is a host compromise. | The socket is never mounted. Only the Jav3 service talks to dockerd, always with an argv that has passed `validate_spec`. Keep runc current. Rootless shrinks the daemon's authority to one user. |
| Shared page cache and side channels | The container shares the host kernel's page cache, scheduler and caches. That allows cache and timing side channels, and memory pressure on the host (the tmpfs counts against the cgroup, but kernel memory is shared). | Memory, swap, pids and CPU are capped. `--oom-score-adj 500`. |
| /proc and /sys info leak | Host kernel version, CPU and uptime are visible, as are some `/sys` paths that Docker's masked-paths list does not cover. | Docker's default masked and read-only paths, plus `--read-only`. gVisor synthesizes its own /proc. |
| Workspace copy lives in tmpfs | The same as KVM (a copy is pushed and writes come back through the host gates), but it is RAM on the host kernel. | Capped at half the box's memory. |
| Image builds use Docker's build network | `docker build` for variants fetches packages outside the egress proxy. | Only operator-approved packages, argv-built with names validated. Recommended follow-up: build variants in a builder box, or through the proxy. |
| Guest-writable socket dir | The guest can create files in `<vm_dir>/sock/<cid>`. | 0700 plus a single ACL entry. Emptied on each boot. Host connects only after the lstat and peer-uid check. Host listeners are recreated by the host. |
| No DNS inside the box | This limits functionality, not security. | Tools go through the proxy, which resolves names itself. |

## 8. What other packages must do (small)

**Status (integration, 2026-09-26): all of the items below are done**, and
`tests/test_boxes_integration.py` tests each one. Two residuals remain:

- Variant builds with `docker build` still use Docker's build network. No code
  runs `docker build`: the operator runs it from
  `GET /api/vm/images/{v}/dockerfile`. See the residual in section 7.
- A docker SERVICE box is not supported yet, for three reasons:
  - svcd's socket is created root-only (umask 077).
  - svcd needs systemd, which a container does not have.
  - `DockerBox.acquire` waits on the run-turn socket, not on svcd's.

  `services.py` always allocates service boxes as kvm, whatever the
  profile's `box_runtime` says.

WP1 (as of 945c367, which already made `server.py`/`shell.py`/`model.py`/
`registry.py` go through `boxinfo`, so no guest socket code forks):

1. `boxes.controller`: import the driver lazily like kvm:
   `if box.runtime == "docker": importlib.import_module(".docker_runtime", __package__)`
   (importing registers `"docker"` and installs `transport_unix.UnixTransport`).
2. `boxes._TRANSPORTS["docker"] = transport_unix.UnixTransport` officially
   (same paths as `boxes.UnixTransport`; adds the lstat + SO_PEERCRED checked
   `connect`, `proxy_path()`), so the check never depends on import order.
3. `Box.box_json()` for runtime docker: `net.proxy = "http://127.0.0.1:8443"`
   (the in-container forwarder) and `net.proxy_socket = "/run/jav3/proxy.sock"`;
   `guest_ip`/`gateway`/`dns` are meaningless there. And in `server.py`,
   `_detect_egress_proxy`, for runtime docker just export `net.proxy`
   instead of probing a route (today it prints "none (netless)" but keeps
   the env the entrypoint already set, so it works).
4. `shell.py`: HOME / cwd fallback `/root` -> `$HOME` (`/home/jav3` in docker;
   the container user cannot enter `/root`).
5. App lifespan: `if settings.docker_enabled: await docker_runtime.reap_orphans()`.
6. `GET /api/vm/boxes`: add `"runtimes": await docker_runtime.runtimes_json()`.
7. config.py: `docker_require_runsc: bool = False`,
   `docker_require_userns: bool = False` (read via
   `docker_runtime.PENDING_SETTINGS` until they exist). Section L's bridge
   (`jvbr<cid>`, `net_box.sh pin`) is unused by docker boxes; see section 1.

WP2: `egress_proxy.handle_box_conn(box, reader, writer)`, the same as
`handle_conn` with the box fixed (attribution = that box, `peer_ip` =
`"unix"`), and a `box_up` hook that does NOT bind `box.host_ip:8443` for
`box.runtime == "docker"` (that address does not exist on the host). Until
`handle_box_conn` exists, `proxy.sock` splices to `box.host_ip:8443` and so
fails closed (no egress).
