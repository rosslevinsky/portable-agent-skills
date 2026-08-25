#!/usr/bin/env python3
"""Assertions about what the shipped skill text tells a runtime to *do*.

The `test_validate_*.md` fixtures beside this file exercise rules inside
`validate_cross_runtime.py`; these are the other thing — facts about the skills themselves.
"This skill pushes to the default branch" has no validator rule behind it and never will.
**"Runtime documents"** means every `*.md` under `skills/` except `DECISIONS.md`: a ledger
recording *why* a rule was dropped must be free to quote the dropped rule.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS = REPO_ROOT / "skills"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import validate_cross_runtime as vcr  # noqa: E402

# The skills that push on the user's behalf at a phase boundary, unattended. `commit` also
# pushes and is deliberately NOT here: it publishes because someone asked it to, in a
# conversation, and whether that ask has to be explicit is a separate question about that
# skill. The hazard these two carry is the one an unattended run walks into — a push nobody
# was watching, to whatever branch happened to be checked out.
PUSHING_EXECUTORS = ("plan-run", "plan-run-v1")

# A guard that derives the default branch rather than assuming `main`, plus the literal
# fallbacks for a repository whose `origin/HEAD` is not set. This is the shape
# `plan-run-v1/SKILL.md` already ships; naming it here is what makes "port it" checkable.
_DEFAULT_BRANCH_DERIVATION = "symbolic-ref"

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
# a negator that broad would suppress the very offence this looks for.
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
            "default branch (`git symbolic-ref --quiet --short refs/remotes/origin/HEAD`) "
            "and compares the current branch to it, falling back to `main`/`master` where "
            "`origin/HEAD` is unset — then skips the push and says so, rather than "
            "failing:\n  " + "\n  ".join(offences))

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

        `guards_*` used to read the whole block, so a snippet could derive the default
        branch, mention `main`, bind `HEAD` and then push unconditionally — every marker in
        place, no guard anywhere. The predicates read the conditions *enclosing the push*.
        """
        code = shell_code(
            'default="$(git symbolic-ref --short refs/remotes/origin/HEAD)"\n'
            'echo main\n'
            'branch=HEAD\n'
            'git push origin HEAD\n')
        (_, denied, own), = enclosing_conditions(code)
        self.assertEqual((denied, own.strip()), ("", ""))
        self.assertFalse(guards_default_branch(denied, own, code))
        self.assertFalse(guards_detached_head(denied, own, code))

    def test_a_conditional_that_does_not_test_the_branch_is_not_a_guard(self):
        code = ('default="$(git symbolic-ref --short refs/remotes/origin/HEAD)"\n'
                'if [ -n "$default" ]; then\n  git push origin HEAD\nfi\n')
        (_, denied, own), = enclosing_conditions(code)
        self.assertFalse(guards_default_branch(denied, own, code))

    def test_the_guard_shape_plan_run_v1_already_ships_is_recognised(self):
        """The predicates must accept the fix, or phase 2 has nothing it can write."""
        code = shell_code(
            'branch="$(git rev-parse --abbrev-ref HEAD)"\n'
            'default="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD '
            "2>/dev/null | sed 's#^origin/##')\"\n"
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
                'default="$(git symbolic-ref --short refs/remotes/origin/HEAD)"\n'
                'if [ "$branch" = "HEAD" ] || [ "$branch" = "main" ]; then :; fi\n'
                'git push origin HEAD\n')
        (_, denied, own), = enclosing_conditions(code)
        self.assertEqual((denied, own.strip()), ("", ""))
        self.assertFalse(guards_default_branch(denied, own, code))
        self.assertFalse(guards_detached_head(denied, own, code))

    def test_a_push_on_the_same_line_as_then_is_still_seen_as_guarded(self):
        """The inequality governing the push directly — the other correct shape."""
        code = ('branch="$(git rev-parse --abbrev-ref HEAD)"\n'
                'default="$(git symbolic-ref --short refs/remotes/origin/HEAD)"\n'
                'if [ "$branch" != "main" ]; then git push origin HEAD; fi\n')
        (_, denied, own), = enclosing_conditions(code)
        self.assertTrue(guards_default_branch(denied, own, code))

    def test_a_comparison_against_something_other_than_the_branch_is_not_a_guard(self):
        """`[ "$mode" = "main" ]` reads like a guard and tests nothing about the branch."""
        code = ('branch="$(git rev-parse --abbrev-ref HEAD)"\n'
                'default="$(git symbolic-ref --short refs/remotes/origin/HEAD)"\n'
                'if [ "$mode" = "main" ]; then :; else git push origin HEAD; fi\n')
        (_, denied, own), = enclosing_conditions(code)
        self.assertFalse(guards_default_branch(denied, own, code))

    def test_the_accepted_guard_shapes_are_these_and_only_these(self):
        """What satisfies these assertions, written down so a future author can satisfy it.

        A `case`/`esac` guard, a guard inside a shell function, and a condition wrapped across
        lines are **not** recognised — a recorded limitation: each errs toward reporting a
        push as *unguarded*, so the failure mode is a red suite over correct prose, never a
        green suite over a push to the trunk.
        """
        derive = ('branch="$(git rev-parse --abbrev-ref HEAD)"\n'
                  'default="$(git symbolic-ref --short refs/remotes/origin/HEAD)"\n')
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
                        'default="$(git symbolic-ref --short refs/remotes/origin/HEAD)"\n'
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

        It used to read this repository and name `.claude/settings.json`, which is a fact
        about one checkout rather than about the code: a repository without that directory
        fails the assertion on its first run for a reason unrelated to the behaviour under
        test. A fixture states the behaviour where the behaviour lives.
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

    # Files the old sweep did not reach. Each is a real shipped path, not a fixture.
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
                    f"{path} is a home directory and was not recognised as one")
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
        enumerate a dangling link, so the test failed there while the behaviour it checks is
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


if __name__ == "__main__":
    unittest.main()
