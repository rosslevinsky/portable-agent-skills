#!/usr/bin/env python3
"""Every text shipped Python decodes names the codec that decodes it.

Three ways in: a file it reads, a file it writes, and the output of a process it runs. All
three default to the host's locale encoding, which is ASCII under `LC_ALL=C` and the ANSI
code page on Windows — cp1252, cp932, cp949.

The CI encoding proxy (`PYTHONUTF8=0 LC_ALL=C LANG=C`) catches this class only
*behaviourally*: a bare `read_text()` fails there only if the file it happens to read
contains a non-ASCII byte. That is how twelve of them survived in
`validate_cross_runtime.py`, and a bare read against a currently-ASCII file breaks the day
someone adds an em dash to it.

This test is the exhaustive half. It reads the source, not the behaviour, so a bare call is
a failure the moment it is written, whatever the file it points at contains today.

Scope is the Python that SHIPS, discovered rather than listed — see `shipped_python()`.
Tooling no user receives is out of scope.
"""
import ast
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import validate_cross_runtime as vcr  # noqa: E402


def shipped_python() -> list[Path]:
    """Every `.py` a user receives: the installer, the validator, and the skills' engines.

    The skills half is DISCOVERED rather than listed: everything under `skills/` reaches a
    user, so a new bundled engine is covered the day it is added. The other two are named
    explicitly, because neither `scripts/` nor the repository root is wholly shipped, so the
    directory is not the unit there.

    `install.py` was missing from this list while the docstring claimed "every `.py` a user
    receives", and it is the one Python file EVERY user runs. It cost a real defect:
    `main()` never reconfigured its output streams, so `--verify` redirected to a file on a
    cp932 console raised UnicodeEncodeError while reporting a healthy install.

    A stricter version would parse that set out of configuration rather than restating it,
    and is deliberately not written: TOML parsing needs `tomllib`, and this project's floor
    is Python 3.10, which predates it.
    """
    found = [REPO_ROOT / "install.py", REPO_ROOT / "scripts/validate_cross_runtime.py"]
    # Scope, case and the residue filter all come from the validator's traversal. The
    # `rglob` plus private `"__pycache__" not in p.parts` test this replaces was the third
    # of three separate residue filters in the repository, and the one furthest from the
    # other two — which is how they got out of step.
    found += sorted(
        path
        for skill_md in vcr.iter_skill_roots(REPO_ROOT / "skills")
        for path, _relative, suffix in vcr.walk_tree_files(skill_md.parent)
        if suffix == ".py"
    )
    return found


# `Path.read_text` / `Path.write_text` are text-mode by definition, so a missing
# `encoding=` on either is unconditionally wrong.
TEXT_METHODS = {"read_text", "write_text"}


def _has_encoding(call: ast.Call) -> bool:
    return any(kw.arg == "encoding" for kw in call.keywords)


def _is_binary_open(call: ast.Call) -> bool:
    """True when every mode this `open()` can take is a binary one.

    The mode is not always a bare literal. `open(p, "ab" if append else "wb")` is a real
    call in `plan_duel.py`, and it is unambiguously binary even though neither branch is
    the whole argument. So collect every string constant reachable in the mode expression
    and require them all to be binary.

    A mode with no string constants at all — a plain variable — yields False and is
    reported, which is the conservative reading: a caller that computes its mode somewhere
    else is exactly the one worth a human glance.
    """
    node = None
    if len(call.args) >= 2:
        node = call.args[1]
    for kw in call.keywords:
        if kw.arg == "mode":
            node = kw.value
    if node is None:  # `open(p)` — text mode by default
        return False
    modes = [n.value for n in ast.walk(node)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return bool(modes) and all("b" in m for m in modes)


def _is_bare_text_subprocess(call: ast.Call) -> bool:
    """A call asking for decoded output without saying which codec decodes it.

    `text=True` / `universal_newlines=True` decode with the LOCALE's encoding, which is
    ASCII under `LC_ALL=C` and the ANSI code page on Windows — cp1252, cp932, cp949. A
    child process writing UTF-8 then either raises `UnicodeDecodeError` in the PARENT, or,
    worse, decodes to something plausible and wrong.

    Matched on the keyword rather than on the callee, so it holds however `subprocess` is
    spelled at the call site. The correct answer for the path case — read bytes, decode
    once, explicitly — was written down long before two shipped call sites got it.
    """
    text_mode = any(
        kw.arg in ("text", "universal_newlines")
        and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in call.keywords
    )
    return text_mode and not _has_encoding(call)


def _offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in TEXT_METHODS:
            if not _has_encoding(node):
                found.append(f"{path.name}:{node.lineno}: bare .{func.attr}()")
        elif isinstance(func, ast.Name) and func.id == "open":
            if not _is_binary_open(node) and not _has_encoding(node):
                found.append(f"{path.name}:{node.lineno}: bare open() in text mode")
        if _is_bare_text_subprocess(node):
            found.append(f"{path.name}:{node.lineno}: text=True with no encoding=")
    return found


class EncodingHygieneTests(unittest.TestCase):
    def test_shipped_python_always_names_its_encoding(self):
        paths = shipped_python()
        self.assertGreaterEqual(
            len(paths), 3,
            "discovery found almost nothing — the glob is broken, and a check that "
            f"inspects no files passes for the wrong reason: {paths}")
        offenders = []
        for path in paths:
            self.assertTrue(path.is_file(), f"{path} is shipped but missing")
            offenders.extend(_offenders(path))
        self.assertEqual(
            offenders,
            [],
            "shipped Python must name a codec on every text read, text write and decoded "
            "subprocess; the default is the host's locale, which is cp1252 on Windows and "
            "ASCII under LC_ALL=C — it mangles the em dashes these files are full of, and "
            "raises on the raw path bytes git emits:\n  " + "\n  ".join(offenders),
        )

    def test_the_check_can_actually_fail(self):
        """A guard nobody has seen fail is a guard nobody has tested."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.py"
            bad.write_text(
                "from pathlib import Path\n"
                "Path('x').read_text()\n"
                "Path('y').write_text('z')\n"
                "open('a')\n"
                "open('b', 'rb')\n"
                "open('c', 'ab' if flag else 'wb')\n"
                "open('d', 'a' if flag else 'w')\n"
                "Path('ok').read_text(encoding='utf-8')\n"
                "subprocess.run(cmd, capture_output=True, text=True)\n"
                "subprocess.run(cmd, capture_output=True)\n"
                "subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')\n"
                "subprocess.check_output(cmd, universal_newlines=True)\n",
                encoding="utf-8",
            )
            found = _offenders(bad)
        # bare read_text, bare write_text, `open('a')`, the all-text conditional mode, and
        # the two subprocess calls that decode without naming a codec. `open('b','rb')` and
        # the all-binary conditional decode nothing and are not reported — the distinction
        # a cruder mode check got wrong on a real call. Nor is the subprocess that returns
        # bytes, nor the one that names its encoding.
        self.assertEqual(len(found), 6, f"expected 6 offenders, got {found}")
        self.assertTrue(any("read_text" in f for f in found))
        self.assertTrue(any("write_text" in f for f in found))
        self.assertEqual(sum("open()" in f for f in found), 2)
        self.assertEqual(sum("text=True with no encoding=" in f for f in found), 2)


def _is_main_guard(node: ast.stmt) -> bool:
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__")


def _reconfigures_a_stream(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "reconfigure"
        and any(kw.arg == "encoding" for kw in node.keywords)
        for node in ast.walk(tree)
    )


class EntrypointsPinTheirOutputEncoding(unittest.TestCase):
    """Reads are pinned at every call site; this is the other end of the pipe.

    What is written to a console is decided by the console: on Windows that is the code page,
    cp932 and cp949 among them, and none of these files can print a paragraph without an em
    dash. So an entrypoint that does not reconfigure its streams dies *reporting* — after the
    work succeeded, which is the worst moment for it.

    Source-read rather than behavioural, on the same argument the module docstring makes: a
    run only fails today if the message it happens to print carries a non-ASCII byte. The
    behavioural half — which also catches a preamble placed after the first print — is
    `test_install_py.OutputSurvivesANonUtf8Console`.
    """

    def test_every_shipped_entrypoint_reconfigures_stdout_and_stderr(self):
        entrypoints = []
        for path in shipped_python():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if not any(_is_main_guard(node) for node in tree.body):
                continue        # a bundled module, run by an entrypoint, never directly
            entrypoints.append(path.name)
            self.assertTrue(
                _reconfigures_a_stream(tree),
                f"{path.relative_to(REPO_ROOT)} is run directly by a user and never pins "
                f"its output encoding: on a console whose code page cannot represent an em "
                f"dash it raises UnicodeEncodeError while printing its own report")
        self.assertIn(
            "install.py", entrypoints,
            "the installer is the first command a user runs and must be among the "
            f"entrypoints checked here; found {sorted(entrypoints)}")
        self.assertGreaterEqual(
            len(entrypoints), 3,
            f"discovery found almost no entrypoints — this passed for the wrong reason: "
            f"{sorted(entrypoints)}")


# `PowerShellEncodingTests` lived here, checking that every shipped `.ps1` was ASCII or
# carried a UTF-8 BOM. It is gone because its subject is: `install.ps1`,
# `install.Tests.ps1` and `ci-windows.ps1` were deleted with the shell installers, and this
# repository now ships no PowerShell at all.
#
# Worth recording HOW that was noticed. The class kept passing locally after the deletion —
# its `rglob("*.ps1")` was finding `.ps1` files inside stale checkouts of older branches left
# in the working copy. CI, on a fresh clone, failed immediately. A whole-tree glob run from a
# working copy can be green for a reason that has nothing to do with the repository.


if __name__ == "__main__":
    unittest.main()
