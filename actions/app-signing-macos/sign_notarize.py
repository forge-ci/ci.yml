#!/usr/bin/env python3
"""macOS Developer ID signing + notarization for release jobs (REL-006, #462).

Proven procedure from obs-rust/obs-studio#2430/#2433 (run 40246027884), plus
the parts that repository did not have: a temporary keychain that is always
deleted (also on SIGINT/SIGTERM cancellation), App Store Connect API key
notary auth, and an event guard that refuses pull_request/merge_group.

Standard library only. Child output is never pipe-captured (PY-003): tool
output streams to the job log, and the one JSON result we parse is written to
a temporary file.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

REFUSED_EVENTS = frozenset({"pull_request", "pull_request_target", "merge_group"})
RELEASE_EVENTS = frozenset({"push", "release", "workflow_dispatch"})
BUNDLE_SUFFIXES = (".app", ".appex", ".xpc", ".framework", ".bundle", ".plugin")
MACHO_MAGICS = frozenset(
    {
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }
)


class SigningError(Exception):
    pass


@dataclass(frozen=True)
class NotaryAuth:
    args: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    app: Path
    artifact: Path | None
    entitlements: Path | None
    identity: str
    cert_b64: str
    cert_password: str
    notarize: bool
    api_key_b64: str
    api_key_id: str
    api_issuer_id: str
    apple_id: str
    apple_password: str
    team_id: str


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def load_config() -> Config:
    artifact = env("SIGN_ARTIFACT_PATH")
    entitlements = env("SIGN_ENTITLEMENTS")
    return Config(
        app=Path(env("SIGN_APP_PATH")),
        artifact=Path(artifact) if artifact else None,
        entitlements=Path(entitlements) if entitlements else None,
        identity=env("SIGN_IDENTITY"),
        cert_b64=env("SIGN_CERT_P12_B64"),
        cert_password=os.environ.get("SIGN_CERT_PASSWORD", ""),
        notarize=env("SIGN_NOTARIZE").lower() != "false",
        api_key_b64=env("NOTARY_API_KEY_B64"),
        api_key_id=env("NOTARY_API_KEY_ID"),
        api_issuer_id=env("NOTARY_API_ISSUER_ID"),
        apple_id=env("NOTARY_APPLE_ID"),
        apple_password=os.environ.get("NOTARY_PASSWORD", ""),
        team_id=env("NOTARY_TEAM_ID"),
    )


def check_event() -> None:
    """Release jobs only: never PR/merge-queue events, never a non-tag push."""
    if sys.platform != "darwin":
        raise SigningError("app-signing-macos needs a macOS runner")
    event = env("GITHUB_EVENT_NAME")
    if event in REFUSED_EVENTS:
        raise SigningError(f"refusing to sign on '{event}': release jobs only (REL-006)")
    if event and event not in RELEASE_EVENTS:
        raise SigningError(f"refusing to sign on '{event}': allowed {sorted(RELEASE_EVENTS)}")
    if event == "push" and not env("GITHUB_REF").startswith("refs/tags/"):
        raise SigningError("refusing to sign a non-tag push (REL-006)")


_REDACT: set[str] = set()


def redact(value: str) -> None:
    """Never echo a secret: mask it in the job log and in our own argv echo."""
    if value:
        _REDACT.add(value)
        print(f"::add-mask::{value}", flush=True)


def show(argv: tuple[str, ...]) -> None:
    print("+ " + " ".join("***" if a in _REDACT else a for a in argv), flush=True)


def run(*argv: str, check: bool = True) -> int:
    show(argv)
    code = subprocess.run(argv, check=False).returncode
    if check and code != 0:
        raise SigningError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} exited {code}")
    return code


def run_to_file(argv: tuple[str, ...], out: Path) -> int:
    show(argv)
    with out.open("wb") as fh:
        return subprocess.run(argv, stdout=fh, check=False).returncode


class Keychain:
    """Temporary keychain; `close()` always restores the search list and deletes it."""

    def __init__(self, tmp: Path) -> None:
        self.path = tmp / f"app-signing-{secrets.token_hex(4)}.keychain-db"
        self.password = secrets.token_hex(24)
        redact(self.password)
        self.original: tuple[str, ...] = ()
        self.created = False

    def _search_list(self, tmp: Path) -> tuple[str, ...]:
        out = tmp / "keychains.txt"
        if run_to_file(("security", "list-keychains", "-d", "user"), out) != 0:
            return ()
        return tuple(line.strip().strip('"') for line in out.read_text().splitlines() if line.strip())

    def open(self, p12: Path, cert_password: str, identity: str, tmp: Path) -> None:
        self.original = self._search_list(tmp)
        run("security", "create-keychain", "-p", self.password, str(self.path))
        self.created = True
        run("security", "set-keychain-settings", "-lut", "21600", str(self.path))
        run("security", "unlock-keychain", "-p", self.password, str(self.path))
        run(
            "security",
            "import",
            str(p12),
            "-P",
            cert_password,
            "-f",
            "pkcs12",
            "-k",
            str(self.path),
            "-T",
            "/usr/bin/codesign",
            "-T",
            "/usr/bin/security",
        )
        run(
            "security",
            "set-key-partition-list",
            "-S",
            "apple-tool:,apple:,codesign:",
            "-s",
            "-k",
            self.password,
            str(self.path),
        )
        run("security", "list-keychains", "-d", "user", "-s", str(self.path), *self.original)
        found = tmp / "identities.txt"
        run_to_file(("security", "find-identity", "-v", "-p", "codesigning", str(self.path)), found)
        if f'"{identity}"' not in found.read_text():
            raise SigningError("the configured identity is not in the imported .p12")

    def close(self) -> None:
        if not self.created:
            return
        if self.original:
            run("security", "list-keychains", "-d", "user", "-s", *self.original, check=False)
        run("security", "delete-keychain", str(self.path), check=False)
        self.created = False


def is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) in MACHO_MAGICS
    except OSError:
        return False


def nested_code(app: Path) -> list[Path]:
    """Every nested bundle and Mach-O, deepest first (inside-out)."""
    found: list[Path] = []
    for root, dirs, files in os.walk(app):
        base = Path(root)
        for name in dirs:
            item = base / name
            if not item.is_symlink() and name.endswith(BUNDLE_SUFFIXES):
                found.append(item)
        for name in files:
            item = base / name
            if not item.is_symlink() and is_macho(item):
                found.append(item)
    return sorted(found, key=lambda p: (len(p.parts), str(p)), reverse=True)


def sign_app(cfg: Config, keychain: Keychain) -> None:
    base = (
        "codesign",
        "--force",
        "--timestamp",
        "--options",
        "runtime",
        "--keychain",
        str(keychain.path),
        "--sign",
        cfg.identity,
    )
    for item in nested_code(cfg.app):
        extra = ("--preserve-metadata=entitlements",) if item.is_dir() else ()
        run(*base, *extra, str(item))
    if cfg.entitlements:
        run(*base, "--entitlements", str(cfg.entitlements), str(cfg.app))
    else:
        run(*base, "--preserve-metadata=entitlements,requirements,flags", str(cfg.app))
    run("codesign", "--verify", "--strict", "--deep", "--verbose=2", str(cfg.app))


def notary_auth(cfg: Config, tmp: Path) -> NotaryAuth:
    if cfg.api_key_b64 and cfg.api_key_id and cfg.api_issuer_id:
        key = tmp / f"AuthKey_{cfg.api_key_id}.p8"
        key.write_bytes(base64.b64decode(cfg.api_key_b64))
        key.chmod(0o600)
        return NotaryAuth(("--key", str(key), "--key-id", cfg.api_key_id, "--issuer", cfg.api_issuer_id))
    if cfg.apple_id and cfg.apple_password:
        team = cfg.team_id
        if not team:
            match = re.search(r"\(([A-Z0-9]{10})\)\s*$", cfg.identity)
            team = match.group(1) if match else ""
        if not team:
            raise SigningError("notary-team-id is required with Apple ID notary auth")
        return NotaryAuth(("--apple-id", cfg.apple_id, "--password", cfg.apple_password, "--team-id", team))
    raise SigningError("notarization needs an App Store Connect API key (or Apple ID fallback)")


def notarize(submit: Path, auth: NotaryAuth, tmp: Path) -> str:
    out = tmp / "notary-submit.json"
    code = run_to_file(
        ("xcrun", "notarytool", "submit", str(submit), *auth.args, "--wait", "--output-format", "json"),
        out,
    )
    text = out.read_text()
    print(text, flush=True)
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = {}
    submission = str(result.get("id", "")) if isinstance(result, dict) else ""
    status = str(result.get("status", "")) if isinstance(result, dict) else ""
    if code != 0 or status != "Accepted":
        if submission:
            run("xcrun", "notarytool", "log", submission, *auth.args, check=False)
        raise SigningError(f"notarization status '{status or 'unknown'}' (submission {submission or 'unknown'})")
    return submission


def ditto_zip(app: Path, dest: Path) -> None:
    dest.unlink(missing_ok=True)
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(dest))


def validate(cfg: Config) -> None:
    if not cfg.app.is_dir() or cfg.app.suffix != ".app":
        raise SigningError(f"app-path is not an .app bundle: {cfg.app}")
    for name, value in (("identity", cfg.identity), ("certificate-p12-base64", cfg.cert_b64)):
        if not value:
            raise SigningError(f"missing {name}: an unsigned release is a failed release")
    if cfg.artifact is not None and cfg.artifact.suffix not in (".dmg", ".zip"):
        raise SigningError(f"artifact-path must be a .dmg or .zip: {cfg.artifact}")


def staple_and_assess(cfg: Config, dmg: Path | None) -> None:
    target = dmg if dmg is not None else cfg.app
    run("xcrun", "stapler", "staple", str(target))
    run("xcrun", "stapler", "validate", str(target))
    if dmg is not None:
        run("spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=2", str(dmg))
        return
    run("spctl", "--assess", "--type", "execute", "--verbose=2", str(cfg.app))
    if cfg.artifact is not None:
        ditto_zip(cfg.app, cfg.artifact)  # re-zip so the shipped zip holds the stapled app


def sign_notarize(cfg: Config, tmp: Path, keychain: Keychain) -> str:
    validate(cfg)
    p12 = tmp / "signing.p12"
    p12.write_bytes(base64.b64decode(cfg.cert_b64))
    p12.chmod(0o600)
    keychain.open(p12, cfg.cert_password, cfg.identity, tmp)
    sign_app(cfg, keychain)
    artifact = cfg.artifact
    dmg = artifact if artifact is not None and artifact.suffix == ".dmg" else None
    if dmg is not None:
        run("codesign", "--force", "--timestamp", "--keychain", str(keychain.path), "--sign", cfg.identity, str(dmg))
    elif artifact is not None:
        ditto_zip(cfg.app, artifact)
    if not cfg.notarize:
        return ""
    auth = notary_auth(cfg, tmp)
    submit = artifact if artifact is not None else tmp / f"{cfg.app.stem}-notarize.zip"
    if artifact is None:
        ditto_zip(cfg.app, submit)
    submission = notarize(submit, auth, tmp)
    staple_and_assess(cfg, dmg)
    return submission


def _raise_on_signal(signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


def main() -> int:
    signal.signal(signal.SIGTERM, _raise_on_signal)
    signal.signal(signal.SIGINT, _raise_on_signal)
    cfg = load_config()
    redact(cfg.cert_password)
    redact(cfg.apple_password)
    tmp = Path(tempfile.mkdtemp(prefix="app-signing-", dir=os.environ.get("RUNNER_TEMP") or None))
    keychain = Keychain(tmp)
    try:
        check_event()
        submission = sign_notarize(cfg, tmp, keychain)
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as fh:
                fh.write(f"submission-id={submission}\n")
        return 0
    except SigningError as exc:
        print(f"::error::{exc}", flush=True)
        return 1
    finally:
        keychain.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
