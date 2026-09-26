"""Image-variant recipes -> Dockerfiles (WP8 side of WP5's one recipe format).

ADAPTER: WP5's recipe format (vm/images/*.recipe) was not visible when this was
written. This renders from the variant shape the contract already fixes
(`POST /api/vm/images {name, from, packages:[{manager, package, version}]}`,
docs/boxes-contract.md J), so WP5 only needs `recipe_to_dict()` to parse its
file into that dict; KVM layers and Dockerfiles then come from one source.

The rendered Dockerfile builds FROM the parent variant's docker image, installs
the approved packages as root, strips setuid bits again, and drops back to the
image's non-root user. Package names/versions are validated here as well as in
WP5 (they end up in a RUN line).
"""
from __future__ import annotations

import re

from ..config import settings

MANAGERS = ("apt", "pip", "npm")
_NAME = {"apt": re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}$"),
         "pip": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}(\[[A-Za-z0-9,_-]+\])?$"),
         "npm": re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]{0,213}$")}
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+:~_-]{0,63}$")
_VARIANT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class RecipeError(ValueError):
    pass


def variant_image(variant: str) -> str:
    """Image reference of a variant: 'main' is the turn image; others are
    tagged <repo>:<variant> next to it."""
    if variant == "main":
        return settings.docker_image_turn
    repo = settings.docker_image_turn.rsplit(":", 1)[0]
    return f"{repo}:{variant}"


def _spec(p: dict) -> tuple[str, str]:
    mgr, name, ver = p.get("manager"), p.get("package"), p.get("version")
    if mgr not in MANAGERS:
        raise RecipeError(f"unknown manager {mgr!r}")
    if not isinstance(name, str) or not _NAME[mgr].match(name):
        raise RecipeError(f"bad {mgr} package name {name!r}")
    if ver not in (None, "") and (not isinstance(ver, str) or not _VERSION.match(ver)):
        raise RecipeError(f"bad version {ver!r} for {name}")
    if not ver:
        return mgr, name
    return mgr, {"apt": f"{name}={ver}", "pip": f"{name}=={ver}",
                 "npm": f"{name}@{ver}"}[mgr]


def render_dockerfile(recipe: dict) -> str:
    name, parent = recipe.get("name"), recipe.get("from") or "main"
    if not isinstance(name, str) or not _VARIANT.match(name) or name == "main":
        raise RecipeError(f"bad variant name {name!r}")
    if not _VARIANT.match(parent):
        raise RecipeError(f"bad parent variant {parent!r}")
    by: dict[str, list[str]] = {m: [] for m in MANAGERS}
    for p in recipe.get("packages") or []:
        mgr, s = _spec(p)
        by[mgr].append(s)
    lines = [f"# rendered by backend/vm/docker_recipe.py from variant {name!r}",
             f"FROM {variant_image(parent)}", "USER root"]
    if by["apt"]:
        lines.append("RUN set -eux; export DEBIAN_FRONTEND=noninteractive; apt-get update; "
                     "apt-get install -y --no-install-recommends "
                     + " ".join(by["apt"]) + "; rm -rf /var/lib/apt/lists/*")
    if by["pip"]:
        lines.append("RUN pip install --no-cache-dir --break-system-packages "
                     + " ".join(by["pip"]))
    if by["npm"]:
        lines.append("RUN npm install --global --no-fund --no-audit "
                     + " ".join(by["npm"]) + " && npm cache clean --force")
    lines += ["RUN find / -xdev -type f -perm /6000 -exec chmod a-s {} + ",
              f"LABEL jav3.variant={name} jav3.parent={parent}",
              "USER 10001:10001"]
    return "\n".join(lines) + "\n"


def build_argv(recipe: dict, context_dir: str) -> list[str]:
    """`docker build` argv (without the binary). The context holds only the
    rendered Dockerfile; the build never sees the repo."""
    return ["build", "--pull=false", "--tag", variant_image(recipe["name"]),
            "--label", "jav3.managed=1", "--file", f"{context_dir}/Dockerfile",
            context_dir]
