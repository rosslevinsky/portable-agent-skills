#!/usr/bin/env python3
"""Assertions about what the shipped skill text tells a runtime to *do*.

The `test_validate_*.md` fixtures beside this file exercise rules inside
`validate_cross_runtime.py`; these are the other thing — facts about the skills themselves.
"This skill pushes to the default branch" has no validator rule behind it and never will.
**"Runtime documents"** means every `*.md` under `skills/` except `DECISIONS.md`: a ledger
recording *why* a rule was dropped must be free to quote the dropped rule.
"""
import argparse
import dataclasses
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS = REPO_ROOT / "skills"

sys.path.insert(0, str(REPO_ROOT / "scripts"))


def working_bash():
    """A bash that can actually RUN something, or `None`.

    Not `shutil.which("bash")`. Windows ships `C:\\Windows\\System32\\bash.exe` — the WSL
    launcher — which is on `PATH` whether or not a distribution is installed. Without one it
    exits non-zero and writes nothing, so a suite that trusted `which` ran every case against
    a shell that never started and read the empty output as a failed assertion.

    Duplicated from the other suite that needs it rather than shared, for the reason its copy
    gives: these modules are self-contained by design, and a `tests/` package would be a
    bigger change than the eight lines it saves.
    """
    for candidate in (os.environ.get("BASH"), shutil.which("bash")):
        if not candidate:
            continue
        try:
            probe = subprocess.run([candidate, "-c", "printf ok"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "ok":
            return candidate
    return None


BASH = working_bash()


def run_bash(code, **kwargs):
    """Run a shipped shell block from a FILE, never as `bash -c <string>`.

    An argv is encoded with the filesystem encoding. These blocks carry em dashes in their
    comments, so under a non-UTF-8 locale — which CI runs deliberately, as a proxy for
    Windows' encoding — passing one as an argument raises UnicodeEncodeError before bash is
    even started. A file is bytes on disk and its name is ASCII, so neither has to survive
    an encode. It is also what the blocks themselves tell a caller to do.
    """
    for key, value in (("capture_output", True), ("text", True), ("encoding", "utf-8"),
                       ("errors", "replace"), ("timeout", 60)):
        kwargs.setdefault(key, value)
    with tempfile.TemporaryDirectory() as holder:
        script = Path(holder) / "block.sh"
        script.write_text(code, encoding="utf-8")
        return subprocess.run([BASH, str(script)], **kwargs)
import validate_cross_runtime as vcr  # noqa: E402

# The skills that push on the user's behalf at a phase boundary, unattended. `commit` also
# pushes and is deliberately NOT here: it publishes because someone asked it to, in a
# conversation, and whether that ask has to be explicit is a separate question about that
# skill. The hazard these two carry is the one an unattended run walks into — a push nobody
# was watching, to whatever branch happened to be checked out.
PUSHING_EXECUTORS = ("plan-run", "plan-run-v1")

# A guard that derives the default branch rather than assuming `main`, plus the literal
# fallbacks for a repository the remote never answered for. All four push blocks ship the
# same shape; naming it here is what makes "port it" checkable.
#
# The derivation must ASK THE REMOTE. Reading `refs/remotes/origin/HEAD` was the earlier
# shape and it has a hole: the command that sets that ref refuses unless the matching
# tracking ref already exists, so a clone that never fetched the default branch leaves the
# variable empty — and a trunk named neither `main` nor `master` then clears the literal
# fallbacks too, and the push lands on it. `ls-remote --symref` needs no tracking ref.
_DEFAULT_BRANCH_DERIVATION = "ls-remote --symref"

# The variable the block bound the CURRENT BRANCH to. Every comparison below must name it:
# `[ "$mode" = "main" ]` tests something else entirely and guards nothing, while reading
# exactly like a guard to anything that only looks for `main`.
_BRANCH_VAR = re.compile(r'(\w+)\s*=\s*"?\$\(\s*git rev-parse --abbrev-ref HEAD')


def _comparison(var: str, literal: str) -> re.Pattern:
    """`$var` compared against `literal`, with the operator captured.

    `!=` before `=`, so the alternation never reads the `!` off an inequality and calls it
    an equality — which would invert the answer on the one shape that matters most.
    """
    return re.compile(r'"?\$\{?' + re.escape(var) + r'\}?"?\s*(!=|==?)\s*"?[^"\]]*?(?:'
                      + literal + r')')


_DEFAULT_BRANCH_LITERAL = r"main|master"
_DETACHED_HEAD_LITERAL = r"HEAD"

# `git rev-parse --abbrev-ref HEAD` answers the literal string `HEAD` when the checkout is
# detached, and `git push origin HEAD` then has no destination ref. The guard is a test of
# the branch variable against that literal: `[ "$BRANCH" = "HEAD" ]`, `[[ $b == HEAD ]]`.
# Deliberately not satisfied by the word "detached" in a comment — comments are stripped
# before this runs, because a warning is not a guard.

# Writing to a `.gitignore` a user never offered. The marker is the literal *filename*
# beside a write verb, which is narrower than it may look and narrow on purpose: four
# skills say "ensure the repo gitignores <the output directory>" about their own disposable
# artifacts, and none of them names the file or prescribes an edit to it. Stating the state
# a repo should be in is not the defect. Editing tracked config during what the user asked
# to be a read-only pass is.
_WRITE_VERB = re.compile(
    # Spelled out rather than as `add\w*`, which also matches "additionally" — a stem
    # wide enough to catch an ordinary adverb would make this assertion unfalsifiable.
    r"\b(?:creat(?:e|es|ed|ing|ion)|append(?:s|ed|ing)?|add(?:s|ed|ing)?"
    r"|writ(?:e|es|ing|ten)|edit(?:s|ed|ing)?|modif(?:y|ies|ied|ying|ication)"
    r"|updat(?:e|es|ed|ing)|touch(?:es|ed)?|plac(?:e|es|ed|ing)|put(?:s|ting)?"
    r"|insert(?:s|ed|ing)?|ensur(?:e|es|ing)|contain(?:s|ing)?|includ(?:e|es|ing))\b",
    re.IGNORECASE,
)

# A prohibition is the opposite of the defect, and the sentence that *fixes* this is
# likeliest to read "must not create or edit a `.gitignore`". Flagging that would leave the
# assertion with no wording that satisfies it. Deliberately excludes "without" and a bare
# "no": the instruction in the tree reads "if it exists **without** the entry, append", and
# a negator that broad would suppress the very offense this looks for.
_NEGATOR = re.compile(
    r"\b(?:not|never|don't|cannot|can't|rather than|instead of|avoid|refrain"
    r"|no need|nothing to)\b",
    re.IGNORECASE,
)

# Clauses, not sentences. A negation binds to its own clause: "do not touch unrelated files;
# create `.gitignore` if absent" prohibits one thing and instructs another, and reading the
# whole sentence as negated would let the instruction through.
_CLAUSE = re.compile(r"(?<=[.!?])\s+|[;\n]")


def _instructs_gitignore_write(text: str) -> bool:
    """A clause naming `.gitignore` and telling someone to change it, negation aside."""
    return any(
        ".gitignore" in clause
        and _WRITE_VERB.search(clause)
        and not _NEGATOR.search(clause)
        for clause in _CLAUSE.split(text)
    )

# CommonMark: a fence closes only on a run of the SAME character at least as long as the
# opener. Closing on any fence would end a ```` ```` block at the first ``` inside it — a
# heredoc, a nested example — and everything after would go unscanned.
_FENCE = re.compile(r"^[ \t]*(?:>[ \t]?)*(`{3,}|~{3,})")

# A shell conditional. `if`/`elif` open a chain and carry their condition; `fi` closes it;
# `else` leaves the chain in place, because the condition is still what decided which branch
# the push landed in. Matched against `;`-separated SEGMENTS rather than whole lines, so
# `if X; then :; fi` opens and closes on the one line it occupies.
_IF = re.compile(r"^\s*(el)?if\b(.*)$")
_FI = re.compile(r"^\s*fi\b")
_ELSE = re.compile(r"^\s*else\b")
# A `#` that opens a comment is preceded by whitespace or nothing. One preceded by any
# other character is inside a word — `sed 's#^origin/##'` uses it as a delimiter, and
# cutting there would delete the default-branch derivation this file exists to look for.
_SHELL_COMMENT = re.compile(r"(?m)(?:(?<=\s)|(?<=^))#.*$")


def skill_runtime_documents(skill_dir: Path) -> list[Path]:
    """Every `*.md` a runtime reads inside ONE skill, its ledger excluded.

    The exclusion is BY SKILL-RELATIVE PATH, matching the validator, not by basename: the two
    differ on a `references/DECISIONS.md`, which the validator treats as ordinary prose and a
    basename rule would skip. Scope and case come from the shared walk, since `rglob("*.md")`
    matched neither `GUIDE.MD` nor a symlink's refusal. Split from :func:`runtime_documents`
    rather than sharing a `root` parameter that meant a skill to one caller and the whole
    skills directory to another.
    """
    return sorted(
        path
        for path, relative, suffix in vcr.walk_tree_files(skill_dir)
        if suffix == ".md" and str(relative) != vcr.LEDGER_FILENAME
    )


def runtime_documents() -> list[Path]:
    """The same, for every skill the validator agrees to walk."""
    return sorted(
        path
        for skill_md in vcr.iter_skill_roots(SKILLS)
        for path in skill_runtime_documents(skill_md.parent)
    )


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _skill_of(path: Path) -> str:
    """The skill a document belongs to — the directory directly under `skills/`.

    Not `path.parent.name`, which answers `references` for half the corpus. A failure here
    is read by someone deciding which skill to open, so it has to name the skill.
    """
    return path.relative_to(SKILLS).parts[0]


def fenced_blocks(text: str) -> list[tuple[int, str]]:
    """`(1-based line of the opening fence, block body)` for every fenced block."""
    blocks, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        opener = _FENCE.match(lines[i])
        if opener:
            marker = opener.group(1)
            start, body = i + 1, []
            i += 1
            while i < len(lines):
                closer = _FENCE.match(lines[i])
                if (closer and closer.group(1)[0] == marker[0]
                        and len(closer.group(1)) >= len(marker)):
                    break
                body.append(lines[i])
                i += 1
            blocks.append((start, "\n".join(body)))
        i += 1
    return blocks


def shell_code(block: str) -> str:
    """The block with comments removed, so prose in a `#` line cannot satisfy a guard."""
    return _SHELL_COMMENT.sub("", block)


def enclosing_conditions(code: str) -> list[tuple[int, str]]:
    """`(0-based line, the conditions RULED OUT before that push runs)` for every push.

    **Polarity is the whole question.** A push reached only when the branch *is* `main`
    mentions `main` in its condition exactly as a correct guard does, so asking "is `main` in
    there?" passes the inverted guard. What is returned is what the push's position *denies*:
    the conditions of earlier branches in each `if`/`elif`/`else` chain it sits inside.
    """
    # One frame per open chain: `own` is the condition of the branch currently being read,
    # `denied` is every earlier branch's condition in that same chain.
    stack: list[dict] = []
    sites = []
    for i, line in enumerate(code.splitlines()):
        for segment in line.split(";"):
            opened = _IF.match(segment)
            if opened and opened.group(1) and stack:      # elif: the branch above is denied
                stack[-1]["denied"].append(stack[-1]["own"])
                stack[-1]["own"] = opened.group(2)
            elif opened:
                stack.append({"own": opened.group(2), "denied": []})
            elif _ELSE.match(segment) and stack:          # else: same, with no condition
                stack[-1]["denied"].append(stack[-1]["own"])
                stack[-1]["own"] = ""
            elif _FI.match(segment) and stack:
                stack.pop()
            if "git push" in segment:
                sites.append((
                    i,
                    " ".join(c for f in stack for c in f["denied"]),
                    " ".join(f["own"] for f in stack),
                ))
    return sites


def _tested(pattern: re.Pattern, denied: str, own: str) -> bool:
    """Is the hazard ruled out before the push — either way round?

    Two shapes are correct and only two. Either the equality is DENIED, because the push sits
    past it in the chain (`if on-main; then skip; else push; fi`), or the INEQUALITY governs
    the push directly. An equality governing the push is the guard inverted, and an
    inequality merely denied is the same thing.
    """
    return (any(m.group(1) == "=" for m in pattern.finditer(denied))
            or any(m.group(1) == "!=" for m in pattern.finditer(own)))


def push_sites(skill: str) -> list[tuple[Path, int, str, str, str]]:
    """`(document, 1-based push line, denied conditions, governing conditions, code)`.

    Detection runs on the code with comments stripped, so a `git push` written inside a
    `#` comment is not mistaken for one the runtime would execute.
    """
    sites = []
    for doc in skill_runtime_documents(SKILLS / skill):
        for fence, block in fenced_blocks(doc.read_text(encoding="utf-8")):
            code = shell_code(block)
            for offset, denied, own in enclosing_conditions(code):
                sites.append((doc, fence + 1 + offset, denied, own, code))
    return sites


def unguarded(predicate) -> list[str]:
    """Push sites across the executors that `predicate` says are unguarded."""
    return [
        f"{skill}: {_rel(doc)}:{line}"
        for skill in PUSHING_EXECUTORS
        for doc, line, denied, own, code in push_sites(skill)
        if not predicate(denied, own, code)
    ]


def _guards(literal: str, denied: str, own: str, code: str) -> bool:
    """Is `literal` ruled out for the branch variable this block actually derived?"""
    var = _BRANCH_VAR.search(code)
    return bool(var) and _tested(_comparison(var.group(1), literal), denied, own)


def guards_default_branch(denied: str, own: str, code: str) -> bool:
    """The default branch is derived in the block, and ruled out before the push."""
    return (_DEFAULT_BRANCH_DERIVATION in code
            and _guards(_DEFAULT_BRANCH_LITERAL, denied, own, code))


def guards_detached_head(denied: str, own: str, code: str) -> bool:
    return _guards(_DETACHED_HEAD_LITERAL, denied, own, code)


def gitignore_writes() -> list[str]:
    """Runtime documents that tell a runtime to create or edit a `.gitignore`."""
    offences = []
    for doc in runtime_documents():
        lines = doc.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            if ".gitignore" not in line:
                continue
            # Two lines of context, then re-joined into one paragraph before it is split
            # into clauses: prose wraps mid-sentence, so the filename and the verb that
            # acts on it routinely sit on different lines of the same instruction.
            window = " ".join(lines[max(0, i - 2): i + 2])
            if _instructs_gitignore_write(window):
                offences.append(f"{_skill_of(doc)}: {_rel(doc)}:{i}")
    return sorted(set(offences))


class PushGuards(unittest.TestCase):
    """A plan executor pushes unattended, so every push path needs both guards."""

    def test_the_push_sites_are_actually_found(self):
        """Anchor against vacuity: a green run below must not mean nothing was scanned."""
        found = {skill: push_sites(skill) for skill in PUSHING_EXECUTORS}
        for skill, sites in found.items():
            self.assertTrue(sites, f"no `git push` block found in {skill} — discovery broke")
        self.assertGreaterEqual(
            sum(len(s) for s in found.values()), 3,
            f"expected at least three push paths across the executors, found {found}")

    def test_every_push_path_is_guarded_against_the_default_branch(self):
        offences = unguarded(guards_default_branch)
        self.assertEqual(
            offences, [],
            "a phase-boundary push must never land on the trunk. The guard derives the "
            "default branch from the remote's advertised HEAD (`git ls-remote --symref "
            "origin HEAD`) and compares the current branch to it, falling back to "
            "`main`/`master` where the remote did not answer — then skips the push and says "
            "so, rather than failing. Reading `refs/remotes/origin/HEAD` instead is the "
            "shape with the hole: it is unset in a clone that never fetched the default "
            "branch, and a trunk named neither `main` nor `master` then passes every "
            "test:\n  " + "\n  ".join(offences))

    def test_every_push_path_is_guarded_against_a_detached_head(self):
        offences = unguarded(guards_detached_head)
        self.assertEqual(
            offences, [],
            "`git rev-parse --abbrev-ref HEAD` returns the literal `HEAD` when the "
            "checkout is detached, and `git push origin HEAD` then has no destination "
            "ref. Test the branch value against that literal — `[ \"$BRANCH\" = \"HEAD\" ]` "
            "— in executable code, not in a comment:\n  " + "\n  ".join(offences))

    def test_a_push_the_tokens_surround_but_no_branch_guards(self):
        """The shape a token scan waves through: everything present, nothing conditional.

        A `guards_*` predicate reading the whole block passes a snippet that derives the
        default branch, mentions `main`, binds `HEAD` and then pushes unconditionally —
        every marker in place, no guard anywhere. These read the conditions *enclosing the
        push*.
        """
        code = shell_code(
            'default="$(git ls-remote --symref origin HEAD)"\n'
            'echo main\n'
            'branch=HEAD\n'
            'git push origin HEAD\n')
        (_, denied, own), = enclosing_conditions(code)
        self.assertEqual((denied, own.strip()), ("", ""))
        self.assertFalse(guards_default_branch(denied, own, code))
        self.assertFalse(guards_detached_head(denied, own, code))

    def test_a_conditional_that_does_not_test_the_branch_is_not_a_guard(self):
        code = ('default="$(git ls-remote --symref origin HEAD)"\n'
                'if [ -n "$default" ]; then\n  git push origin HEAD\nfi\n')
        (_, denied, own), = enclosing_conditions(code)
        self.assertFalse(guards_default_branch(denied, own, code))

    def test_the_guard_shape_plan_run_v1_already_ships_is_recognised(self):
        """The predicates must accept the fix, or phase 2 has nothing it can write."""
        code = shell_code(
            'branch="$(git rev-parse --abbrev-ref HEAD)"\n'
            'default="$(git ls-remote --symref origin HEAD 2>/dev/null)"\n'
            'if [ "$branch" = "HEAD" ]; then\n'
            '  echo "Detached HEAD — committed but NOT pushing."\n'
            'elif [ "$branch" = "${default:-main}" ] || [ "$branch" = "master" ]; then\n'
            '  echo "On default branch — committed but NOT pushing."\n'
            'else\n'
            '  git push origin HEAD\n'
            'fi\n')
        (_, denied, own), = enclosing_conditions(code)
        self.assertTrue(guards_default_branch(denied, own, code))
        self.assertTrue(guards_detached_head(denied, own, code))

    def test_a_push_written_only_in_a_comment_is_not_a_push_site(self):
        self.assertEqual(enclosing_conditions(shell_code("# never run git push here\n")), [])

    def test_a_conditional_that_opens_and_closes_on_one_line_does_not_leak(self):
        """Read line-wise, `if X; then :; fi` never closes and its condition is inherited.

        The push below is unconditional, and a scanner still holding the guard above it
        would call it guarded — the single worst answer this file can give.
        """
        code = ('branch="$(git rev-parse --abbrev-ref HEAD)"\n'
                'default="$(git ls-remote --symref origin HEAD)"\n'
                'if [ "$branch" = "HEAD" ] || [ "$branch" = "main" ]; then :; fi\n'
                'git push origin HEAD\n')
        (_, denied, own), = enclosing_conditions(code)
        self.assertEqual((denied, own.strip()), ("", ""))
        self.assertFalse(guards_default_branch(denied, own, code))
        self.assertFalse(guards_detached_head(denied, own, code))

    def test_a_push_on_the_same_line_as_then_is_still_seen_as_guarded(self):
        """The inequality governing the push directly — the other correct shape."""
        code = ('branch="$(git rev-parse --abbrev-ref HEAD)"\n'
                'default="$(git ls-remote --symref origin HEAD)"\n'
                'if [ "$branch" != "main" ]; then git push origin HEAD; fi\n')
        (_, denied, own), = enclosing_conditions(code)
        self.assertTrue(guards_default_branch(denied, own, code))

    def test_a_comparison_against_something_other_than_the_branch_is_not_a_guard(self):
        """`[ "$mode" = "main" ]` reads like a guard and tests nothing about the branch."""
        code = ('branch="$(git rev-parse --abbrev-ref HEAD)"\n'
                'default="$(git ls-remote --symref origin HEAD)"\n'
                'if [ "$mode" = "main" ]; then :; else git push origin HEAD; fi\n')
        (_, denied, own), = enclosing_conditions(code)
        self.assertFalse(guards_default_branch(denied, own, code))

    def test_the_accepted_guard_shapes_are_these_and_only_these(self):
        """What satisfies these assertions, written down so a future author can satisfy it.

        A `case`/`esac` guard, a guard inside a shell function, and a condition wrapped across
        lines are **not** recognized — a recorded limitation: each errs toward reporting a
        push as *unguarded*, so the failure mode is a red suite over correct prose, never a
        green suite over a push to the trunk.
        """
        derive = ('branch="$(git rev-parse --abbrev-ref HEAD)"\n'
                  'default="$(git ls-remote --symref origin HEAD)"\n')
        for shape, code in (
            ("equality denied by an earlier branch",
             derive + 'if [ "$branch" = "main" ]; then :; else git push origin HEAD; fi\n'),
            ("inequality governing the push",
             derive + 'if [ "$branch" != "main" ]; then git push origin HEAD; fi\n'),
        ):
            with self.subTest(shape=shape):
                (_, denied, own), = enclosing_conditions(code)
                self.assertTrue(guards_default_branch(denied, own, code))

    def test_the_guard_written_backwards_is_not_a_guard(self):
        """The defect itself, spelled as its own fix.

        `if on-main; then push; fi` mentions `main` in a condition exactly as the correct
        guard does, so a check asking only "is `main` tested here?" passes it — and it pushes
        to the trunk on every run.
        """
        for cmp_, predicate in ((' = "main"', guards_default_branch),
                                (' = "HEAD"', guards_detached_head)):
            with self.subTest(cmp_=cmp_):
                code = ('branch="$(git rev-parse --abbrev-ref HEAD)"\n'
                        'default="$(git ls-remote --symref origin HEAD)"\n'
                        f'if [ "$branch"{cmp_} ]; then git push origin HEAD; fi\n')
                (_, denied, own), = enclosing_conditions(code)
                self.assertFalse(predicate(denied, own, code))

    def test_a_longer_fence_is_not_closed_by_a_shorter_one_inside_it(self):
        """A ````-fenced block quoting ``` must not end there, hiding the push below."""
        text = ("````bash\n"
                "cat <<'EOF'\n```\nEOF\n"
                "git push origin HEAD\n"
                "````\n")
        (fence, body), = fenced_blocks(text)
        self.assertIn("git push", body)
        self.assertEqual(len(enclosing_conditions(shell_code(body))), 1)

    def test_a_tilde_fence_is_scanned_too(self):
        (_, body), = fenced_blocks("~~~bash\ngit push origin HEAD\n~~~\n")
        self.assertEqual(len(enclosing_conditions(body)), 1)


def commit_push_blocks(skill: str) -> list[tuple[str, str]]:
    """`(where, block)` for every fenced block in `skill` that commits and then pushes."""
    blocks: dict[str, str] = {}
    for doc, line, _denied, _own, code in push_sites(skill):
        if "git commit" in code:
            blocks.setdefault(code, f"{_rel(doc)}:{line}")
    return [(where, code) for code, where in blocks.items()]


@unittest.skipIf(os.name == "nt" or not (BASH and shutil.which("git")),
                 "runs the shipped sh blocks under bash; native Windows runs their decisions "
                 "in its own shell, as the adapter notes say")
class CommitAndPushBlocksRun(unittest.TestCase):
    """Each commit-and-push block, run in a throwaway repository with a bare `origin`.

    `PushGuards` reads what a block tests. Only running it shows what a rejected commit or an
    unset `origin/HEAD` does to the push decision.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # No inherited config, hooks path, signing or repository location: the blocks answer
        # from the repository this test builds and nothing else.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.blocks = [b for skill in PUSHING_EXECUTORS for b in commit_push_blocks(skill)]
        self.assertTrue(self.blocks, "no commit-and-push block found — discovery broke")

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def published(self, name: str, default: str, branch: str) -> tuple[Path, Path]:
        """A repository on `branch`, pushed, with a change staged. `origin` is a bare
        repository whose HEAD names `default`, joined by `git remote add`, which creates no
        local `origin/HEAD`."""
        remote, work = self.tmp / f"{name}.git", self.tmp / name
        self.git(self.tmp, "init", "-q", "--bare", str(remote))
        self.git(remote, "symbolic-ref", "HEAD", f"refs/heads/{default}")
        self.git(self.tmp, "init", "-q", str(work))
        self.git(work, "checkout", "-q", "-b", default)
        (work / "a.txt").write_text("a\n", encoding="utf-8")
        self.git(work, "add", "a.txt")
        self.git(work, "commit", "-q", "-m", "a")
        self.git(work, "remote", "add", "origin", str(remote))
        self.git(work, "push", "-q", "origin", default)
        if branch != default:
            self.git(work, "checkout", "-q", "-b", branch)
            self.git(work, "push", "-q", "origin", branch)
        (work / "b.txt").write_text("b\n", encoding="utf-8")
        self.git(work, "add", "b.txt")
        return work, remote

    def run_block(self, block: str, cwd: Path) -> subprocess.CompletedProcess:
        return run_bash(block, cwd=cwd, env=self.env,
                              capture_output=True, text=True, timeout=120)

    def test_a_commit_a_hook_rejects_ends_the_block_in_failure(self):
        """With the tip already published nothing after the commit fails on its own, so the
        commit must: a block that exits 0 there sends the runner on to tick uncommitted work."""
        for i, (where, block) in enumerate(self.blocks):
            with self.subTest(block=where):
                work, _remote = self.published(f"hook{i}", "main", "feature")
                hook = work / ".git" / "hooks" / "pre-commit"
                hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                hook.chmod(0o755)
                proc = self.run_block(block, work)
                self.assertNotEqual(
                    proc.returncode, 0,
                    f"a rejected commit ended the block with exit 0:\n{proc.stdout}{proc.stderr}")

    def test_a_default_branch_with_no_local_origin_head_is_not_pushed(self):
        """A default named anything but `main` or `master` must be learned from the remote
        when `origin/HEAD` was never set, or every phase is pushed straight to it."""
        for i, (where, block) in enumerate(self.blocks):
            with self.subTest(block=where):
                work, remote = self.published(f"trunk{i}", "trunk", "trunk")
                before = self.git(remote, "rev-parse", "refs/heads/trunk")
                proc = self.run_block(block, work)
                self.assertEqual(
                    self.git(remote, "rev-parse", "refs/heads/trunk"), before,
                    f"the block pushed to the default branch:\n{proc.stdout}{proc.stderr}")

    def test_a_custom_default_is_still_guarded_when_the_remote_cannot_be_answered(self):
        """Why the LOCAL ref is asked first.

        `refs/remotes/origin/HEAD` needs no network and is right in any ordinary clone.
        Asking the remote before it made an unreachable network the reason the guard stopped
        working: the lookup came back empty, `main` and `master` both missed a trunk called
        `trunk`, and the phase published straight to it — a far commoner way to lose than the
        unfetched-clone case that ordering was meant to fix.
        """
        for i, (where, block) in enumerate(self.blocks):
            with self.subTest(block=where):
                work, _remote = self.published(f"offline{i}", "trunk", "trunk")
                # What an ordinary clone carries, and then a remote nothing can reach.
                self.git(work, "remote", "set-head", "origin", "trunk")
                self.git(work, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
                proc = self.run_block(block, work)
                self.assertIn(
                    "NOT pushing", proc.stdout,
                    f"the guard did not fire with the remote unreachable:\n"
                    f"{proc.stdout}{proc.stderr}")

    def test_a_default_that_moved_is_guarded_though_the_clone_still_names_the_old_one(self):
        """Why the REMOTE is asked and the local ref only answers when it cannot.

        `refs/remotes/origin/HEAD` is as true as the last fetch and no truer. Read first, a
        default that has moved leaves the clone naming the old one, the new trunk matches
        neither it nor the literals, and the phase publishes straight to the branch the guard
        exists to protect. Asking only the remote fails the other way, which
        `test_a_custom_default_is_still_guarded_when_the_remote_cannot_be_answered` pins.
        """
        for i, (where, block) in enumerate(self.blocks):
            with self.subTest(block=where):
                work, remote = self.published(f"moved{i}", "main", "trunk")
                # The remote's default moves to `trunk`; the clone goes on naming `main`.
                self.git(remote, "symbolic-ref", "HEAD", "refs/heads/trunk")
                self.git(work, "remote", "set-head", "origin", "main")
                self.assertEqual(
                    self.git(work, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"),
                    "origin/main", "the fixture did not leave a stale local default")
                before = self.git(remote, "rev-parse", "refs/heads/trunk")
                proc = self.run_block(block, work)
                self.assertEqual(
                    self.git(remote, "rev-parse", "refs/heads/trunk"), before,
                    f"pushed to the branch that is now the default:\n{proc.stdout}{proc.stderr}")
                self.assertIn("NOT pushing", proc.stdout,
                              f"the remote is unchanged, but not because the guard fired:\n"
                              f"{proc.stdout}{proc.stderr}")

    def test_a_default_branch_is_not_pushed_with_no_remote_tracking_refs_at_all(self):
        """The case the test above CANNOT reach, and the reason it could not.

        Pushing a branch creates its remote-tracking ref. So `origin/trunk` exists up there,
        and `git remote set-head origin -a` — which refuses with "Not a valid ref" unless
        that ref is already present — succeeded, set `origin/HEAD`, and the guard worked.
        Every derivation that reads `refs/remotes/origin/HEAD` passes that test while being
        blind here.

        With the tracking refs gone, such a derivation yields nothing, `${default:-main}`
        collapses to `main`, and neither literal fallback matches a trunk called `trunk`:
        the push lands on the default branch. Asking the remote for its advertised HEAD
        needs no tracking ref and answers `trunk` either way."""
        for i, (where, block) in enumerate(self.blocks):
            with self.subTest(block=where):
                work, remote = self.published(f"notrack{i}", "trunk", "trunk")
                for ref in self.git(work, "for-each-ref", "--format=%(refname)",
                                    "refs/remotes").splitlines():
                    self.git(work, "update-ref", "-d", ref.strip())
                self.assertEqual(
                    self.git(work, "for-each-ref", "--format=%(refname)", "refs/remotes"), "",
                    "the setup left a remote-tracking ref, so this reaches no further than the "
                    "test above")
                before = self.git(remote, "rev-parse", "refs/heads/trunk")
                proc = self.run_block(block, work)
                self.assertEqual(
                    self.git(remote, "rev-parse", "refs/heads/trunk"), before,
                    f"the block pushed to the default branch:\n{proc.stdout}{proc.stderr}")
                # Not just "did not push": a block that failed for some unrelated reason
                # would also leave the remote untouched and would pass on that alone.
                self.assertIn(
                    "NOT pushing", proc.stdout,
                    f"the remote is unchanged, but not because the guard fired:\n"
                    f"{proc.stdout}{proc.stderr}")


class TheRestageAfterAGateNamesTheFile(unittest.TestCase):
    """Finalization unstages the paths the run does not own. Re-staging a gated file with a
    sweep puts them back, and the commit that follows carries and pushes them."""

    def test_no_executor_sweeps_the_index_again_after_unstaging(self):
        for skill in PUSHING_EXECUTORS:
            with self.subTest(skill=skill):
                text = " ".join((SKILLS / skill / "SKILL.md").read_text(encoding="utf-8").split())
                self.assertFalse("`git add -A` again" in text,
                                 "re-stage the file the gate changed by name, not with a sweep")


def commit_push_step() -> str:
    """The `commit` skill's push step — its heading down to the next one."""
    text = (SKILLS / "commit" / "SKILL.md").read_text(encoding="utf-8")
    body = text.split("## Step 6 — Push to origin", 1)[1]
    return body.split("\n## ", 1)[0]


class CommitPublishesOnlyOnRequest(unittest.TestCase):
    """`/commit` answers to "stage and commit" as well as "commit and push".

    Those are different requests. A local commit is amended or reset; a pushed one is on a
    remote others have already fetched. Running both off either trigger takes that decision
    away from whoever made it.
    """

    def test_the_push_step_is_where_this_thinks_it_is(self):
        """Anchor against vacuity: the assertions below read a section that must exist."""
        step = commit_push_step()
        self.assertIn("git push origin", step)

    def test_the_push_is_gated_on_an_explicit_request(self):
        step = commit_push_step().lower()
        self.assertRegex(
            step, r"only (?:when|if)\b",
            "the push step must state the condition it runs under, not just how to push")
        named = [c for c in _CLAUSE.split(step) if "stage and commit" in c]
        self.assertTrue(
            named,
            "the trigger that must NOT publish has to be named in the step that publishes "
            "— it is listed in this skill's own description, so a reader arriving from it "
            "needs to find the answer here")
        # Naming it is not enough: "stage and commit always publishes too" names it and
        # says the opposite. The clause has to be the one that withholds the push.
        self.assertTrue(
            any(re.search(r"\b(?:stop|do not push|without pushing|no push|not publish)\b", c)
                for c in named),
            f"`stage and commit` is named but not withheld from publishing: {named}")


class WorktreeMutation(unittest.TestCase):
    """What a skill may write, and where."""

    def test_the_gitignore_scan_reads_a_non_empty_corpus(self):
        """Anchor against vacuity, as above."""
        docs = runtime_documents()
        self.assertGreater(len(docs), 20, "runtime-document discovery found almost nothing")
        self.assertTrue(
            any(".gitignore" in d.read_text(encoding="utf-8") for d in docs),
            "no document mentions `.gitignore` at all — the scan cannot be meaningful")

    def test_no_skill_creates_or_edits_a_gitignore(self):
        offences = gitignore_writes()
        self.assertEqual(
            offences, [],
            "a skill must not edit config the user never offered — least of all one whose "
            "whole promise is that it only reads. Write outside the worktree and print the "
            "absolute path instead:\n  " + "\n  ".join(offences))

    def test_the_gitignore_check_can_actually_fail(self):
        """A guard nobody has seen fail is a guard nobody has tested.

        Both orders, because the instruction found in the tree names the file first ("if
        `.gitignore` is absent, create it") while the natural rewording puts the verb first
        ("append the entry to `.gitignore`").
        """
        for text in ("if `.gitignore` is absent, create it",
                     "append `security-review/` to the `.gitignore`",
                     "write the entry into .gitignore",
                     "place `security-review/` in `.gitignore`",
                     "ensure `.gitignore` contains `security-review/`"):
            with self.subTest(text=text):
                self.assertTrue(_instructs_gitignore_write(text))

    def test_a_prohibition_is_not_an_instruction_to_write(self):
        """The sentence that FIXES this defect must not be read as committing it.

        A rule flagging "must not edit `.gitignore`" leaves no wording that satisfies it,
        so the assertion could never go green and would be deleted rather than met.
        """
        for text in ("A read-only audit must not create or edit a `.gitignore`.",
                     "Never append to the user's `.gitignore`.",
                     "Write outside the worktree rather than adding to `.gitignore`."):
            with self.subTest(text=text):
                self.assertFalse(_instructs_gitignore_write(text))

    def test_stating_where_output_belongs_is_not_editing_the_file(self):
        """Four skills say this about their own disposable artifacts. None names the file."""
        for text in ("ensure the repo gitignores the output directory",
                     "keep the file in a gitignored directory",
                     "Gitignore that directory so the log never lands in a commit",
                     "`.gitignore` already covers it, so additionally nothing is needed"):
            with self.subTest(text=text):
                self.assertFalse(_instructs_gitignore_write(text))

    def test_negation_binds_to_its_own_clause_not_the_whole_sentence(self):
        """Both halves of the scoping error, each way round."""
        self.assertTrue(_instructs_gitignore_write(
            "Do not touch unrelated files; create `.gitignore` if it is absent."))
        self.assertFalse(_instructs_gitignore_write(
            "No need to edit `.gitignore` — the run directory is outside the worktree."))

    def test_the_negation_rule_does_not_swallow_the_offence_in_the_tree(self):
        """"if it exists **without** the entry, append" — the real instruction contains a
        word a broader negator list would have treated as a prohibition."""
        self.assertTrue(_instructs_gitignore_write(
            "Ensure `security-review/` is gitignored — if `.gitignore` is absent, create "
            "it; if it exists without the entry, append `security-review/`."))

    def test_the_comment_stripper_keeps_the_shell_it_is_asked_about(self):
        """`sed 's#^origin/##'` is the derivation itself — cutting at its `#` would make
        the default-branch guard invisible and the assertion above unfalsifiable."""
        code = shell_code(
            '  # Never push straight to the default branch\n'
            '  d="$(git symbolic-ref --short refs/remotes/origin/HEAD | sed \'s#^origin/##\')"\n'
        )
        self.assertNotIn("Never push", code)
        self.assertIn("symbolic-ref", code)
        self.assertIn("s#^origin/##", code)



# The three documents that carry the plan content model. `plan-duel`'s copy exists because
# the duel generates plans without `plan-init`; the parity ledger in CONTRIBUTING.md records
# that the duplication is deliberate, which is exactly why a section added to one of them has
# to be added to all three.
PLAN_CONTENT_MODEL = (
    # Each site with the marker IT writes, not a bare word: "assumptions" already appears
    # in plan-init's autonomous-mode sentence, so a generic substring would stay green with
    # every new section deleted — the parity check would then be checking nothing.
    (SKILLS / "plan-init" / "SKILL.md", "`## Assumptions`"),
    (SKILLS / "plan-init" / "references" / "plan-template.md", "## Assumptions"),
    (SKILLS / "plan-duel" / "init.md", "**Assumptions**"),
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class WriteLocationIsDerived(unittest.TestCase):
    """A breakdown skill must say where it writes before it starts writing.

    Both `plan-phase` skills accept a plan anywhere — discovery takes a path argument — and
    then spend the rest of the document saying `plans/<slug>/`. Read literally that sends a
    plan found in `docs/` to a directory nothing will look in, so each has to state, once and
    up front, that those paths name the plan's own directory.
    """

    CASES = (("plan-phase", "execution.md"), ("plan-phase-v1", "phases.md"))

    # The directory has to be tied to the PLAN, not merely to "the same directory" as
    # something. The two skills word it differently ("as the plan file you just read", "as
    # the source plan file"), so the tie is what is matched, not either phrasing.
    STATEMENT = re.compile(r"same directory as the (?:\w+ )*plan\b")

    def test_each_breakdown_skill_states_its_output_directory(self):
        for skill, tracker in self.CASES:
            with self.subTest(skill=skill):
                body = _text(SKILLS / skill / "SKILL.md").lower()
                self.assertRegex(body, self.STATEMENT,
                                 f"{skill} never ties its output directory to the plan")
                self.assertIn(tracker.lower(), body)

    def test_the_statement_comes_before_the_first_write_instruction(self):
        """Order, not presence. A rule stated after the write has already happened is prose."""
        for skill, _ in self.CASES:
            with self.subTest(skill=skill):
                body = _text(SKILLS / skill / "SKILL.md").lower()
                said = self.STATEMENT.search(body).start()
                wrote = body.index("create `plans/<slug>/phase-")
                self.assertLess(
                    said, wrote,
                    f"{skill} writes phase documents before it says where they go")


class OverwriteGuardsPrecedeTheirWrites(unittest.TestCase):
    """The guard has to run before the first write, not before the write it names.

    `plan-phase-v1`'s existing `phases.md` check sat in Step 6 — after Step 5 had already
    recreated every phase document. Protecting the tracker while overwriting the documents
    whose progress it points at is not protection, and the half-executed plan is the common
    case.
    """

    def test_the_v1_tracker_guard_runs_before_any_phase_document_is_written(self):
        body = _text(SKILLS / "plan-phase-v1" / "SKILL.md").lower()
        guard = body.index("`phases.md` already exists")
        first_write = body.index("create `plans/<slug>/phase-")
        self.assertLess(
            guard, first_write,
            "the phases.md overwrite guard sits after the first phase-document write, so "
            "it stops only once the damage is done")

    def test_the_v2_tracker_guard_does_too(self):
        body = _text(SKILLS / "plan-phase" / "SKILL.md").lower()
        guard = body.index("before writing anything into `plans/<slug>/`")
        first_write = body.index("create `plans/<slug>/phase-")
        self.assertLess(guard, first_write)


class MirroredPlanSections(unittest.TestCase):
    """A section added to the plan content model lands in all three copies or none.

    They are duplicated on purpose — a skill must be self-contained once installed — so
    nothing but a check like this notices when one copy moves and the others do not.
    """

    def test_every_copy_of_the_content_model_carries_assumptions(self):
        missing = [_rel(path) for path, marker in PLAN_CONTENT_MODEL
                   if marker not in _text(path)]
        self.assertEqual(
            missing, [],
            "`plan-init`'s autonomous mode says to note each assumption explicitly in the "
            "plan, which needs somewhere to go in every document that defines what a plan "
            "contains:\n  " + "\n  ".join(missing))

    def test_the_mirror_set_is_not_silently_empty(self):
        """Anti-vacuity: three real files, or the check above proves nothing."""
        for path, _marker in PLAN_CONTENT_MODEL:
            self.assertTrue(path.is_file(), f"{_rel(path)} is gone — update the mirror set")
        self.assertEqual(len(PLAN_CONTENT_MODEL), 3)


class UntrackedNoiseIsSubtractedButOnlyWhenGitCanSaySo(unittest.TestCase):
    """The sweep skips files git does not track, and fails CLOSED when git cannot answer.

    An untracked file is scratch work, build residue or a stale checkout, so scanning it for
    things that must not be shared checks a route that does not exist — 645 violations on an
    ordinary working copy, every one untracked, which is enough noise to make the guard
    useless as a signal. The danger runs the other way, which is why three checks fail
    closed: a wrong "tracked" answer subtracts everything and a sweep that scanned nothing
    reports clean. Git is optional because the validator SHIPS, and is used only to SUBTRACT.
    """

    CANARY = "/home/someone/secret"  # hygiene-exempt: the canary itself

    def setUp(self):
        self.vcr = vcr

    def _tree_with_a_planted_path(self, root: Path):
        (root / "notes.md").write_text(
            f"a private path {self.CANARY} that must be caught\n", encoding="utf-8")

    def test_a_tracked_file_under_a_tooling_directory_is_still_scanned(self):
        """Both halves of the subtraction, in a repository this test builds.

        Reading this repository and naming `.claude/settings.json` would assert a fact about
        one checkout rather than about the code: a repository without that directory fails on
        its first run for a reason unrelated to the behavior under test. A fixture states the
        behavior where the behavior lives.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True,
                           capture_output=True)
            worktree = root / ".claude" / "worktrees" / "agent-1"
            worktree.mkdir(parents=True)
            (root / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
            # Untracked, and carrying something the sweep would otherwise report: this is
            # the 645-violation noise the subtraction exists to remove.
            self._tree_with_a_planted_path(worktree)
            subprocess.run(
                ["git", "-C", str(root), "add", "-f", ".claude/settings.json"],
                check=True, capture_output=True)

            scanned = {rel for _p, rel, _pat, _l in self.vcr.iter_hygiene_targets(root)}

        self.assertIn(
            ".claude/settings.json", scanned,
            "a TRACKED file under .claude was dropped — the rule is 'untracked', not "
            "'anything under a tooling directory', and a whole-directory skip would have "
            "lost this one")
        self.assertEqual(
            sorted(r for r in scanned if r.startswith(".claude/worktrees/")), [],
            "untracked worktree files are still being scanned")

    def test_a_non_ascii_filename_does_not_depend_on_the_locale(self):
        """git writes raw path bytes, and the locale must not get a say in reading them.

        Text mode decodes with the LOCALE's encoding: on a repository holding `café.md`,
        `LC_ALL=C` raises UnicodeDecodeError — a ValueError the function's own
        `except (OSError, SubprocessError)` misses. Windows fails worse by not failing, since
        cp1252 decodes those bytes to *something* that matches no walked path, so the file is
        subtracted and a sweep that skipped it reports clean. Run in a subprocess under that
        locale, because the defect is in how the parent decodes; the assertion is the round
        trip rather than the spelling.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True,
                           capture_output=True)
            # Named in BYTES, and turned into a path with `os.fsdecode`, because the fixture
            # must not depend on the locale either. Writing `"café.md"` directly fails to
            # *create* the file under `LC_ALL=C`, where the filesystem encoding is ASCII —
            # which is the CI job this test exists for, so it broke exactly where it was
            # needed. `os.fsdecode` gives the string this process would use for those bytes,
            # and both spellings write the same bytes to disk.
            raw_name = "café.md".encode("utf-8")
            (root / os.fsdecode(raw_name)).write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                           capture_output=True)

            probe = (
                "import importlib.util, os, sys\n"
                "spec = importlib.util.spec_from_file_location('vcr', sys.argv[1])\n"
                "vcr = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(vcr)\n"
                "from pathlib import Path\n"
                "tracked = vcr.tracked_paths(Path(sys.argv[2]))\n"
                "sys.stdout.buffer.write(b'\\0'.join(os.fsencode(p) for p in tracked))\n"
            )
            env = os.environ.copy()
            env.update(PYTHONUTF8="0", LC_ALL="C", LANG="C")
            proc = subprocess.run(
                [sys.executable, "-c", probe,
                 str(REPO_ROOT / "scripts" / "validate_cross_runtime.py"), str(root)],
                capture_output=True, env=env)

        self.assertEqual(
            proc.returncode, 0,
            "reading git's file list died on a non-ASCII filename:\n"
            + proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(
            proc.stdout.split(b"\0"), [raw_name],
            "the decoded path did not round-trip to the bytes git emitted, so it names a "
            "different file than the one on disk")

    def test_without_git_nothing_is_subtracted(self):
        """The shipped case: a user's installed pack is not a repository."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._tree_with_a_planted_path(root)
            self.assertIsNone(
                self.vcr.tracked_paths(root),
                "a directory that is not a repository must yield no tracked set")
            found = self.vcr.sweep_content_hygiene(root)
        self.assertTrue(
            found,
            "with no git answer the sweep must scan everything, exactly as it did before — "
            "a public user gets no subtraction and no loss of coverage")

    def test_a_directory_inside_an_unrelated_repository_subtracts_nothing(self):
        """The fail-closed case holds because `ls-files` is directory-scoped.

        A temporary directory inside an unrelated repository would otherwise yield a tracked
        set describing a different tree, subtract every file, and report clean. Instead
        `ls-files` prints `nested/inner.txt` at a repository root and `inner.txt` at
        `nested/`, so a nested directory with nothing tracked gets an EMPTY answer, which the
        empty-set guard turns into no answer at all. That is a property of git rather than of
        this code, so it needs an assertion of its own.
        """
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            subprocess.run(["git", "init", "-q", str(outer)], check=True,
                           capture_output=True)
            (outer / "unrelated.txt").write_text("nothing to see\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(outer), "add", "unrelated.txt"],
                           check=True, capture_output=True)
            inner = outer / "nested"
            inner.mkdir()
            self._tree_with_a_planted_path(inner)

            self.assertIsNone(
                self.vcr.tracked_paths(inner),
                "git named files from a directory that tracks none of its own — the answer "
                "describes another tree, and acting on it would subtract everything")
            found = self.vcr.sweep_content_hygiene(inner)
        self.assertTrue(
            found,
            "the planted path was not caught, so the sweep subtracted files on an answer "
            "that did not describe this tree")

    def test_ls_files_is_relative_to_where_it_is_asked(self):
        """The git property the subtraction rests on, asserted directly.

        Separate from the test above because that one would still pass if git changed and
        the empty-set guard happened to catch it. This one fails on the change itself.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True,
                           capture_output=True)
            (root / "nested").mkdir()
            (root / "top.txt").write_text("a\n", encoding="utf-8")
            (root / "nested" / "inner.txt").write_text("b\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                           capture_output=True)
            at_root = self.vcr.tracked_paths(root)
            at_nested = self.vcr.tracked_paths(root / "nested")
        self.assertEqual(at_root, frozenset({"top.txt", "nested/inner.txt"}))
        self.assertEqual(
            at_nested, frozenset({"inner.txt"}),
            "ls-files must be relative to the directory it is asked in; if it ever returns "
            "repository-relative paths here, every walk-relative lookup misses and the "
            "sweep subtracts everything it should have scanned")

    def test_an_empty_repository_is_treated_as_no_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True,
                           capture_output=True)
            self._tree_with_a_planted_path(root)
            self.assertIsNone(
                self.vcr.tracked_paths(root),
                "a repository that tracks nothing tells us nothing, so it must not be "
                "read as 'nothing is tracked'")
            self.assertTrue(self.vcr.sweep_content_hygiene(root))


class PrivacyGuardCoversTheWholeRepository(unittest.TestCase):
    """The private-identifier sweep must not quietly shrink back to one directory.

    It scanned `skills/` and `README.md` only, while the docs claimed it replaced a
    `grep -r` over everything. A fake home path planted in six files passed clean — the
    installers among them, which were 47 KB of path-handling shell and the likeliest place
    for a real one to be pasted. These tests are that review's canary, kept.
    """

    CANARY = "/home/someone/secret"  # hygiene-exempt: the canary itself

    # Files a sweep scoped to `skills/` does not reach. Each is a real shipped path, not a
    # fixture.
    BLIND_SPOTS = (
        # `install.sh` and `install.ps1` were the original two entries and the reason this
        # canary exists — 47 KB of path-handling shell where a real home path is likeliest
        # to be pasted. They are gone; `install.py` takes their place here rather than the
        # list simply getting shorter.
        "install.py",
        "CONTRIBUTING.md",
        "PORTABILITY.md",
        ".github/workflows/validate.yml",
        "tests/README.md",
        # The file that INVITES a user to type their own private names. A user who edits
        # it in place rather than copying it commits them, and `.gitignore` protects the
        # copy rather than the template.
        "scripts/private-identifiers.txt.example",
    )

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "vcr", REPO_ROOT / "scripts" / "validate_cross_runtime.py")
        self.vcr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.vcr)

    def test_a_planted_private_path_is_caught_in_every_previously_blind_file(self):
        import shutil, tempfile
        for rel in self.BLIND_SPOTS:
            with self.subTest(path=rel):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / "repo"
                    shutil.copytree(REPO_ROOT, root, symlinks=True, ignore=shutil.ignore_patterns(
                        ".git", "__pycache__", "plans", "*.pyc"))
                    target = root / rel
                    self.assertTrue(target.is_file(), f"{rel} is gone — update this list")
                    target.write_text(target.read_text(encoding="utf-8")
                                      + f"\n# LEAKCANARY {self.CANARY}\n", encoding="utf-8")
                    found = self.vcr.sweep_content_hygiene(root)
                    self.assertTrue(
                        any(rel.split("/")[-1] in f for f in found),
                        f"a private path planted in {rel} was NOT caught — the sweep has "
                        f"narrowed again. Findings: {found}")

    def test_a_home_path_is_caught_on_every_platform_not_just_linux(self):
        """The macOS and Windows forms went uncaught until a review pasted one in.

        macOS is in the CI matrix, so it is a real place for a real path to come from.
        """
        for path in ("/home/bob/x", "/Users/bob/x", r"C:\Users\bob\x", r"D:\Users\bob\x"):  # hygiene-exempt: test data
            with self.subTest(path=path):
                self.assertTrue(
                    any(p.search(path) for p in self.vcr.PRIVATE_PATH_PATTERNS),
                    f"{path} is a home directory and was not recognized as one")
        for path in ("/usr/share/x", "/Userspace/lib", "/homebrew/bin"):
            with self.subTest(path=path):
                self.assertFalse(
                    any(p.search(path) for p in self.vcr.PRIVATE_PATH_PATTERNS),
                    f"{path} is not a home directory and was flagged as one")

    @unittest.skipUnless(os.name == "posix", "symlink creation and enumeration differ on "
                                             "Windows; the artifact this guards against is "
                                             "a POSIX-authored committed symlink")
    def test_a_symlink_target_is_read_as_content(self):
        """git stores the target string, so a dangling link still publishes its path.

        POSIX-only, and skipped rather than weakened: on Windows `rglob` does not reliably
        enumerate a dangling link, so the test failed there while the behavior it checks is
        about what a POSIX author committed.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills").mkdir()
            os.symlink("/home/someone/secret", root / "skills" / "dangling")  # hygiene-exempt: test data
            found = self.vcr.sweep_content_hygiene(root)
            self.assertTrue(found, "a symlink pointing at a home path was not caught — "
                                   "`is_file()` is False for a dangling link, so reading "
                                   "the target is the only way to see it")

    @unittest.skipUnless(os.name == "posix", "see the note on the test above")
    def test_a_symlink_is_judged_by_the_same_rule_wherever_it_sits(self):
        """The link branch kept the rule the file branch had already been fixed away from.

        Which pattern set applies is decided by the file's NAME — the identity documents that
        legitimately carry the owner's handle get the relaxed set, and nothing else does. The
        symlink branch instead asked "is it under `skills/`?", so the identical
        `../<sibling-repo>/x` target was caught under `skills/` and missed at the repository
        root, which is where such a link would actually sit.
        """
        import tempfile
        target = "../dotfiles/private/x"  # hygiene-exempt: test data
        for where in ("skills/demo/link", "notes", "scripts/link"):
            with self.subTest(location=where):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "skills").mkdir()
                    link = root / where
                    link.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(target, link)
                    found = self.vcr.sweep_content_hygiene(root)
                    self.assertTrue(
                        found,
                        f"a symlink at {where} pointing into a private sibling repo was "
                        f"not caught. Location must not change the rule — only the "
                        f"identity documents in OWNER_NAMING_FILES relax it.")

    def test_the_repository_is_clean_right_now(self):
        """Anti-vacuity: the canary test above proves nothing if the tree already fails."""
        self.assertEqual(self.vcr.sweep_content_hygiene(REPO_ROOT), [])

    def test_every_exemption_is_greppable_and_earns_its_place(self):
        """Exemptions are per-LINE, so grepping the marker enumerates every one of them.

        Exempting a whole file is how the coverage shrank in the first place; a line marker
        keeps the escape hatch visible and countable. Counted by EFFECT, not by presence: a
        line carrying the marker but holding no pattern it could suppress is dead weight, and
        dead weight is how a live one hides. NAMING the marker is not APPLYING it — its own
        definition and the paragraph documenting it are quoted or in backticks, while an
        applied one is bare, in a trailing comment.
        """
        marker = self.vcr.HYGIENE_EXEMPT_MARKER
        # EVERY file, not the old `.py`/`.md` glob — a marker in a shell script or a workflow
        # escaped that one entirely.
        #
        # The file set and the pattern choice come from `iter_hygiene_targets`, the generator
        # the sweep itself uses, rather than being rebuilt from the constants here: a rebuilt
        # copy goes stale silently, and an audit of "every line the guard covers" that
        # computes coverage differently from the guard is not an audit of the guard.
        effective, dead = [], []
        for p, rel, base, is_symlink in self.vcr.iter_hygiene_targets(REPO_ROOT):
            if is_symlink:
                continue  # a link's target is one string, and it carries no comment
            patterns = list(base) + list(self.vcr.HARDCODED_ATTRIBUTION_PATTERNS)
            for i, line in enumerate(
                    p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if marker not in line:
                    continue
                # Judge the line as it would read WITHOUT the marker, so the marker's own
                # words can never be mistaken for the thing it is suppressing. `:?` because
                # the canonical spelling now CARRIES the colon, so prose naming it writes
                # `hygiene-exempt:` inside the quotes — without this every sentence
                # documenting the rule counted as an application of it.
                if re.search(r"[`\"']" + re.escape(marker) + r":?[`\"']", line):
                    continue  # named, not applied — see the docstring
                bare = line.replace(marker, "")
                bucket = effective if any(pat.search(bare) for pat in patterns) else dead
                bucket.append(f"{rel}:{i}")
        # A ceiling, not a target. Most sit in the validator itself, which necessarily
        # contains every pattern it searches for, and in the fixtures that prove the checks
        # fire. The number exists so growth is noticed and read — raise it deliberately,
        # with the reason, as you would a word budget.
        self.assertLessEqual(
            len(effective), 30,
            "the exemption list is growing — each one turns the guard off for a line, so "
            f"they need reading, not accumulating ({len(effective)} now):\n  "
            + "\n  ".join(effective))
        self.assertEqual(
            dead, [],
            "a line applies the exemption marker and has nothing to exempt. Either the "
            "path it guarded was removed and the marker outlived it, or it was added by "
            f"mistake. Delete it: {dead}")
        self.assertEqual(len(self.vcr.HYGIENE_ALLOWLIST), 3,
                         "a whole-file exemption was added; prefer a line marker")

    # Built from ONE exempted constant rather than repeated inline, which is the idiom the
    # canary tests above already use. Writing the literal into each probe would put four
    # more private paths in this file's source, each needing its own exemption — growing
    # the very list the test below caps.
    PROBE = "/home/someone/secret"  # hygiene-exempt: probe data for the tests below

    def _probe_finding_count(self, trailing_comment: str) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.py"
            probe.write_text(f'p = "{self.PROBE}"  {trailing_comment}\n', encoding="utf-8")
            return len(self.vcr.check_private_paths(probe))

    def test_ordinary_english_cannot_switch_the_guard_off(self):
        """The marker carries a colon and a reason — not a substring anyone can hit.

        Matched as a bare substring, any line merely CONTAINING those characters silenced the
        private-path check for that whole line: an `-ible` suffix, a sentence discussing the
        marker, a URL with it in the slug. The escape hatch has to be opened deliberately and
        say why, or it is a hole.
        """
        self.assertEqual(
            self._probe_finding_count("# " + "hygiene-exempt" + "ible, probably"), 1,
            "a private path went unreported because the line happened to contain the "
            "marker's characters inside a longer ordinary word")

    def test_a_reasonless_marker_does_not_exempt(self):
        """A colon with nothing after it states no reason, so it earns nothing."""
        self.assertEqual(
            self._probe_finding_count("# " + "hygiene-exempt" + ":"), 1,
            "an exemption with no reason beside it still suppressed the finding")

    def test_the_marker_still_works_when_spelled_properly(self):
        """The positive control. Without it the two tests above pass on a broken rule."""
        self.assertEqual(
            self._probe_finding_count("# " + "hygiene-exempt" + ": fixture data"), 0)

    def test_no_shipped_skill_prose_carries_an_exemption(self):
        """Regression guard — passes today, and is the one place it must never stop passing.

        `skills/**` is the product; everything else carrying a marker is machinery. A skill
        has no legitimate reason to hold a private-looking string, so a marker there is
        either a real leak wearing a permission slip or dead weight, and the guard cannot
        tell which.
        """
        marker = self.vcr.HYGIENE_EXEMPT_MARKER
        offenders = []
        for p, rel, base, is_symlink in self.vcr.iter_hygiene_targets(REPO_ROOT):
            if is_symlink or not rel.startswith("skills/"):
                continue
            for i, line in enumerate(
                    p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if marker in line:
                    offenders.append(f"{rel}:{i}: {line.strip()[:70]}")
        self.assertEqual(
            offenders, [],
            "a shipped skill turns the private-path guard off for a line. Skill prose must "
            f"be generic outright, so there is nothing here to exempt:\n  "
            + "\n  ".join(offenders))


class ResumeReadsOnlyStateSomeStepWrites(unittest.TestCase):
    """A resume branch keyed on a box nobody ticks re-runs work that is already finished.

    `plan-run-v1`'s 3a decides where an interrupted run re-enters by reading the phase
    document's Task and Test boxes. 3d ticked task boxes only, so the middle branch (work
    done, gate not yet run) was unreachable and a resumed run redid the phase from the top.

    This pins that defect; it does not prove the general property, since a document could
    satisfy it by wording alone. The value is that deleting the instruction again fails.
    """

    # skill -> the box-bearing sections its resume branch keys on
    RESUME_SECTIONS = {
        "plan-run-v1": ("Task", "Test"),
        "plan-run": ("Work", "Tests"),
    }

    TICK_VERB = r"(tick|check(?:ing|s)?(?: it)? off|change `- \[ \]`)"

    def test_each_section_a_resume_branch_reads_is_one_some_step_ticks(self):
        for skill, sections in self.RESUME_SECTIONS.items():
            text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
            for section in sections:
                with self.subTest(skill=skill, section=section):
                    # An instruction naming the section within a sentence of a tick verb.
                    near = re.search(
                        rf"{self.TICK_VERB}[^.]{{0,160}}\b{section}s?\b"
                        rf"|\b{section}s?\b[^.]{{0,160}}{self.TICK_VERB}",
                        text, re.IGNORECASE | re.DOTALL)
                    self.assertIsNotNone(
                        near,
                        f"{skill} resumes by reading the {section} boxes, but no step "
                        f"instructs ticking them. A resumed run then reads finished work "
                        f"as unstarted and repeats it.")

    def test_the_resume_branches_really_do_name_those_sections(self):
        """Anti-vacuity: if a resume step stops naming a section, this set is stale."""
        for skill, sections in self.RESUME_SECTIONS.items():
            text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
            for section in sections:
                with self.subTest(skill=skill, section=section):
                    self.assertRegex(
                        text, rf"\b{section}s?\b",
                        f"{skill} no longer mentions {section} — update RESUME_SECTIONS")


class EverySuiteRunsWholeWhenExecutedDirectly(unittest.TestCase):
    """`unittest.main()` must be the last statement in every suite that has one.

    A class defined *below* the guard is invisible to `python3 tests/<suite>.py` and visible
    to discovery, so CI stays green while the direct run someone uses to debug quietly covers
    less. Checked across the whole suite directory rather than for this file alone — the
    defect is a property of how a suite is assembled, and it recurs by appending.
    """

    def test_no_code_follows_the_main_guard_in_any_suite(self):
        offenders = {}
        for suite in sorted((REPO_ROOT / "tests").glob("test_*.py")):
            lines = suite.read_text(encoding="utf-8").splitlines()
            guards = [i for i, line in enumerate(lines) if line.startswith("if __name__")]
            if not guards:
                continue  # discovery-only suite; nothing to get wrong
            self.assertEqual(len(guards), 1,
                             f"{suite.name} has {len(guards)} __main__ guards")
            after = [line for line in lines[guards[0] + 1:]
                     if line.strip() and line.strip() != "unittest.main()"]
            if after:
                offenders[suite.name] = after[:2]
        self.assertEqual(
            offenders, {},
            "code follows the __main__ guard, so `python3 <suite>` silently runs less "
            f"than discovery does: {offenders}")




class TheTestsReadmeNamesEverySuite(unittest.TestCase):
    """Documentation that ships has to be true, and this bit had drifted badly.

    `tests/README.md` is the map of this directory; naming a fraction of what is here meant
    the only reliable way to learn what the suites cover was to run them. No count in this
    docstring and none in the README's prose either: a fixed number goes stale the first time
    a suite is added, and a count above a table is a second answer to a question the table
    already answers.
    """

    def test_every_python_suite_is_named_in_the_readme(self):
        readme = (REPO_ROOT / "tests" / "README.md").read_text(encoding="utf-8")
        missing = [p.name for p in sorted((REPO_ROOT / "tests").glob("test_*.py"))
                   if p.name not in readme]
        self.assertEqual(missing, [], f"suites exist but are not in tests/README.md: {missing}")

    def test_every_validator_fixture_is_named_in_the_readme(self):
        readme = (REPO_ROOT / "tests" / "README.md").read_text(encoding="utf-8")
        missing = [p.name for p in sorted((REPO_ROOT / "tests").glob("test_validate_*.md"))
                   if p.name not in readme]
        self.assertEqual(missing, [], f"fixtures not in tests/README.md: {missing}")

    def test_the_readme_names_nothing_that_is_gone(self):
        """The other direction: a suite deleted must not linger in the map."""
        import re
        readme = (REPO_ROOT / "tests" / "README.md").read_text(encoding="utf-8")
        on_disk = {p.name for p in (REPO_ROOT / "tests").glob("test_*")}
        named = set(re.findall(r"`(test_[\w.-]+\.(?:py|md|sh))`", readme))
        self.assertEqual(sorted(named - on_disk), [],
                         "tests/README.md names files that no longer exist")


class DelegationAdaptersBothCarryTheWorkerContract(unittest.TestCase):
    """A dispatched worker must be handed the contract, not only its three input paths.

    `plan-run` delegates a phase to a *fresh* worker, and the durable state it needs is on
    disk — but the rules it must obey are not: leave every change uncommitted, never write
    `execution.md`, never run the independent review, never commit. Those live in
    `references/phase-worker-contract.md`, and a general-purpose sub-agent does not load the
    skill on its own, so an adapter naming only the paths dispatches a worker that has never
    read them. Asserted per adapter because they diverged and nothing compared them.
    """

    CONTRACT = "references/phase-worker-contract.md"

    def _adapter_block(self) -> str:
        text = (SKILLS / "plan-run" / "SKILL.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        start = next(i for i, l in enumerate(lines) if "**Claude adapter:**" in l)
        end = next(i for i in range(start + 1, len(lines)) if not lines[i].startswith(">"))
        return "\n".join(lines[start:end])

    def test_the_contract_file_the_adapters_point_at_exists(self):
        self.assertTrue(
            (SKILLS / "plan-run" / self.CONTRACT).is_file(),
            f"plan-run/{self.CONTRACT} is missing, so both adapters point at nothing")

    def test_both_adapters_name_the_worker_contract(self):
        block = self._adapter_block()
        # Split on the adapter markers themselves, never on a bare `**`: the emphasis a
        # correct adapter line puts around the contract clause would truncate the segment
        # before the path and fail a passing file.
        markers = [(m.start(), m.group(1))
                   for m in re.finditer(r"\*\*(\w+) adapter:\*\*", block)]
        self.assertEqual(
            [name for _, name in markers], ["Claude", "Codex"],
            "expected exactly a Claude and a Codex adapter line, in that order")
        bounds = [pos for pos, _ in markers] + [len(block)]
        for i, (_, adapter) in enumerate(markers):
            segment = block[bounds[i]:bounds[i + 1]]
            self.assertIn(
                self.CONTRACT, segment,
                f"the {adapter} adapter dispatches a worker without naming "
                f"{self.CONTRACT}, so the worker never learns to leave changes "
                f"uncommitted, never write execution.md and never commit")

class EveryCodexExecReviewerIsPinnedReadOnly(unittest.TestCase):
    """`diff-review` spawns a reviewer, and a reviewer that can write is not a review.

    Two flags are needed and neither implies the other. `-s read-only` bounds the model's
    *shell*, not the runtime's built-in patch tool, which is gated by the approval policy
    instead: a `-s read-only` spawn with approvals at their default still wrote a file, and
    wrote nothing once `approval_policy=never` was pinned.

    Rung 1 spells both. Rung 2 named `codex exec` with no arguments, which the validator's
    lint cannot see — it reads flags on a command it can find. So the rung reached on exactly
    the hosts with least set up was the unpinned one.
    """

    def test_no_codex_exec_reviewer_is_spelled_without_both_flags(self):
        text = (SKILLS / "diff-review" / "SKILL.md").read_text(encoding="utf-8")
        offenders = []
        for i, line in enumerate(text.splitlines(), 1):
            if "codex exec" not in line:
                continue
            missing = [f for f in ('-s read-only', 'approval_policy="never"')
                       if f not in line]
            if missing:
                offenders.append(f"  :{i}: missing {missing}: {line.strip()[:90]}")
        self.assertEqual(
            offenders, [],
            "a `codex exec` reviewer is spawned without a hard read-only bound; "
            "both flags are required and neither implies the other:\n"
            + "\n".join(offenders))


class TheSkillsDirectoryArgumentNamesTheTreeUnderTest(unittest.TestCase):
    """The documented `[skills/]` argument worked against exactly one tree — this pack's.

    `repo_root` was `Path(__file__).parent.parent` unconditionally, so the skill rules read
    the caller's directory while `check_portability_md`, the hygiene sweep, the README
    inventory and the budget ratchet kept reading the pack. Asserted on which tree the
    findings NAME, because both the old and the new code fail on a foreign tree — what
    changed is whether the failure is about the right one.
    """

    def test_findings_name_the_given_tree_not_the_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "otherrepo"
            (other / "skills" / "demo").mkdir(parents=True)
            (other / "skills" / "demo" / "SKILL.md").write_text(
                "---\nname: demo\ndescription: A demo skill for this probe.\n---\n\n"
                "# Demo\n\nDoes nothing.\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "validate_cross_runtime.py"),
                 str(other / "skills")],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            # Matched on the probe directory's NAME plus the file, not on its absolute path.
            # On Windows `tempfile` hands back a path whose user-directory component is the
            # 8.3 short form while the validator resolves it to the long form: the same
            # directory, two spellings, and a string comparison between them fails on the one
            # platform this rule most needs to hold. Requiring `README.md` beside it keeps the
            # assertion specific.
            named_probe_tree = [
                line for line in result.stdout.splitlines()
                if other.name in line and "README.md" in line
            ]
            self.assertTrue(
                named_probe_tree,
                "the run reported nothing about the tree it was pointed at:\n" + result.stdout)
            self.assertNotIn(
                str(REPO_ROOT / "README.md"), result.stdout,
                "the run read THIS pack's README while validating another tree's skills:\n"
                + result.stdout)


class TheProgressPostureIsAWholeWord(unittest.TestCase):
    """A declaration contract that accepts a word merely *beginning* like a posture.

    Trailing prose is legitimate — every real declaration carries it — and matching that
    allowance with `startswith` swallowed the word boundary too. Both directions are asserted
    because fixing this the obvious way breaks the other: `_Progress:` is written italic, `_`
    is a word character, so a bare `bounded_` has no boundary after the posture.
    """

    def _verdict(self, value: str) -> bool:
        """True when the declaration is accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            skill.write_text(f"# S\n\nUses a sub-agent.\n\n_Progress: {value}_\n",
                             encoding="utf-8")
            return not vcr.check_progress_declaration(skill)

    def test_a_word_that_merely_starts_like_a_posture_is_rejected(self):
        for value in ("boundedish nonsense", "observableness"):
            with self.subTest(value=value):
                self.assertFalse(self._verdict(value),
                                 f"{value!r} declared a posture it does not name")

    def test_the_valid_forms_are_still_accepted(self):
        for value in ("bounded", "observable", "observable via a run-level log",
                      "bounded — each sub-agent returns its result"):
            with self.subTest(value=value):
                self.assertTrue(self._verdict(value),
                                f"{value!r} is a valid declaration and was rejected")


class CrossReferencesPointTheWayTheySay(unittest.TestCase):
    """"See the note below" that is above sends a reader in the wrong direction.

    Cheap to get wrong and invisible to every machine check in the pack: the words are
    ordinary English and the target genuinely exists, so nothing but reading catches it.
    """

    def test_the_diff_review_schema_note_is_where_its_pointer_says(self):
        lines = (SKILLS / "diff-review" / "SKILL.md").read_text(encoding="utf-8").splitlines()
        note = next(i for i, l in enumerate(lines) if "Adapter note — why only one rung-1" in l)
        pointers = [(i, l) for i, l in enumerate(lines) if "asymmetry note" in l]
        self.assertTrue(pointers, "the pointer at the schema-flag omission is gone")
        for i, line in pointers:
            direction = "below" if "below" in line else "above" if "above" in line else None
            self.assertIsNotNone(direction, f"line {i + 1} names no direction: {line.strip()}")
            actual = "below" if note > i else "above"
            self.assertEqual(
                direction, actual,
                f"line {i + 1} says the asymmetry note is {direction}, but it is "
                f"{actual} (line {note + 1})")


class EveryBundledReferenceResolves(unittest.TestCase):
    """`references/...` names a file that travels inside the skill, so it must be there.

    The complement of `check_self_contained_skill_refs`, which asks whether a reference
    ESCAPES the skill and never whether it LANDS on anything. A skill could name
    `references/anchored-assumptions.md`, ship no such file, and pass every other rule, so the
    agent following it hits a dead end mid-task.

    **Covered here rather than in the validator's fixture corpus**, because the rule takes a
    skill ROOT as well as a file, and a corpus case would need a skill tree beside the fixture.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "references").mkdir()
        (self.root / "references" / "present.md").write_text("# Here\n", encoding="utf-8")

    def _skill(self, body: str) -> Path:
        f = self.root / "SKILL.md"
        f.write_text(body, encoding="utf-8")
        return f

    def test_a_reference_to_a_missing_file_is_reported(self):
        f = self._skill("See `references/absent.md` for the details.\n")
        found = vcr.check_bundled_refs_resolve(f, self.root)
        self.assertEqual(len(found), 1, found)
        self.assertIn("references/absent.md", found[0])
        self.assertIn("does not exist in this skill", found[0])

    def test_a_reference_that_resolves_is_not_reported(self):
        """The positive control. Without it the rule could reject everything and pass."""
        f = self._skill("See `references/present.md` for the details.\n")
        self.assertEqual(vcr.check_bundled_refs_resolve(f, self.root), [])

    def test_a_placeholder_is_not_a_path(self):
        """`<...>`, `⟪...⟫` and globs are shapes an author writes, not files to resolve.

        The pack already uses this convention (`skills/<name>/`, `-C <dir>`), and `plan-duel`
        writes `⟪workdir⟫/...` throughout. Reading either as a path would make the rule fire
        on correct prose, which is how an author learns to write around it.
        """
        for token in ("references/<name>.md", "references/\u27easlug\u27eb.md",
                      "references/*.md"):
            with self.subTest(token=token):
                f = self._skill(f"See `{token}` for the details.\n")
                self.assertEqual(vcr.check_bundled_refs_resolve(f, self.root), [])

    def test_the_rule_runs_from_inside_references_too(self):
        """A reference written in `references/x.md` is still relative to the SKILL root.

        That is the convention every skill already uses, and getting it wrong in the other
        direction would make `references/present.md` unresolvable from its own sibling.
        """
        f = self.root / "references" / "guide.md"
        f.write_text("See `references/present.md`.\n", encoding="utf-8")
        self.assertEqual(vcr.check_bundled_refs_resolve(f, self.root), [])


class TheShippedSkillsHaveNoDanglingReferences(unittest.TestCase):
    """The rule above, applied to the real pack — the thing it exists to protect."""

    def test_every_reference_in_every_skill_resolves(self):
        problems = []
        for skill_md in sorted((REPO_ROOT / "skills").glob("*/SKILL.md")):
            root = skill_md.parent
            for path, _rel, suffix in vcr.walk_tree_files(root):
                if suffix == ".md":
                    problems.extend(vcr.check_bundled_refs_resolve(path, root))
        self.assertEqual(problems, [], "\n".join(problems))


class TheContractAndTheCodeNameEachOther(unittest.TestCase):
    """`PORTABILITY.md` describes the rules; the validator implements them, separately.

    Two statements of one intent with nothing tying them together, which is the drift shape
    this repository keeps finding in itself. A mapping that pins only some sections leaves
    the rest deletable with nothing noticing.
    """

    def test_every_named_check_exists(self):
        """A mapping naming a function that has been renamed is worse than no mapping."""
        for section, checks in vcr.PORTABILITY_SECTION_CHECKS.items():
            for name in checks:
                with self.subTest(section=section, check=name):
                    self.assertTrue(
                        callable(getattr(vcr, name, None)),
                        f"{section!r} names {name!r}, which is not a function in the "
                        f"validator any more")

    def test_every_section_in_the_contract_is_pinned(self):
        """The document may not grow a section the check does not know about.

        The reverse of the rule the validator already enforces. That one stops a section
        being deleted; this one stops one being added and left unpinned, which is how the
        eight-of-sixteen gap opened in the first place.
        """
        import re
        headings = re.findall(r"^## (.+)$",
                              (REPO_ROOT / "PORTABILITY.md").read_text(encoding="utf-8"),
                              re.MULTILINE)
        unpinned = [
            h for h in headings
            if not any(re.match(pattern, h)
                       for pattern in vcr.PORTABILITY_SECTION_CHECKS)
        ]
        self.assertEqual(
            unpinned, [],
            "PORTABILITY.md has sections no pattern matches, so they can be deleted "
            "silently — add them to PORTABILITY_SECTION_CHECKS, with an empty tuple if "
            "no lexical rule enforces them")

    def test_a_prose_only_section_is_declared_rather_than_omitted(self):
        """An empty tuple is a statement; a missing key is an oversight. Keep them apart."""
        self.assertEqual(
            [s for s, c in vcr.PORTABILITY_SECTION_CHECKS.items() if c == ()],
            ["Parallel", "Shell Assumptions", "Verifying a skill pack"],
            "the set of sections with no lexical rule changed — if that is deliberate, "
            "update this list in the same change so the next reader sees the two together")


class TheUnbornRepoUnstageActuallyUnstages(unittest.TestCase):
    """The one command this skill offers for a staged secret in a repository with no commits.

    `git rm --cached` refuses the moment the file was edited after being staged — which is
    exactly the shape a secret caught mid-edit has — so the advice failed in the single case
    it exists for, and the first commit is where a stray `.env` is likeliest to be sitting.
    """

    # Matched across a line break: prose gets rewrapped, and an anchor that breaks on
    # rewrapping sends both tests below to read the end of the file instead.
    MARKER = re.compile(r"Use the form that\s+needs no history:")

    def _shipped_command(self):
        text = (SKILLS / "commit" / "SKILL.md").read_text(encoding="utf-8")
        parts = self.MARKER.split(text, 1)
        self.assertEqual(len(parts), 2, "the unborn-repo paragraph moved; update this test")
        blocks = fenced_blocks(parts[1])
        self.assertTrue(blocks, "no code block follows that sentence")
        return shell_code(blocks[0][1]).strip()

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_it_unstages_a_secret_edited_after_staging_and_keeps_the_file(self):
        command = self._shipped_command().replace("<path>", ".env")
        self.assertTrue(command.startswith("git "), command)
        with tempfile.TemporaryDirectory() as d:
            def git(*args):
                return subprocess.run(("git",) + args, cwd=d,
                                      capture_output=True, text=True)
            git("init", "-q", ".")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "t")
            secret = Path(d) / ".env"
            secret.write_text("SECRET=1\n", encoding="utf-8")
            git("add", ".env")
            secret.write_text("SECRET=2\n", encoding="utf-8")  # edited AFTER staging
            done = subprocess.run(command.split(), cwd=d, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0,
                             f"the shipped command {command!r} failed: "
                             f"{done.stderr.strip()}")
            self.assertEqual(git("diff", "--cached", "--name-only").stdout.strip(), "",
                             "the secret is still staged for the first commit")
            self.assertTrue(secret.exists(), "the working-tree copy was removed with it")

    def test_the_two_failures_are_named_separately(self):
        """One error was quoted for both commands, and they do not fail alike: `restore
        --staged` cannot resolve HEAD, while `reset HEAD <path>` calls HEAD an ambiguous
        argument. A reader given the wrong message looks for the wrong problem."""
        text = (SKILLS / "commit" / "SKILL.md").read_text(encoding="utf-8")
        # Whitespace-normalized: every phrase below is one a rewrap can split across
        # lines, and an assertion that breaks on rewrapping reports the wrong thing.
        paragraph = " ".join(self.MARKER.split(text, 1)[0][-1200:].split())
        self.assertIn("could not resolve HEAD", paragraph)
        self.assertIn("ambiguous argument", paragraph,
                      "`git reset HEAD <path>` fails with its own message, not that one")


def _powershells():
    """Every PowerShell on this host, and the shipped block is run under each of them.

    The block targets Windows PowerShell 5.1, which is what runs on a user's Windows
    machine, and a host that has PowerShell 7 beside it must exercise both: `pwsh` alone
    answers only for 7, whose parser and cmdlets differ from 5.1's. `PWSH=` names one
    explicitly and comes first; the rest are whatever the path offers. Deduplicated by
    resolved path, because on Linux the only one is `pwsh` while on Windows `powershell` is
    5.1 and a different program.
    """
    seen, found = set(), []
    for candidate in (os.environ.get("PWSH"), shutil.which("pwsh"), shutil.which("powershell")):
        if not candidate:
            continue
        key = os.path.realpath(candidate).lower()
        if key not in seen:
            seen.add(key)
            found.append(candidate)
    return found


POWERSHELLS = _powershells()
PWSH = POWERSHELLS[0] if POWERSHELLS else None


class TheRunDirectoryIsOutsideTheAuditedTree(unittest.TestCase):
    """This skill's central promise is that it never writes into the code it is reading.

    Both safety checks compared how a path is SPELLED. A symlink, a bind mount or a `..`
    segment can spell a path outside the tree and resolve inside it, and the whole report
    then lands in the user's source.
    """

    DOC = SKILLS / "security-review-codebase" / "references" / "hierarchical-mode.md"

    def _block(self, marker):
        found = [body for _line, body in fenced_blocks(self.DOC.read_text(encoding="utf-8"))
                 if marker in body]
        self.assertEqual(len(found), 1, f"expected exactly one block containing {marker!r}")
        return found[0]

    @unittest.skipUnless(BASH and shutil.which("git") and os.name == "posix",
                         "the POSIX half of the document, and its link needs privileges "
                         "on Windows")
    def test_a_temp_directory_that_resolves_inside_the_tree_is_refused(self):
        block = self._block("security-review-$(basename")
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            (repo / "scratch").mkdir(parents=True)
            for args in (("init", "-q", "."), ("config", "user.email", "t@example.com"),
                         ("config", "user.name", "t")):
                subprocess.run(("git",) + args, cwd=repo, capture_output=True, text=True)
            # Spelled outside the repository, resolving inside it.
            link = Path(d) / "link"
            link.symlink_to(repo / "scratch", target_is_directory=True)

            env = dict(os.environ, TMPDIR=str(link))
            done = run_bash(block, cwd=repo, env=env,
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            run = Path(done.stdout.strip().splitlines()[-1])
            self.addCleanup(shutil.rmtree, str(run), True)
            inside = str(run.resolve()).startswith(str(repo.resolve()) + os.sep)
            self.assertFalse(inside,
                             f"the run directory {run} resolves inside the audited tree at "
                             f"{repo} — the report would be written into the code under review")

    @unittest.skipUnless(BASH and shutil.which("git") and os.name == "posix",
                         "the POSIX half of the document")
    def test_a_safe_temp_directory_is_still_used_as_given(self):
        """The other half of resolving: a base that leads outside must be kept. A guard, not
        a reproduction — it passes before the fix too. Without it, a resolve that failed
        silently would send every run to /tmp and cost the project name with it."""
        block = self._block("security-review-$(basename")
        with tempfile.TemporaryDirectory() as d:
            repo, safe = Path(d) / "repo", Path(d) / "safe"
            repo.mkdir()
            safe.mkdir()
            for args in (("init", "-q", "."), ("config", "user.email", "t@example.com"),
                         ("config", "user.name", "t")):
                subprocess.run(("git",) + args, cwd=repo, capture_output=True, text=True)
            done = run_bash(block, cwd=repo,
                                  env=dict(os.environ, TMPDIR=str(safe)),
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            run = Path(done.stdout.strip().splitlines()[-1])
            self.addCleanup(shutil.rmtree, str(run), True)
            self.assertTrue(run.is_dir(), "the run directory was not created")
            self.assertEqual(run.parent.resolve(), safe.resolve(),
                             "a safe TMPDIR was discarded, so every run lands in /tmp")
            self.assertIn("security-review-repo-", run.name, "the project name was lost")

    def test_the_windows_check_resolves_both_paths_before_comparing(self):
        """Asserted on the source, because the block cannot run here: its own
        fully-qualified test demands a drive letter or a UNC path, so every POSIX path is
        refused before the containment check is ever reached."""
        block = self._block("Test-SafeBase")
        self.assertIn("function Resolve-Physical", block,
                      "nothing resolves a junction or a `..` segment before the compare")
        safe = block.split("function Test-SafeBase", 1)[1].split("\n   }", 1)[0]
        for side in ("$candidate", "$repoRoot"):
            self.assertIn(f"Resolve-Physical {side}", safe,
                          f"{side} is still compared as spelled")

    def test_every_shipped_powershell_block_is_ascii(self):
        """Windows PowerShell 5.1 reads a script file without a byte-order mark in the ANSI
        code page, so an em dash's UTF-8 bytes arrive as three other characters, one of
        which it takes for a closing quote: a string holding one ends early and the block
        does not parse. Asserted here, on every host, because only a Windows runner with
        5.1 can run the parse check below."""
        checked = 0
        for doc in sorted(SKILLS.rglob("*.md")):
            text = doc.read_text(encoding="utf-8")
            for line, body in fenced_blocks(text):
                if not text.splitlines()[line - 1].strip().startswith("```powershell"):
                    continue
                checked += 1
                with self.subTest(doc=doc.relative_to(SKILLS).as_posix(), line=line):
                    stray = sorted({ch for ch in body if ord(ch) > 127})
                    self.assertEqual(stray, [], "non-ASCII characters in a PowerShell block")
        self.assertGreater(checked, 0, "no PowerShell block was found, so nothing was checked")

    @unittest.skipUnless(PWSH, "no PowerShell on this host")
    def test_the_windows_block_parses(self):
        """Nothing in this repository has ever parsed that block. A typo in it fails on a
        user's Windows machine, which is the one place it runs. Parsed under every
        PowerShell on the host, because the block targets Windows PowerShell 5.1 and a
        runner that has it beside PowerShell 7 must answer for 5.1 too."""
        block = self._block("Test-SafeBase")
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "block.ps1"
            src.write_text(block, encoding="utf-8")
            check = (
                "$errs = $null\n"
                "[void][System.Management.Automation.Language.Parser]::ParseFile("
                f"'{src}', [ref]$null, [ref]$errs)\n"
                "if ($errs.Count) {{ $errs | ForEach-Object {{ $_.ToString() }}; exit 1 }}\n"
            ).replace("{{", "{").replace("}}", "}")
            for shell in POWERSHELLS:
                with self.subTest(shell=shell):
                    # Decoded explicitly and forgivingly: Windows PowerShell 5.1 answers in
                    # the console code page, and a byte the host's default cannot map
                    # crashes the reader thread and loses the parse error being reported.
                    done = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", check],
                                          capture_output=True, encoding="utf-8",
                                          errors="replace")
                    self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class TheBoundedPollReportsWhatItActuallySaw(unittest.TestCase):
    """The poll's own exit status is what a caller reads.

    It exited 0 whether the marker arrived or the clock ran out, while the paragraph beside
    it says a non-zero exit means the poll timed out — so a caller learned "finished" from
    a run that never finished. And the block is Bash throughout, on a step whose text
    claims any host.
    """

    FILES = ("plan-run", "plan-run-v1")

    def _detach_block(self, skill):
        doc = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
        blocks = [body for _line, body in fenced_blocks(doc) if "WORK-EXIT rc=%s" in body]
        self.assertEqual(len(blocks), 1, f"{skill}: expected exactly one detach block")
        return blocks[0]

    def _run_poll(self, skill, log_text):
        block = self._detach_block(skill)
        start = block.index("for i in $(seq")
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "work.log"
            log.write_text(log_text, encoding="utf-8")
            # Two substitutions only: the literal path this skill documents, and the loop's
            # timing. The predicate and the exit behavior under test are shipped as-is.
            code = (block[start:].replace("/abs/path/work.log",
                                          str(log).replace("\\", "/"))
                                 .replace("$(seq 1 240)", "$(seq 1 1)")
                                 .replace("sleep 15", "sleep 0"))
            return run_bash(code, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=60)

    @unittest.skipUnless(BASH, "needs bash")
    def test_a_marker_that_arrived_is_success(self):
        for skill in self.FILES:
            with self.subTest(skill=skill):
                done = self._run_poll(skill, "some output\nWORK-EXIT rc=0\n")
                self.assertEqual(done.returncode, 0,
                                 f"{skill}: a finished run was reported as a failure: "
                                 f"{done.stderr.strip()[:200]}")

    @unittest.skipUnless(BASH, "needs bash")
    def test_a_marker_that_never_arrived_is_not_success(self):
        for skill in self.FILES:
            with self.subTest(skill=skill):
                done = self._run_poll(skill, "output, and no marker at all\n")
                self.assertNotEqual(done.returncode, 0,
                                    f"{skill}: the poll ran out of time and still exited 0, "
                                    f"so an unfinished run reads as a finished one")

    def test_the_detach_block_says_what_a_native_windows_shell_should_do(self):
        """`setsid`, `nohup`, `&` and `seq` exist in no native-Windows shell. Naming the
        primitives for each decision is this family's own idiom — the decisions are the
        contract, not the spelling — and it beats shipping a second implementation that
        nothing here tests."""
        for skill in self.FILES:
            with self.subTest(skill=skill):
                doc = " ".join((SKILLS / skill / "SKILL.md")
                               .read_text(encoding="utf-8").split())
                for primitive, decision in (("Start-Process", "detaching the work"),
                                            ("LASTEXITCODE", "carrying the exit status"),
                                            ("Select-String", "polling for the marker")):
                    if primitive not in doc:
                        self.fail(f"{skill}: no native-Windows answer for {decision} — "
                                  f"{primitive} appears nowhere in the skill")


class TheResumeExemptionIsAFileSetNotADirectory(unittest.TestCase):
    """What a resumed run treats as "expected metadata" decides whether it re-verifies.

    Expressed as anything under `plans/<slug>/`, it assumes the plan lives under `plans/`.
    Step 1 finds a plan anywhere — the repository root, or `docs/` — and the prefix then
    exempts the whole tree: work a crash left dirty reads as bookkeeping, the resume skips
    re-verification and the gate, and branch 3 ends at a `git add -A` that commits it.
    """

    def _select(self):
        doc = (SKILLS / "plan-run" / "SKILL.md").read_text(encoding="utf-8")
        start = doc.index("### Select")
        return " ".join(doc[start:doc.index("### Satisfy", start)].split())

    def test_the_exemption_is_not_written_as_a_directory_prefix(self):
        select = self._select()
        if "plans/<slug>/" in select:
            self.fail("Select still decides on a `plans/<slug>/` prefix, which exempts the "
                      "whole repository for a plan that sits at its root")

    def test_the_exemption_names_the_documents_it_means(self):
        """Anchored on the phrase, not on a paragraph. Searching the whole step passes
        vacuously — Select's opening names these files for an unrelated reason — and
        searching one paragraph misses a definition that is deliberately stated up front,
        before the branches that use it."""
        select = self._select()
        phrase = "the plan's own documents"
        if phrase not in select:
            self.fail("Select names no exemption at all, so each branch is back to "
                      "deciding on a directory")
        definition = select[select.index(phrase):][:400]
        for name in ("execution.md", "as-built.md"):
            if name not in definition:
                self.fail(f"the exemption is never defined in terms of {name}, so what "
                          f"counts as bookkeeping is left to the reader to guess")

    def test_a_metadata_only_phase_is_not_said_to_commit_nothing(self):
        """Publish stages the whole tree, so that phase's commit carries its ticks and its
        evidence record. Saying it commits nothing contradicts the step that runs next."""
        select = self._select()
        if "commits nothing at all" in select:
            self.fail("Select says a metadata-only phase commits nothing, while Publish's "
                      "`git add -A` commits its ticked boxes and evidence record")


class ThePushAsksTheRemoteNotItsLocalCopy(unittest.TestCase):
    """`origin/<branch>` is only as fresh as the last fetch.

    A branch deleted or rewound elsewhere leaves that ref still matching HEAD, so the guard
    decides the work is already published, skips the push, and the phase is ticked with
    nothing on the remote. Every one of the four push sites asked the local copy.
    """

    def _guards(self):
        for skill in ("plan-run", "plan-run-v1"):
            doc = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
            for line, body in fenced_blocks(doc):
                if "git push origin HEAD" not in body:
                    continue
                start = re.search(r"^\s*(BRANCH|branch)=", body, re.M)
                self.assertIsNotNone(start, f"{skill}:{line}: no branch lookup in the block")
                yield skill, line, body[start.start():]

    def test_the_four_push_sites_are_found(self):
        """Anti-vacuity: the case below proves nothing if it iterates over nothing."""
        found = [(skill, line) for skill, line, _guard in self._guards()]
        self.assertEqual(len(found), 4, f"expected four push sites, found {found}")

    @unittest.skipUnless(shutil.which("git") and BASH, "needs git and bash")
    def test_a_remote_branch_deleted_elsewhere_is_pushed_again(self):
        for skill, line, guard in self._guards():
            with self.subTest(skill=skill, line=line), tempfile.TemporaryDirectory() as d:
                bare, work = Path(d) / "origin.git", Path(d) / "work"
                subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True,
                               capture_output=True)
                subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True,
                               capture_output=True)

                def git(*args):
                    return subprocess.run(("git",) + args, cwd=work, capture_output=True,
                                          text=True, encoding="utf-8", errors="replace")

                git("config", "user.email", "t@example.com")
                git("config", "user.name", "t")
                git("checkout", "-q", "-b", "feature")
                (work / "f.txt").write_text("one\n", encoding="utf-8")
                git("add", "f.txt")
                git("commit", "-qm", "one")
                git("push", "-q", "origin", "HEAD")
                # A DECOY. `ls-remote`'s patterns match from the right on path components,
                # so a bare `feature` also matches `archive/feature` — and a guard that
                # accepts that hit skips publishing the branch it was asked about.
                git("push", "-q", "origin", "HEAD:refs/heads/archive/feature")
                # And one that survives asking for the exact ref, since the pattern still
                # matches from the right: `refs/heads/feature` matches this too.
                git("push", "-q", "origin",
                    "HEAD:refs/heads/archive/refs/heads/feature")
                # Deleted straight in the remote, so this clone's `origin/feature` stays
                # behind — exactly what a branch rewound or deleted by someone else leaves.
                subprocess.run(["git", "-C", str(bare), "update-ref", "-d",
                                "refs/heads/feature"], check=True, capture_output=True)
                def remote_tip(ref):
                    """What the guard asks: the hash of the ref whose NAME matches exactly.

                    `ls-remote`'s patterns match from the right, so both decoys above answer
                    a pattern query. A precondition asked loosely is answered by a branch
                    nobody enquired about, and this case then proves nothing.
                    """
                    out = git("ls-remote", "--heads", "origin", ref).stdout
                    return "".join(line.split("\t")[0] for line in out.splitlines()
                                   if line.split("\t")[1:] == [ref])

                self.assertEqual(remote_tip("refs/heads/feature"), "",
                                 "the remote branch was not actually removed")

                done = run_bash(guard, cwd=work, capture_output=True,
                                      text=True, encoding="utf-8", errors="replace",
                                      timeout=60)
                restored = remote_tip("refs/heads/feature")
                self.assertTrue(
                    restored,
                    f"{skill}:{line}: the push was skipped because the LOCAL copy of the "
                    f"remote ref still matched HEAD, so the commit stayed unpublished while "
                    f"the phase ticks anyway. Block said: "
                    f"{(done.stdout + done.stderr).strip()[:160]!r}")


class TheV1WindowsNoteAndResumeAreComplete(unittest.TestCase):
    """Two places in the superseded suite where a partial statement changes what happens.

    Its commit-and-push block carries four decisions. The adapter note for a native-Windows
    shell — the one place a reader is told to reimplement the block — listed two, and the two
    it left out are the guards that stop every phase being pushed to the default branch.

    And its resume reads "Tasks and Tests ticked, Exit Criteria not" as ungated work. The
    bookkeeping step ticks those criteria ONE AT A TIME, so a crash inside it leaves some
    ticked: that reads as ungated, re-runs the gate, and lands the bookkeeping-only commit
    the third branch exists to avoid.
    """

    def _doc(self):
        return " ".join((SKILLS / "plan-run-v1" / "SKILL.md")
                        .read_text(encoding="utf-8").split())

    def _windows_note(self):
        """Anchored on wording unique to the commit-and-push note. Two adapter notes in this
        file now open with the same sentence, and the first one is the detached-work block's."""
        doc = self._doc()
        start = doc.index("carry out the same decisions with your shell's own syntax")
        return doc[start:start + 600]

    def test_the_windows_note_names_all_four_decisions(self):
        note = self._windows_note()
        for phrase, decision in (("staged", "committing only when something is staged"),
                                 ("already published", "skipping an already-published tip"),
                                 ("detached", "refusing to push a detached HEAD"),
                                 ("default branch", "refusing to push the default branch")):
            if phrase not in note:
                self.fail(f"the note omits {decision}, so a reader reimplementing this "
                          f"block on Windows drops that guard")

    def test_the_resume_accounts_for_partly_ticked_exit_criteria(self):
        doc = self._doc()
        start = doc.index("Tasks and Tests ticked")
        branch = doc[start:start + 900]
        if "partly ticked" not in branch:
            self.fail("the resume treats any unticked Exit Criterion as ungated work, so a "
                      "crash part-way through ticking them re-runs a gate that passed and "
                      "commits nothing but the ticks")


class ShippedTextSaysWhatTheCodeDoes(unittest.TestCase):
    """Six places where a document promised something the program does not do.

    Every check here reads whitespace-normalized text: these are sentences, and a sentence
    that gets rewrapped must not change the answer.
    """

    def _norm(self, *parts):
        # Blockquote markers are stripped first. Half of what these check lives inside `>`
        # quotes, and a sentence wrapped across two quoted lines keeps a `>` in the middle
        # of it — which is how one of these passed while checking nothing.
        text = (SKILLS.joinpath(*parts)).read_text(encoding="utf-8")
        return " ".join(re.sub(r"(?m)^\s*>\s?", "", text).split())

    def test_diff_review_does_not_promise_an_exact_recount(self):
        """The supervisor FLOORS the count: a positive claim is never lowered to 0, and an
        unrecognised severity floors it at 1. So the published number can exceed the
        entries listed, and a gate told it matches them is told wrong."""
        text = self._norm("diff-review", "SKILL.md")
        if "never lowered to 0" not in text:
            self.fail("SKILL.md describes the recount without its floor, so the published "
                      "count is presented as equal to the blocker/major entries")

    def test_the_schema_does_not_promise_equality(self):
        schema = " ".join((SKILLS / "diff-review" / "review-schema.json")
                          .read_text(encoding="utf-8").split())
        if "must equal the number of such entries" in schema:
            self.fail("the schema still tells a gate the count equals the entries in "
                      "`findings`, which the floor makes untrue")

    def test_the_rung_one_lines_say_their_paths_are_absolute(self):
        """With `--cwd` set the supervisor refuses a relative output path, and the skill
        reads that refusal as grounds to fall back to a same-model reviewer."""
        text = self._norm("diff-review", "SKILL.md")
        start = text.index("Claude adapter (Claude is running this review)")
        if "absolute" not in text[max(0, start - 400):start]:
            self.fail("nothing near the adapter lines says those paths must be absolute")

    def test_the_writer_claim_is_about_edit_tools(self):
        """The bound the flags actually set is over the edit tools and over a shell command
        judged to change a file — a permission check, not a kernel boundary, so an incidental
        write by a command judged read-only still lands. A flat "can write" claim overstates
        what the flags promise, in either direction."""
        text = self._norm("diff-review", "SKILL.md")
        if "a reviewer that can write is not a review" in text:
            self.fail("the flat claim is still there, in place of the bound the flags "
                      "actually set over the edit tools")

    def test_review_panel_names_git_as_a_prerequisite(self):
        """Auditing a tree inside a repository asks git what is tracked, and `plan` refuses
        without it — while the prerequisite paragraph names only Python."""
        text = self._norm("review-panel", "SKILL.md")
        start = text.index("a prerequisite the skill checks")
        if "git" not in text[start:start + 300]:
            self.fail("only Python is named as a prerequisite, and git is needed too")

    def test_plan_phase_does_not_send_the_reader_to_its_own_step_5(self):
        """Step 5 writes phase documents. The cross-model review lives in `plan-run`."""
        text = self._norm("plan-phase", "SKILL.md")
        start = text.index("cross-model")
        if "see Step 5" in text[start:start + 300]:
            self.fail("the overview sends the reader to Step 5 for the cross-model review, "
                      "which Step 5 never mentions")


class PlanRunsOwnDocumentsAgreeWithEachOther(unittest.TestCase):
    """Six statements in `plan-run` that its own neighbours, adapters or code contradict."""

    def _plan_run(self, *parts):
        text = (SKILLS / "plan-run").joinpath(*parts).read_text(encoding="utf-8")
        return " ".join(re.sub(r"(?m)^\s*>\s?", "", text).split())

    def test_the_progress_line_does_not_promise_the_reviewer_a_log(self):
        """The note below it says the review sub-agent is NOT handed one, because it runs
        read-only and cannot write."""
        text = self._plan_run("SKILL.md")
        line = text[text.index("_Progress: observable"):][:400]
        if "independent-review sub-agent" in line:
            self.fail("the Progress line hands the review sub-agent a progress file, which "
                      "the Delegation note says it never gets")

    def test_the_reviewer_exemption_does_not_rest_on_streaming_alone(self):
        """Only rung 1 streams. On rung 2 the review is an in-harness sub-agent and nothing
        streams at all, so "it already streams" explains nothing there."""
        text = self._plan_run("SKILL.md")
        start = text.index("sub-agent is not handed one")
        if "rung 2" not in text[start:start + 400]:
            self.fail("the exemption cites diff-review's streaming without saying that only "
                      "rung 1 streams")

    def test_watching_the_log_is_possible_on_windows(self):
        text = self._plan_run("SKILL.md")
        start = text.index("`tail -f` it in another pane")
        if "Get-Content" not in text[max(0, start - 200):start + 200]:
            self.fail("`tail -f` is offered as the any-runtime answer, and no stock Windows "
                      "shell has it")

    def test_the_briefs_pass_what_the_contract_requires(self):
        """The contract names a fourth input and points at this skill for the procedure.
        A worker handed neither has no progress file to write and no procedure to follow."""
        text = self._plan_run("SKILL.md")
        start = text.index("Claude adapter:** dispatch a phase")
        adapters = text[start:start + 1600]
        if "progress file" not in adapters:
            self.fail("neither adapter brief passes the progress file the contract and the "
                      "Progress line both promise the worker")

    def test_the_as_built_template_does_not_hardcode_the_plan_filename(self):
        text = self._plan_run("references", "as-built-template.md")
        if "](./plan.md)" in text:
            self.fail("the template links to `./plan.md`, but a plan can carry any name, so "
                      "the link is broken or points at the wrong file")

    def test_the_decisions_quote_matches_the_shipped_check(self):
        """A ledger arguing for a rule has to quote the rule it argues for."""
        quoted = self._plan_run("DECISIONS.md")
        start = quoted.index("The check in `Publish` is:")
        block = quoted[start:start + 900]
        for token, what in (("ls-remote", "asking the remote rather than its local copy"),
                            ('"HEAD"', "the detached-HEAD guard"),
                            ("DEFAULT", "the default-branch guard")):
            if token not in block:
                self.fail(f"the quoted check is missing {what}")
        # Token presence is not the quote being current: `ls-remote` is also in the
        # published-tip line, so a quote still carrying a retired derivation of `DEFAULT`
        # passed the loop above. The derivation lines are held to the shipped block verbatim.
        derivation = {
            " ".join(line.split())
            for _where, code in commit_push_blocks("plan-run")
            for line in code.splitlines()
            if line.startswith("DEFAULT=") or line.startswith('[ -n "$DEFAULT" ]')
        }
        self.assertTrue(derivation, "no `DEFAULT` derivation found in a shipped push block")
        for line in sorted(derivation):
            if line not in block:
                self.fail(f"the quoted check derives DEFAULT differently from the shipped "
                          f"block; missing: {line}")


class PlanInitAndDemoVideoKeepTheirPromises(unittest.TestCase):
    """Three promises whose mechanism was somewhere else, or nowhere."""

    def _norm(self, *parts):
        text = SKILLS.joinpath(*parts).read_text(encoding="utf-8")
        return " ".join(re.sub(r"(?m)^\s*>\s?", "", text).split())

    def test_the_index_step_keys_on_the_path_not_the_slug(self):
        """The Overview promises a row for any plan under `plans/`. Step 7 skipped unless
        Steps 2 and 5 had generated a slug, so `plans/custom/plan.md` — under `plans/`, and
        perfectly linkable — got none."""
        text = self._norm("plan-init", "SKILL.md")
        if "Step 2 and Step 5 generated a `<slug>`" in text:
            self.fail("Step 7 still decides on whether a slug was generated, so a plan the "
                      "user placed under `plans/` himself is left out of the index")

    def test_the_template_says_which_marker_is_actually_read(self):
        """Calling both rows markers invites someone to treat `Suite` as required, or to
        drop `Format` as decoration. One is refused on; the other is a record."""
        text = self._norm("plan-init", "references", "plan-template.md")
        if "nothing reads it" not in text:
            self.fail("the template presents Format and Suite as equally required, though "
                      "only Format is checked")

    def test_the_tour_spec_records_what_the_subtitles_need(self):
        """`subtitles.md` derives timing from each step's start and duration, citing the
        tour spec. The spec's `step()` had a comment where the recording should be, and
        cited `subtitles.md` back."""
        spec = self._norm("demo-video", "references", "guided-tour-spec.md")
        for token in ("duration", "push"):
            if token not in spec:
                self.fail(f"the tour spec never records {token}, so subtitle timing has no "
                          f"source and each file points at the other for it")


class TheSafetyChecksSurviveTheirOwnFixes(unittest.TestCase):
    """Four ways a containment or timing check is right in the small and wrong overall.

    Two are the shape a fix itself introduces. Resolving only the leaf of a path leaves a
    junction ABOVE it unresolved, which is worse than comparing the paths as text — text at
    least catches a temp base spelled under the root. And a clock started when the module
    loads runs before the recording exists, so every caption carries the launch offset.
    """

    DOC = SKILLS / "security-review-codebase" / "references" / "hierarchical-mode.md"

    def _block(self, marker):
        found = [body for _line, body in fenced_blocks(self.DOC.read_text(encoding="utf-8"))
                 if marker in body]
        self.assertEqual(len(found), 1, f"expected one block containing {marker!r}")
        return found[0]

    def _ps_function(self, name):
        """One function lifted out by brace matching, so it can be tested without running
        the whole block — which resolves a repository root and throws outside one."""
        block = self._block("Test-SafeBase")
        start = block.index(f"function {name}(")
        i, depth = block.index("{", start), 0
        while True:
            if block[i] == "{":
                depth += 1
            elif block[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        return block[start:i + 1]

    @unittest.skipUnless(PWSH, "no PowerShell on this host (set PWSH= to point at one)")
    def test_a_link_above_the_leaf_is_resolved_too(self):
        """A junction ABOVE the candidate moves everything below it. `C:\\work` linked to
        `D:\\repo` leaves `C:\\work\\tmp` looking unrelated to the root it sits inside — and
        the textual comparison this replaced caught that one, so resolving only the leaf
        made the case worse."""
        with tempfile.TemporaryDirectory() as d:
            real = Path(d) / "real"
            (real / "inner").mkdir(parents=True)
            link = Path(d) / "link"
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"cannot create a link here: {exc}")
            script = Path(d) / "fn.ps1"
            script.write_text(self._ps_function("Resolve-Physical") +
                              f'\nResolve-Physical "{link / "inner"}"\n', encoding="utf-8")
            for shell in POWERSHELLS:
                with self.subTest(shell=shell):
                    done = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(script)],
                                          capture_output=True, text=True, timeout=120)
                    self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                    answer = Path(done.stdout.strip())
                    self.assertEqual(answer.resolve(), (real / "inner").resolve(),
                                     "the ancestor link was left unresolved, so containment "
                                     "is decided against a path that is not where the files "
                                     "land")

    @unittest.skipUnless(BASH and os.name == "posix", "the POSIX half of the document")
    def test_the_fallback_base_is_checked_too(self):
        """An audit rooted where the fallback lives. The PowerShell block already refuses
        this; POSIX quietly accepted it."""
        block = self._block("security-review-$(basename")
        fallback = Path("/tmp")   # the block's own fallback, not the platform's
        done = run_bash(block, cwd=str(fallback),
                              env=dict(os.environ, TMPDIR=str(fallback)),
                              capture_output=True, text=True, timeout=60)
        if done.returncode == 0:
            stray = done.stdout.strip().splitlines()[-1]
            self.addCleanup(shutil.rmtree, stray, True)
            self.fail(f"an audit rooted at {fallback} put its run directory at {stray}, "
                      f"inside the tree it is reading")
        self.assertIn("outside", (done.stdout + done.stderr).lower())

    def test_the_comment_claims_only_what_pwd_p_resolves(self):
        """`pwd -P` resolves pathname links. A bind mount is not a link, and claiming it is
        covered is the same class of defect these commits were fixing."""
        text = " ".join(self.DOC.read_text(encoding="utf-8").split())
        window = text[text.index("Compared by WHERE IT LEADS"):][:500]
        if "bind mount" in window and "NOT covered" not in window:
            self.fail("the comment lists a bind mount among what it resolves, and `pwd -P` "
                      "does not see through one")
        if "NOT covered" not in window:
            self.fail("the comment never states its limit, so the next reader assumes a "
                      "mount alias is handled")

    def test_the_tour_clock_starts_with_the_recording(self):
        """`t0` at module level runs before Playwright creates the page, so every caption
        carries the launch offset — the very error the subtitle document warns about."""
        spec = (SKILLS / "demo-video" / "references" / "guided-tour-spec.md").read_text(
            encoding="utf-8")
        if "const t0 = Date.now();" in spec:
            self.fail("t0 is captured at module evaluation, before the recording exists")
        if "beforeEach" not in spec:
            self.fail("t0 is set in the test body, which runs after any hook that already "
                      "used the recorded page — that time is video the captions miss")
        start = spec.index("beforeEach")
        if "t0 = Date.now()" not in spec[start:start + 400]:
            self.fail("t0 is never set in the first hook to receive the page")


class TheResolversAnswerIsPhysicalOrItIsNothing(unittest.TestCase):
    """The remaining ways a path check can be satisfied by a spelling.

    Resolution has to proceed from the root DOWN. A relative target means nothing until the
    directory holding it is itself physical: `..\\scratch` under a junction reads against the
    junction's spelled parent, not the real one. And where resolution cannot finish, the
    answer must be a refusal — falling back to the unresolved spelling hands the decision to
    the text the resolver exists to distrust.
    """

    DOC = SKILLS / "security-review-codebase" / "references" / "hierarchical-mode.md"

    def _block(self, marker):
        found = [body for _line, body in fenced_blocks(self.DOC.read_text(encoding="utf-8"))
                 if marker in body]
        self.assertEqual(len(found), 1, f"expected one block containing {marker!r}")
        return found[0]

    def _ps_function(self, name):
        block = self._block("Test-SafeBase")
        start = block.index(f"function {name}(")
        i, depth = block.index("{", start), 0
        while True:
            if block[i] == "{":
                depth += 1
            elif block[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        return block[start:i + 1]

    @unittest.skipUnless(PWSH, "no PowerShell on this host (set PWSH= to point at one)")
    def test_a_relative_target_resolves_against_its_real_parent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            (root / "sub").mkdir(parents=True)
            (root / "scratch").mkdir()
            try:
                # `alias` -> repo/sub, and repo/sub/temp -> ../scratch, written RELATIVE.
                (Path(d) / "alias").symlink_to(root / "sub", target_is_directory=True)
                (root / "sub" / "temp").symlink_to("../scratch", target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"cannot create a link here: {exc}")
            script = Path(d) / "fn.ps1"
            script.write_text(self._ps_function("Resolve-Physical") +
                              f'\nResolve-Physical "{Path(d) / "alias" / "temp"}"\n',
                              encoding="utf-8")
            for shell in POWERSHELLS:
                with self.subTest(shell=shell):
                    done = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(script)],
                                          capture_output=True, text=True, timeout=120)
                    self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                    answer = Path(done.stdout.strip())
                    self.assertEqual(answer.resolve(), (root / "scratch").resolve(),
                                     "the relative target was resolved against the "
                                     "junction's spelled parent, so a directory inside the "
                                     "audited tree reads as one outside it")

    @unittest.skipUnless(BASH and os.name == "posix", "needs a POSIX bash")
    def test_an_audit_rooted_at_the_filesystem_root_is_refused(self):
        """`case "$base/" in "$root_real"/*)` becomes `//*` when the root is `/`, which no
        single-slash path matches — so every base read as outside a tree that contains
        everything. The trailing slash has to come off before the comparison."""
        block = self._block("security-review-$(basename")
        done = run_bash(block, cwd="/",
                              env=dict(os.environ, TMPDIR="/tmp"),
                              capture_output=True, text=True, timeout=60)
        if done.returncode == 0:
            stray = done.stdout.strip().splitlines()[-1]
            self.addCleanup(shutil.rmtree, stray, True)
            self.fail(f"an audit rooted at / accepted {stray}, which is inside it")
        self.assertIn("outside", (done.stdout + done.stderr).lower())

    def test_an_unresolvable_root_is_refused_not_assumed(self):
        safe = self._ps_function("Test-SafeBase")
        if "$rootReal = $repoRoot" in safe:
            self.fail("a root that cannot be resolved falls back to its spelling, which is "
                      "the comparison the resolver exists to replace")

    def test_the_posix_fallback_is_resolved_wherever_it_is_assigned(self):
        """Two paths assign the fallback: the base cannot be entered, and the base resolves
        inside the tree. Both have to resolve it, or `/tmp` is compared as text."""
        block = self._block("security-review-$(basename")
        # From the resolution onward. The relative-path guard above it assigns a literal
        # `/tmp` and is then resolved by the line below it, which is fine; what must not
        # happen is an unresolved assignment surviving PAST that point.
        tail = block[block.index("base=$( (cd -P"):]
        bare = [line.strip() for line in tail.splitlines()
                if re.search(r"base=/tmp(\s|$)", line) and not line.strip().startswith("#")]
        if bare:
            self.fail(f"a fallback is assigned the literal /tmp after resolution: {bare}")
        self.assertIn("cd -P -- /tmp", block,
                      "the fallback is never resolved anywhere, so /tmp is compared as text")


class TheMaintainersGuideNamesNothingThatWasRenamed(unittest.TestCase):
    """`review-panel/references/architecture.md` explains two programs by naming their
    internals. A guide naming something that no longer exists is worse than no guide: a
    reader trusts it, finds nothing, and cannot tell a rename from their own mistake.

    Two checks, and the second is the one that survives editing. The first pins the symbols
    the guide is *required* to name, so deleting a section fails here rather than quietly
    narrowing what the guide covers. The second holds every private name the guide mentions
    in backticks — whatever they turn out to be — against the modules, so a symbol added to
    the guide later is covered without anyone extending a list. A leading underscore is the
    discriminator because ordinary prose never carries one.
    """

    GUIDE = SKILLS / "review-panel" / "references" / "architecture.md"

    # Named in the guide and resolved in the module it belongs to. The value is the
    # attribute's owner: "engine" is `review_panel.py`, "driver" is `review_panel_run.py`.
    REQUIRED = {
        "_MockDialect": "engine",
        "_MOCK_DIALECTS": "engine",
        "_for_stem": "engine",
        "_fence_language": "engine",
        "_FENCE_LANGUAGES": "engine",
        "_mock_only": "engine",
        "_statements": "engine",
        # The guide's order — strings located before comments are stripped — is this
        # function, and a reader told the order without the name cannot go and read it.
        "_strip_comments": "engine",
        "subject_map": "engine",
        "subjects_by_name": "engine",
        "subjects_by_mention": "engine",
        "SUBJECT_BY_NAME": "engine",
        "SUBJECT_BY_MENTION": "engine",
        "SUBJECT_HOW_SAID": "engine",
        "UNIT_COMPLETE": "engine",
        "UNIT_FAILED": "engine",
        "UNIT_MISSING": "engine",
        "ROUNDS": "driver",
        "WRITE_CAPABLE_KINDS": "driver",
    }

    # The engine's stage markers, by the constant that holds each one. `reported` is the
    # newest and the easiest to leave out of a table that is otherwise still correct.
    #
    # TWO SETS, because the constants are two different kinds of thing and one table
    # conflating them is what put a stage in the guide that the loop never passes through.
    # `UNITS_STAGE_CONSTANTS` is what `units.json` can hold, which is what the driver's
    # `ROUNDS` keys on and what the guide's table is about. `ROUTED_STAGE` is written into
    # `candidates.json` as a TYPE TAG -- `_read_candidates` refuses a file whose `stage` is
    # not `routed` -- so it is a real run value, and belongs in the vocabulary the guide may
    # quote, but never in the table.
    UNITS_STAGE_CONSTANTS = ("READING_STAGE", "VERIFICATION_STAGE", "CLUSTERED_STAGE",
                             "SYNTHESIZED_STAGE", "REPORTED_STAGE")
    STAGE_CONSTANTS = UNITS_STAGE_CONSTANTS + ("ROUTED_STAGE",)

    @classmethod
    def setUpClass(cls):
        skill_dir = SKILLS / "review-panel"
        if str(skill_dir) not in sys.path:
            sys.path.insert(0, str(skill_dir))
        import review_panel
        import review_panel_run
        cls.modules = {"engine": review_panel, "driver": review_panel_run}
        cls.text = cls.GUIDE.read_text(encoding="utf-8")
        cls.quoted = set(re.findall(r"`([^`\n]+)`", cls.text))

    def test_every_required_symbol_is_named_and_exists(self):
        missing_from_guide, missing_from_code = [], []
        for name, where in self.REQUIRED.items():
            if name not in self.quoted:
                missing_from_guide.append(name)
            if not hasattr(self.modules[where], name):
                missing_from_code.append(f"{name} ({where})")
        self.assertEqual(missing_from_guide, [],
                         "the guide no longer names these, so a section it is required to "
                         "cover has gone or been reworded past recognition")
        self.assertEqual(missing_from_code, [],
                         "the guide names these and the code does not define them")

    def _markers(self):
        """The test-name markers, read out of the engine rather than listed here.

        `_test` and `_spec` are DATA the guide quotes, not symbols, and a hand-written
        exemption list would have to be extended by whoever adds a marker.
        """
        engine = self.modules["engine"]
        return set(engine._SUBJECT_MARKERS_TRAILING) | set(engine._SUBJECT_MARKERS_LEADING)

    def _vocabulary(self):
        """Every value the run's own namespaces can take, built from the two programs.

        The driver's verbs, the stage markers, the attempt states, the dispositions, the
        three unit landings and the dialect keys. A skill's own directory name is admitted
        beside them because the guide legitimately names a sibling skill, and that is a
        different namespace rather than an exemption from this one.
        """
        engine, driver = self.modules["engine"], self.modules["driver"]
        return (set(driver._SUBCOMMANDS) | set(driver.OUTCOMES) | set(driver.ROUNDS)
                | {driver.ADJUDICATED, driver.RESOLVED, driver.DECIDED,
                   driver.ORPHAN_CLAIM, driver.RUNNING, driver.UNCERTAIN}
                | {engine.UNIT_COMPLETE, engine.UNIT_FAILED, engine.UNIT_MISSING}
                | {getattr(engine, name) for name in self.STAGE_CONSTANTS}
                | set(engine._MOCK_DIALECTS)
                | {p.name for p in SKILLS.iterdir() if p.is_dir()})

    # ---- the direction that catches a name the guide INVENTS --------------------
    # The checks above ask whether a value the code has appears in the guide, which cannot
    # fail on a name the code never had: replacing every `plan` with `plna` left them all
    # green. These three ask the opposite question, of whatever the guide happens to quote,
    # so they hold without anyone extending a list.

    def test_every_private_name_the_guide_quotes_resolves(self):
        unresolved = [token for token in sorted(self.quoted)
                      if re.fullmatch(r"_[A-Za-z][A-Za-z0-9_]*", token)
                      and token not in self._markers()
                      and not any(hasattr(module, token)
                                  for module in self.modules.values())]
        self.assertEqual(unresolved, [],
                         "the guide names private symbols that neither program defines")

    def test_every_constant_name_the_guide_quotes_resolves(self):
        """The same shape for the public constants — `ROUNDS`, `UNIT_MISSING`, the rest.

        Two characters is enough to be one: at a three-character floor an invented `OK`
        passed, and no real constant is helped by the extra letter.
        """
        unresolved = [token for token in sorted(self.quoted)
                      if re.fullmatch(r"[A-Z][A-Z0-9_]+", token)
                      and token not in self._markers()
                      and not any(hasattr(module, token)
                                  for module in self.modules.values())]
        self.assertEqual(unresolved, [],
                         "the guide names constants that neither program defines")

    def test_every_run_value_the_guide_quotes_is_one_the_run_can_take(self):
        """A hyphenated lowercase token is a value out of the run's own vocabulary — a
        disposition, an attempt state, a driver verb. Nothing else in the guide is spelled
        that way, so membership can be required rather than mere presence."""
        invented = [token for token in sorted(self.quoted)
                    if re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+", token)
                    and token not in self._vocabulary()]
        self.assertEqual(invented, [],
                         "the guide quotes run values the two programs never produce")

    def test_every_plain_word_the_guide_quotes_occurs_in_the_code(self):
        """The catch-all under the two above: a bare lowercase word in backticks — a verb, a
        stage, a field, a dialect key — must appear as a whole word in one of the two
        programs. Weaker than membership, because this shape also covers JSON keys and field
        names that no enumeration holds; strong enough for the thing it is here for, which is
        a name the guide invented and the code has never had."""
        source = "\n".join(
            (SKILLS / "review-panel" / name).read_text(encoding="utf-8")
            for name in ("review_panel.py", "review_panel_run.py"))
        invented = [token for token in sorted(self.quoted)
                    if re.fullmatch(r"[a-z][a-z0-9]*", token)
                    and token not in self._markers()
                    and not re.search(rf"\b{re.escape(token)}\b", source)]
        self.assertEqual(invented, [],
                         "the guide quotes lowercase names that appear nowhere in either "
                         "program, so they are the guide's invention")

    def _unnamed(self, values):
        """The ones the guide does not quote. A list, so a failure prints what is missing
        rather than dumping every name the guide does quote — which `assertIn` against a
        set of two hundred does, and nobody reads."""
        return sorted(v for v in values if v not in self.quoted)

    def test_the_name_rule_markers_the_guide_lists_are_the_markers_there_are(self):
        """The guide spells the marker list out, in the engine's own order. A marker added
        to either tuple and not to the guide makes the name rule read as narrower than it
        is, which is exactly the kind of thing a maintainer would trust."""
        engine = self.modules["engine"]
        missing = self._unnamed(engine._SUBJECT_MARKERS_TRAILING
                                + engine._SUBJECT_MARKERS_LEADING)
        self.assertEqual(missing, [],
                         "these strip a test's name and the guide never names them")

    def test_the_dialect_fields_the_guide_counts_are_the_fields_there_are(self):
        """The guide tells a maintainer to fill "the seven fields of `_MockDialect`" and
        then names them. An eighth field added to the dataclass leaves that procedure
        incomplete with nothing else to notice."""
        engine = self.modules["engine"]
        fields = [f.name for f in dataclasses.fields(engine._MockDialect)]
        self.assertIn(f"{self.NUMBER_WORDS[len(fields)]} fields of `_MockDialect`",
                      self.text, "the guide counts the dialect's fields wrongly")
        self.assertEqual(self._unnamed(fields), [],
                         "these are dialect fields the guide never names")

    def test_the_attempt_and_disposition_vocabularies_are_complete(self):
        driver = self.modules["driver"]
        states = (driver.ADJUDICATED, driver.RESOLVED, driver.DECIDED,
                  driver.ORPHAN_CLAIM, driver.RUNNING, driver.UNCERTAIN)
        self.assertEqual(self._unnamed(states + tuple(driver.OUTCOMES)), [],
                         "the guide's attempt vocabulary omits these")

    # ---- the enumerations, each bound to the namespace it claims --------------------
    # Asking whether a word occurs SOMEWHERE in the source is too weak for a list that
    # claims to BE a namespace: a stage called `routing`, a command called `preview` and a
    # dialect key `ruby` all passed that way, because each word is in the source doing some
    # other job. Each check below pins its list to the real namespace instead, and fails
    # first if its anchor sentence has gone — a span that matches nothing must not read as
    # a list with nothing wrong in it.

    def _span(self, pattern):
        found = re.search(pattern, self.text, re.DOTALL)
        self.assertIsNotNone(
            found, f"the guide no longer carries the sentence this check reads "
                   f"({pattern!r}); it cannot be checked, so it fails rather than passing")
        span = found.group(1)
        tokens = set(re.findall(r"`([^`\n]+)`", span))
        self.assertTrue(tokens, f"no quoted names in the span matched by {pattern!r}")
        return tokens

    NUMBER_WORDS = {4: "four", 6: "six", 7: "seven", 8: "Eight"}

    def test_the_stage_table_lists_stages_and_only_stages(self):
        """Every marker in the table's first column is one `units.json` can hold.

        Scoped to `units.json` deliberately. The table is about what the loop does next,
        and the loop reads that file; a row for a marker written somewhere else describes
        a step the run never takes.
        """
        engine = self.modules["engine"]
        stages = {getattr(engine, name) for name in self.UNITS_STAGE_CONSTANTS}
        rows = re.findall(r"(?m)^\|\s*(`[^|]+?)\s*\|", self.text)
        self.assertGreaterEqual(len(rows), len(stages) - 1,
                                "the stage table has lost rows, or stopped being a table")
        listed = {t for row in rows for t in re.findall(r"`([^`]+)`", row)}
        self.assertEqual(listed - stages, set(),
                         "the stage table's first column names markers `units.json` never "
                         "holds")
        self.assertEqual(stages - listed, set(),
                         "`units.json` can hold markers the stage table omits")

    def test_the_type_tag_is_explained_and_is_not_a_table_row(self):
        """`routed` is a real run value that is NOT a stage, and the guide has to say so.

        Without this, the two obvious repairs are both wrong: dropping the constant from
        the vocabulary makes a value the engine really writes unquotable, and leaving it in
        the stage table puts a step in the guide that the loop never passes through. The
        one correct answer -- name it, and say what it is instead -- is the one nothing
        would otherwise hold.
        """
        engine = self.modules["engine"]
        tag = engine.ROUTED_STAGE
        rows = re.findall(r"(?m)^\|\s*(`[^|]+?)\s*\|", self.text)
        listed = {t for row in rows for t in re.findall(r"`([^`]+)`", row)}
        self.assertNotIn(tag, listed,
                         f"`{tag}` is in the stage table; it is written to "
                         f"{engine.CANDIDATES_FILE_NAME}, never to "
                         f"{engine.UNITS_FILE_NAME}, so no round is keyed on it")
        self.assertIn(tag, self.text,
                      f"the guide never mentions `{tag}`, so a reader meeting it in a run "
                      "directory has nothing to read")
        # Paragraph-scoped rather than a distance in characters, and in either order: the
        # explanation can open with the name or arrive at it, and a test that pins which
        # fails on a rewrite that changed nothing a reader would notice.
        said = [p for p in self.text.split("\n\n") if tag in p and "type tag" in p]
        self.assertTrue(said,
                        f"the guide names `{tag}` but no paragraph says it is a type tag "
                        "rather than a stage, which is the whole of what a reader needs")

    def test_the_driver_verb_list_is_the_parsers_subcommands(self):
        driver = self.modules["driver"]
        listed = self._span(r"command surface is \w+ verbs — (.*?) — and")
        self.assertEqual(listed, set(driver._SUBCOMMANDS),
                         "the guide's list of driver verbs is not the driver's verbs")
        self.assertIn(f"is {self.NUMBER_WORDS[len(driver._SUBCOMMANDS)]} verbs", self.text,
                      "the guide counts the driver's verbs wrongly")

    def test_the_engine_subcommand_list_is_the_parsers_subcommands(self):
        """Read off `build_parser`, so a subcommand added or renamed moves this."""
        engine = self.modules["engine"]
        subs = {a for action in engine.build_parser()._actions
                if isinstance(action, argparse._SubParsersAction)
                for a in action.choices}
        listed = self._span(r"it never spawns a worker\*\*: (.*?) for parsing one reply")
        self.assertEqual(listed, subs,
                         "the guide's list of engine subcommands is not the engine's")

    def test_the_dialect_key_list_is_the_tables_keys(self):
        engine = self.modules["engine"]
        listed = self._span(r"keys map to \w+ dialects today: (.*?)\n\n")
        self.assertEqual(listed, set(engine._MOCK_DIALECTS),
                         "the guide claims dialect keys the table does not have, or omits "
                         "keys it does")
        distinct = len({id(d) for d in engine._MOCK_DIALECTS.values()})
        self.assertIn(f"{self.NUMBER_WORDS[len(engine._MOCK_DIALECTS)]} keys map to "
                      f"{self.NUMBER_WORDS[distinct]} dialects", self.text,
                      "the guide counts the dialect table wrongly")

    def test_the_skill_sends_a_maintainer_here_exactly_once(self):
        """One pointer and no more. A dispatcher does not read this guide and must not be
        sent into it mid-run, so the sentence says who it is for and stays out of the steps."""
        skill = (SKILLS / "review-panel" / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(skill.count("references/architecture.md"), 1,
                         "SKILL.md points at the maintainer's guide more than once")


if __name__ == "__main__":
    unittest.main()
