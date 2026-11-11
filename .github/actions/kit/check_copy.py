"""Checks that this vendored copy of the kit's actions is the one its lock names.

Each action runs this as its first step, so an edited copy fails the job. What it checks is the
committed copy: inside a git work tree it reads the folder it sits in, and kit.lock, from the
blobs at HEAD, so a checkout that converts line endings (core.autocrlf, or an eol rule in
.gitattributes) still passes a correct copy. A fresh CI checkout has no uncommitted edits, so
there the commit is also what runs. Outside a git work tree it reads the working files instead.

It hashes every file in the folder, leaving out kit.lock and bytecode caches, exactly as the
kit's drift check hashes a copy, and compares the result with the hash in kit.lock. An edited,
added or removed file, a symlink in the copy, a symlinked kit.lock, a copy reached through a
symlink inside the repo, or a missing or malformed lock fails.

Standard library only, with no TOML parser, so it runs on any runner's python3.
"""

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = "kit.lock"
PLAIN = ("100644", "100755")
LOCK_LINES = {
    "tag": re.compile(r'^tag = "[A-Za-z0-9][A-Za-z0-9._+-]*"$', re.MULTILINE),
    "commit": re.compile(r'^commit = "[0-9a-f]{40}"$', re.MULTILINE),
    "hash": re.compile(r'^hash = "(sha256:[0-9a-f]{64})"$', re.MULTILINE),
}


class Failed(Exception):
    pass


def skipped(rel):
    parts = rel.split("/")
    return rel == LOCK or "__pycache__" in parts or rel.endswith(".pyc")


def digest_files(files):
    """sha256 over each path, in order, with the sha256 of its bytes."""
    outer = hashlib.sha256()
    for rel in sorted(files):
        outer.update(rel.encode() + b"\0" + hashlib.sha256(files[rel]).hexdigest().encode()
                     + b"\n")
    return "sha256:" + outer.hexdigest()


def working_files(folder):
    """The folder's working files as {path: bytes}, without kit.lock or bytecode."""
    folder = Path(folder)
    files = {}
    for path in folder.rglob("*"):
        rel = path.relative_to(folder).as_posix()
        if skipped(rel):
            continue
        if path.is_symlink():
            raise Failed(f"a symlink is in the vendored copy: {rel}")
        if path.is_file():
            files[rel] = path.read_bytes()
    return files


def digest(folder):
    """The digest of the folder's working files."""
    return digest_files(working_files(folder))


def _git(args, cwd, stdin=None):
    try:
        result = subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True,
                                check=False)
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def work_tree(folder):
    """(the repo's top folder, the folder's path in it), or None outside a git work tree."""
    inside = _git(["rev-parse", "--is-inside-work-tree"], folder)
    if inside is None or inside.strip() != b"true":
        return None
    top = _git(["rev-parse", "--show-toplevel"], folder)
    prefix = _git(["rev-parse", "--show-prefix"], folder)
    if top is None or prefix is None:
        return None
    return os.path.realpath(top.decode().strip()), prefix.decode().strip()


def through_symlink(folder, top):
    """The first symlink on the way from the repo's top folder down to folder, or None."""
    path = os.path.abspath(folder)
    while os.path.realpath(path) != top:
        if os.path.islink(path):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return None


def committed_files(folder):
    """The folder at HEAD, from its blobs: ({path: bytes} without kit.lock or bytecode, the
    lock's bytes or None). None outside a git work tree."""
    found = work_tree(folder)
    if found is None:
        return None
    top, prefix = found
    link = through_symlink(folder, top)
    if link is not None:
        raise Failed(f"the vendored copy is reached through a symlink in the repo: {link}")
    listing = _git(["ls-tree", "-r", "-z", "--full-tree", "HEAD", "--", prefix or "."], top)
    if listing is None:
        raise Failed(f"HEAD can't be read in {top}, so the committed copy can't be checked")
    entries = {}
    for record in listing.decode().split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        mode, _, sha = meta.split()
        rel = path[len(prefix):]
        if "__pycache__" in rel.split("/") or rel.endswith(".pyc"):
            continue
        if mode == "120000":
            what = "kit.lock is a symlink" if rel == LOCK else f"a symlink is in the copy: {rel}"
            raise Failed(f"{what}, in the commit at {prefix or './'}")
        if mode not in PLAIN:
            raise Failed(f"{rel} in the vendored copy is not a plain file (mode {mode})")
        entries[rel] = sha
    blobs = _blobs(top, list(entries.values()))
    files = {rel: blobs[sha] for rel, sha in entries.items() if rel != LOCK}
    return files, blobs[entries[LOCK]] if LOCK in entries else None


def _blobs(top, shas):
    """{sha: bytes} for each blob, read with one cat-file --batch."""
    if not shas:
        return {}
    out = _git(["cat-file", "--batch"], top, stdin="".join(f"{s}\n" for s in shas).encode())
    if out is None:
        raise Failed("the committed copy's blobs can't be read")
    blobs, at = {}, 0
    for _ in shas:
        end = out.index(b"\n", at)
        sha, kind, size = out[at:end].decode().split()
        if kind != "blob":
            raise Failed(f"{sha} is a {kind}, not a blob")
        start = end + 1
        blobs[sha] = out[start:start + int(size)]
        at = start + int(size) + 1
    return blobs


def locked_hash(text, where):
    """The hash in a lock that holds exactly one well-formed tag, commit and hash line."""
    text = text.replace("\r\n", "\n")
    for key, pattern in LOCK_LINES.items():
        named = re.findall(rf"^{key}\s*=", text, re.MULTILINE)
        if len(named) != 1 or len(pattern.findall(text)) != 1:
            raise Failed(f"{where} must hold exactly one well-formed {key} line")
    return LOCK_LINES["hash"].search(text).group(1)


def main():
    lock_path = os.path.join(HERE, LOCK)
    try:
        committed = committed_files(HERE)
        if committed is None:
            if os.path.islink(lock_path):
                raise Failed(f"{LOCK} is a symlink: {lock_path}")
            if not os.path.isfile(lock_path):
                raise Failed(f"no {LOCK} beside the vendored actions at {HERE}")
            files, lock = working_files(HERE), Path(lock_path).read_bytes()
        else:
            files, lock = committed
            if lock is None:
                raise Failed(f"no {LOCK} committed beside the vendored actions at {HERE}")
        try:
            text = lock.decode("utf-8")
        except UnicodeDecodeError:
            raise Failed(f"{lock_path} is not UTF-8 text") from None
        locked = locked_hash(text, lock_path)
        actual = digest_files(files)
    except Failed as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    if actual != locked:
        print(f"::error::the vendored actions at {HERE} do not match {LOCK}: {actual}, locked "
              f"{locked}. Move them only with the kit's sync, which rewrites the lock.",
              file=sys.stderr)
        return 1
    source = "working files" if committed is None else "committed copy"
    print(f"the vendored actions at {HERE} match {LOCK} ({source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
