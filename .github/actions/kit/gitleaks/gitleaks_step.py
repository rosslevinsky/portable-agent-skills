#!/usr/bin/env python3
"""A pinned gitleaks, checked against the checksum its project publishes, and the scan.

    gitleaks_step.py install DIR    fetch the pinned release, check it, put gitleaks in DIR
    gitleaks_step.py scan           scan the checkout in the current folder

One version, so a new gitleaks can't change the result or reach the runner unnoticed. The scan
reads MODE (`history`, the default: every commit; or `files`: the current files only), CONFIG
(an optional config path) and IGNORE_GITLEAKS_ALLOW (`true`, the default, or `false`). A
finding exits 1; a bad input or a missing tool exits 2. Standard library only.
"""

import hashlib
import io
import os
import platform
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "8.30.1"
# The linux_x64 archive's sha256, as the project's checksums file gives it for this version.
SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
COMMANDS = {"history": "git", "files": "dir"}
FLAGS = ["--no-banner", "--redact", "--exit-code", "1"]


class Failed(Exception):
    pass


def url():
    archive = f"gitleaks_{VERSION}_linux_x64.tar.gz"
    return f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/{archive}"


def download():
    with urllib.request.urlopen(url(), timeout=60) as response:
        return response.read()


def unpack(data, dest):
    """Check the archive's checksum, then write only its top-level gitleaks to dest."""
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        raise Failed(f"the gitleaks archive fails its checksum: got {digest}, pinned {SHA256}")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name == "gitleaks" and m.isfile()), None)
        if member is None:
            raise Failed("the checked archive holds no gitleaks executable")
        body = tar.extractfile(member).read()
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    binary = dest / "gitleaks"
    binary.write_bytes(body)
    binary.chmod(0o755)
    return binary


def install(dest):
    if (platform.system(), platform.machine()) != ("Linux", "x86_64"):
        raise Failed(f"the pinned gitleaks is for Linux x86_64 only, not "
                     f"{platform.system()} {platform.machine()}")
    binary = unpack(download(), dest)
    path_file = os.environ.get("GITHUB_PATH")
    if path_file:
        with open(path_file, "a") as handle:
            handle.write(f"{binary.parent}\n")
    print(f"gitleaks {VERSION} checked and installed in {binary.parent}")
    return binary


def scan_args():
    mode = os.environ.get("MODE") or "history"
    if mode not in COMMANDS:
        raise Failed(f"MODE must be history or files, not {mode!r}")
    ignore = os.environ.get("IGNORE_GITLEAKS_ALLOW") or "true"
    if ignore not in ("true", "false"):
        raise Failed(f"IGNORE_GITLEAKS_ALLOW must be true or false, not {ignore!r}")
    args = [COMMANDS[mode], ".", *FLAGS]
    if ignore == "true":
        args.append("--ignore-gitleaks-allow")
    config = os.environ.get("CONFIG") or ""
    if config:
        if not Path(config).is_file():
            raise Failed(f"CONFIG names no file: {config}")
        args += ["--config", config]
    if mode == "history":
        # A shallow clone holds part of the history, and a scan of it would pass in silence.
        shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"],
                                 capture_output=True, text=True, check=False)
        if shallow.returncode != 0:
            raise Failed(f"history mode needs a git checkout here: {shallow.stderr.strip()}")
        if shallow.stdout.strip() != "false":
            raise Failed("history mode needs the full history, and this clone is shallow; "
                         "check out with fetch-depth: 0")
    return args


def scan():
    args = scan_args()
    print("gitleaks " + " ".join(args))
    try:
        return subprocess.run(["gitleaks", *args], check=False).returncode
    except OSError as error:
        raise Failed(f"gitleaks can't be run: {error}") from None


def main(argv):
    try:
        if argv[:1] == ["install"] and len(argv) == 2:
            install(argv[1])
            return 0
        if argv == ["scan"]:
            return scan()
        raise Failed("usage: gitleaks_step.py install DIR | scan")
    except Failed as error:
        print(f"gitleaks step: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
