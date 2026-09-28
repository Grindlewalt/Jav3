# Jav3 docker box image (WP8). Build (on the host that runs the boxes):
#   docker build -t jav3-guest-turn:latest vm/docker
#   docker tag jav3-guest-turn:latest jav3-guest-svc:latest      # until WP3 needs more
#   docker tag jav3-guest-turn:latest jav3-guest-builder:latest
# Image variants (WP5 recipes) are rendered FROM this image by
# backend/vm/docker_recipe.py.
#
# The image holds only the baked entrypoint; the guest runtime is pushed by the
# host gateway at start (exactly as in the KVM guest), into a tmpfs.
# Hardening that lives in the image: one non-root user (10001), no setuid/setgid
# binaries, no login shell for that user, no package caches. Everything else
# (read-only rootfs, cap-drop ALL, no network, limits) is in the run spec
# (backend/vm/docker_runtime.py run_spec/validate_spec).
# Debian 13, as the KVM golden image (vm/build_base.sh): the guest code relies on
# its Python (tarfile's extractall(filter=...) is 3.11.4+; bookworm ships 3.11.2
# and every docker box crashed in bootstrap, 2026-09-27)
ARG BASE=debian:trixie-slim
FROM ${BASE}

# the same curated toolchain as the KVM golden image (vm/build_base.sh), so
# run_code behaves the same in both runtimes
ARG JAV3_PACKAGES="python3-pip python3-venv python3-dev git curl ca-certificates nodejs npm build-essential pkg-config jq ripgrep sqlite3 unzip zip xz-utils"
RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends tini python3 ${JAV3_PACKAGES}; \
    apt-get purge -y --auto-remove openssh-client socat netcat-openbsd tcpdump || true; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/*; \
    groupadd --gid 10001 jav3; \
    useradd --uid 10001 --gid 10001 --no-create-home --home-dir /home/jav3 \
            --shell /usr/sbin/nologin jav3; \
    mkdir -p /opt/jarvis /home/jav3 /srv; \
    chown 10001:10001 /opt/jarvis /home/jav3 /srv; \
    chmod 0700 /opt/jarvis /home/jav3 /srv; \
    install -d -o root -g root -m 0755 /usr/local/lib/jav3; \
    find / -xdev -type f -perm /6000 -exec chmod a-s {} + ; \
    echo "jav3-docker-box" > /etc/jarvis-image-version

# the directory exists (0755) BEFORE the COPY: a COPY --chmod that creates its
# parent gave the directory 0644 too — untraversable, so uid 10001 could not
# read bootstrap.py and every docker box died at start (2026-09-27, the Pi)
COPY --chown=root:root --chmod=0644 bootstrap.py /usr/local/lib/jav3/bootstrap.py

USER 10001:10001
WORKDIR /opt/jarvis
ENV HOME=/home/jav3 PYTHONDONTWRITEBYTECODE=1 JAV3_TRANSPORT=unix
# tini is PID 1: it reaps the orphans run_code leaves behind (they would count
# against --pids-limit forever otherwise) without docker's --init, which
# bind-mounts a host binary into the container.
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-I", "/usr/local/lib/jav3/bootstrap.py"]
