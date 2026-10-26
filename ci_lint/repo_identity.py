"""Fleet repository identities that survive GitHub repository transfers.

The fleet's first-party repositories moved owners (zackees/ci.yml and
zackees/setup-soldr -> forge-ci/*, zackees/clud -> talos-hq/clud). GitHub
redirects the old names, so a workflow may legitimately name either; a check
that compares against one literal slug flags a correct pin. Every identity
comparison goes through the sets defined here -- add a future transfer here,
in one place.
"""

from __future__ import annotations

# Canonical (current) name first; the pre-transfer names stay accepted.
CI_YML_REPOS: tuple[str, ...] = ("forge-ci/ci.yml", "zackees/ci.yml")
SETUP_SOLDR_REPOS: tuple[str, ...] = ("forge-ci/setup-soldr", "zackees/setup-soldr")
CI_YML_REPO: str = CI_YML_REPOS[0]
SETUP_SOLDR_REPO: str = SETUP_SOLDR_REPOS[0]

# Regex fragments (no anchors, no groups that capture).
CI_YML_RE: str = r"(?:forge-ci|zackees)/ci\.yml"
SETUP_SOLDR_RE: str = r"(?:forge-ci|zackees)/setup-soldr"


def is_ci_yml(slug: object) -> bool:
    """True when `slug` (an `owner/name`) names this repository, either name."""
    return isinstance(slug, str) and slug.strip().lower() in CI_YML_REPOS


def is_setup_soldr(slug: str) -> bool:
    """True when `slug` (an `owner/name`, no sub-path or ref) is setup-soldr."""
    return slug.strip().rstrip("/").lower() in SETUP_SOLDR_REPOS


def setup_soldr_prefix(uses: str) -> str | None:
    """The setup-soldr repo slug `uses` starts with, as written, or None."""
    low = uses.lower()
    for repo in SETUP_SOLDR_REPOS:
        if low.startswith(repo):
            return uses[: len(repo)]
    return None
