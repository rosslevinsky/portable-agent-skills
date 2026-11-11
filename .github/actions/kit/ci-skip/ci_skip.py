#!/usr/bin/env python3
"""Has this exact tree of files already passed a run of this workflow?

A push to main is usually a merge, and the pull request behind it tested the same files minutes
earlier: the commit is new, the tree is not. The claim needs three things: a candidate commit
with the same tree, a successful run of the workflow at it, and that run's own record (a receipt
artifact named RECEIPT-<tree>) that it checked out this tree. A pull request run tests the merge
of head and base, not the head, so a green run at the head proves this tree only if its receipt
names it.

The candidates are a merge commit's second parent and the head of each pull request this commit
closed, which covers merge and squash merges. Anything that fails to answer counts as "not
proved", and the tests run. Only a bad input or a missing checkout fails the step, and even then
the answer written is "already=false".

Runs in a checkout of the pushed commit with at least its parents fetched. Reads WORKFLOW (the
workflow's file name), RECEIPT (the receipt name, without the tree), GITHUB_EVENT_NAME,
GITHUB_REPOSITORY, GITHUB_SHA, GITHUB_OUTPUT and GITHUB_STEP_SUMMARY; gh reads GH_TOKEN.
Standard library only.
"""

import os
import re
import subprocess
import sys

SHA = re.compile(r"[0-9a-f]{40}")
WORKFLOW = re.compile(r"[A-Za-z0-9._-]+\.ya?ml")
RECEIPT = re.compile(r"[A-Za-z0-9._-]+")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
RUNS_JQ = '.workflow_runs[] | "\\(.id) \\(.head_repository.full_name)"'


class BadInput(Exception):
    pass


def answer(already):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as handle:
            handle.write(f"already={'true' if already else 'false'}\n")


def summary(line):
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as handle:
            handle.write(line + "\n")


def gh(*args):
    """gh's stdout, or None when it fails or isn't there. Its errors are not shown."""
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def read_input(name, pattern):
    value = os.environ.get(name, "")
    if not pattern.fullmatch(value):
        raise BadInput(f"{name} is missing or malformed: {value!r}")
    return value


def proved(repo, sha, workflow, receipt):
    tree = git("rev-parse", "HEAD^{tree}")
    if tree is None or not SHA.fullmatch(tree):
        raise BadInput("no checkout here: HEAD^{tree} can't be read")
    print(f"this tree: {tree}")
    api = f"repos/{repo}"
    second = git("rev-parse", "--verify", "--quiet", "HEAD^2")
    heads = gh("api", f"{api}/commits/{sha}/pulls", "--jq", ".[].head.sha") or ""
    candidates = ([second] if second else []) + heads.split()
    for candidate in candidates:
        if not SHA.fullmatch(candidate):
            print(f"{candidate!r}: not a commit SHA; not asked about")
            continue
        candidate_tree = gh("api", f"{api}/commits/{candidate}", "--jq", ".commit.tree.sha")
        if candidate_tree is None:
            continue
        candidate_tree = candidate_tree.strip()
        if candidate_tree != tree:
            print(f"{candidate}: different tree ({candidate_tree})")
            continue
        # A fork's pull request runs here too, but from the fork's own branch, whose workflow
        # file can upload a receipt without testing anything. Only this repository's runs count.
        runs = gh("api",
                  f"{api}/actions/workflows/{workflow}/runs"
                  f"?head_sha={candidate}&status=success&per_page=100",
                  "--jq", RUNS_JQ)
        if runs is None:
            continue
        run = next((line.split(" ", 1)[0] for line in runs.splitlines()
                    if line.split(" ", 1)[1:] == [repo] and line.split(" ", 1)[0].isdigit()), "")
        if not run:
            print(f"{candidate}: same tree, no green run of {workflow}")
            continue
        names = gh("api", "--paginate", f"{api}/actions/runs/{run}/artifacts",
                   "--jq", ".artifacts[].name") or ""
        if f"{receipt}-{tree}" in names.splitlines():
            summary(f"{candidate}: run {run} checked out this exact tree and was green")
            return True
        print(f"{candidate}: green run {run} carries no {receipt}-{tree} receipt")
    summary("no commit with this tree has a green run; the tests run")
    return False


def main():
    # On a pull request the merge tree can equal the head while the head is exactly what hasn't
    # been tested, so only a push is ever answered yes.
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event != "push":
        answer(False)
        summary(f"the event is {event or 'unknown'}, not a push; the tests run")
        return 0
    try:
        workflow = read_input("WORKFLOW", WORKFLOW)
        receipt = read_input("RECEIPT", RECEIPT)
        repo = read_input("GITHUB_REPOSITORY", REPOSITORY)
        sha = read_input("GITHUB_SHA", SHA)
        already = proved(repo, sha, workflow, receipt)
    except BadInput as error:
        answer(False)
        print(f"ci-skip: {error}", file=sys.stderr)
        return 2
    answer(already)
    return 0


if __name__ == "__main__":
    sys.exit(main())
