"""Deterministic tests for the review-panel driver, ``review_panel_run.py``.

Grouped by the property each group establishes, because that is what these are for: a
report that comes out byte-identical proves none of them. Ownership, one active attempt per
unit, one selected terminal publication, replay-stable budgets, extraction, and completion.

**The worker is a stub CLI, and it has to be a program rather than a canned object.** A
verification or a clustering reply is checked against the ids that unit was actually handed,
so an answer written in advance is refused by the engine for being about other candidates.
The stub reads its payload, works out which kind of unit it is from the schema it was given,
and answers per id — which is also why it exercises the real landing path end to end.

The driver lives at ``skills/review-panel/review_panel_run.py``. That directory name has a
hyphen, so it is not importable as a package; the directory goes on ``sys.path`` and the
modules are imported by their own names, the way the engine suite reaches ``review_panel``.
"""

import contextlib
import errno
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "review-panel"
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

import review_panel  # noqa: E402
import review_panel_run as driver  # noqa: E402

_DRIVER = _SKILL_DIR / "review_panel_run.py"
_SUPERVISOR = _SKILL_DIR.parent / "diff-review" / "review_runner.py"
_FIXTURE_TREE = Path(__file__).resolve().parent / "fixtures" / "review-panel" / "tree"

# The stub worker. Written into each case's temporary directory rather than tracked as a
# fixture, so it sits beside the tests that drive it and no run can pick up a stale copy.
STUB = r'''#!/usr/bin/env python3
"""A stub worker: answers one review-panel unit from its payload and its schema."""
import json
import pathlib
import re
import sys
import time

args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
payload = pathlib.Path(args["--payload"]).read_text(encoding="utf-8")
schema = json.loads(pathlib.Path(args["--schema"]).read_text(encoding="utf-8"))
transcript = pathlib.Path(args["--transcript"])
control_path = pathlib.Path(args["--control"])
try:
    control = json.loads(control_path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    control = {}

counter = control_path.with_name("stub-count.txt")
with open(counter, "a", encoding="utf-8") as fh:
    fh.write("x\n")
n = len(counter.read_text(encoding="utf-8").splitlines())

if control.get("sleep"):
    time.sleep(float(control["sleep"]))
if control.get("outage"):
    # A provider refusing, as one reaches the supervisor: a terminal failure event on the
    # stream carrying the provider's own wording, and an exit status that says nothing.
    print(json.dumps({"type": "turn.failed",
                      "error": {"message": control["outage"]}}), flush=True)
    sys.exit(0)
if n <= int(control.get("exit_nonzero_until", 0)):
    sys.stderr.write("the stub was told to fail\n")
    sys.exit(1)


def files_in_payload():
    match = re.search(r"relative to the snapshot[^\n]*:\n\n((?:- .*\n)+)", payload)
    return [line[2:].strip() for line in match.group(1).splitlines()] if match else []


def section(heading):
    """The payload text under one top-level heading, to the next one or the end.

    Scoped rather than searched whole: a brief that shows `cand-007` as an EXAMPLE is not a
    candidate this unit was handed, and answering for it is refused by the engine.
    """
    start = payload.find(heading)
    if start < 0:
        return ""
    start += len(heading)
    end = payload.find("\n## ", start)
    return payload[start:] if end < 0 else payload[start:end]


def ids(pattern, heading):
    seen, out = set(), []
    for found in re.findall(pattern, section(heading)):
        if found not in seen:
            seen.add(found)
            out.append(found)
    return out


props = set(schema.get("properties", {}))
if props >= {"build", "tests"}:
    nothing = {"answer": "unknown", "argv": [], "cwd": ".", "exit_status": None,
               "output": "", "truncated": False}
    answer = {"build": nothing, "tests": nothing,
              "summary": "Nothing here says how to build it or how to run its tests."}
elif props >= {"findings"}:
    listed = files_in_payload()
    findings = []
    if listed and "Test inventory" not in payload and not control.get("no_findings"):
        target = listed[0]
        first = pathlib.Path(target).read_text(encoding="utf-8").splitlines()[0]
        findings.append({
            "file": target, "line_start": 1, "line_end": 1, "severity": "major",
            "consequence": "A caller reading this line is misled about what it does.",
            "failure": "The first line does not describe what the file actually does.",
            "direction": "Say what the file does.", "fix_size": "small",
            "quote": first, "reproduction": None,
        })
    answer = {"findings": findings, "summary": "read the area"}
elif props >= {"verdicts"}:
    answer = {"verdicts": [{
        "candidate": cid, "status": "confirmed_by_reading", "evidence": None,
        "rationale": "The lines cited do say what the candidate reports.",
        "revision": None,
        "test_first": "tests/test_engine.py: assert the line says what it does.",
        "unresolved_reason": None,
    } for cid in ids(r"cand-\d+", "\n## Candidates\n")],
        "summary": "checked every candidate by reading"}
elif props >= {"clusters"}:
    answer = {"clusters": [{
        "members": [cid],
        "consequence": "A reader of this file is misled about what it does.",
        "split_reason": None,
    } for cid in ids(r"cand-\d+", "\n## Candidates to group\n")],
        "summary": "one defect per candidate"}
else:
    tier = "A reader is misled"
    answer = {"tiers": [tier], "defects": [{
        "defect": did, "tier": tier,
        "what_goes_wrong": "The opening line of the file describes something else.",
        "fix": "Rewrite the opening line to say what the file does.",
        "cross_references": [],
    } for did in ids(r"\bD\d+\b", "\n## Your defects\n")], "summary": "one tier"}

# Refusing by KIND rather than by count, so which units come back unusable does not depend
# on the order two slots happened to be launched in. The schema is what says which kind this
# unit is, and a reply the engine refuses is charged to the unit rather than to the provider.
mode = control.get("mode")
if set(schema.get("properties", {})) & set(control.get("refuse_for", ())):
    mode = "refused"

text = json.dumps(answer, indent=2)
if mode == "no-object":
    body = "I could not answer this one.\n"
elif mode == "trailing":
    body = "Here it is:\n\n" + text + "\n\nand a closing remark.\n"
elif mode == "refused":
    body = json.dumps({"findings": "not a list", "summary": "x"}, indent=2)
else:
    body = "Working on it.\n\n" + text + "\n"
transcript.write_text(body, encoding="utf-8")
'''


def _stub_mode(stub: Path, control: Path, deadline: float = 60.0,
               result_mode: str = "external-file") -> dict:
    """One slot's command. ``deadline`` is the supervisor's bound on a single attempt, and
    it is the lever a test reaches for when it wants an attempt that cannot report to be
    classified quickly — see `test_a_killed_driver_resumes_with_no_unit_run_twice_and_none_lost`.

    ``result_mode`` is the other: a provider's refusal reaches the supervisor as a terminal
    event on the stream, which only the streaming modes read."""
    return {"command": [sys.executable, str(stub), "--payload", "⟪payload⟫",
                        "--schema", "⟪schema⟫", "--transcript", "⟪transcript⟫",
                        "--control", str(control)],
            "permission": "none, it is a stub", "result_mode": result_mode,
            "idle": min(30.0, deadline), "deadline": deadline}


def _lay_out(tmp: Path) -> dict:
    """The tree, the stub, the job and the adapter config one case works against."""
    root = tmp / "root"
    shutil.copytree(_FIXTURE_TREE, root)
    stub = tmp / "stub.py"
    stub.write_text(STUB, encoding="utf-8")
    control = tmp / "control.json"
    control.write_text("{}", encoding="utf-8")
    job_path = tmp / "job.json"
    job_path.write_text(json.dumps({
        "problem": "Find every place the installer can leave a half-written file.",
        "root": str(root), "exclude": [], "partition": "file",
        "lenses": ["bottom-up from the code", "top-down from the contract"],
    }), encoding="utf-8")
    return {"tmp": tmp, "root": root, "rundir": tmp / "run", "stub": stub,
            "control": control, "job_path": job_path,
            "adapter_path": tmp / "adapter.json"}


def _discard(tmp: Path) -> None:
    """Remove a case's temporary tree, **including a hardened snapshot**.

    A committed run directory has a snapshot nothing can write to, and on POSIX that is a
    tree an ordinary recursive delete cannot remove an entry from — so a plain
    `rmtree(ignore_errors=True)` leaves a copy of the fixture tree in the temporary
    directory for every case that planned a run. The driver's own remover restores write
    permission first, which is the same thing an operator does by hand.
    """
    driver.remove_tree(tmp, tmp.parent)


class _Case(unittest.TestCase):
    """A temporary tree, a stub worker, an adapter config and a run directory path."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="rp-driver-"))
        self.addCleanup(_discard, tmp)
        self.__dict__.update(_lay_out(tmp))
        self.write_adapter()

    # -- fixtures ----------------------------------------------------------- #
    def write_adapter(self, deadline=60.0, result_mode="external-file", **over):
        mode = _stub_mode(self.stub, self.control, deadline, result_mode)
        slots = {slot: {"runtime": f"stub-{slot}", "model": f"stub-model-{slot}",
                        "adapter": f"stub slot {slot}", "read_only": mode,
                        "write_capable": mode} for slot in review_panel.SLOTS}
        for slot, extra in over.items():
            slots[slot].update(extra)
        self.adapter_path.write_text(json.dumps({"slots": slots}), encoding="utf-8")

    def write_control(self, **kw):
        self.control.write_text(json.dumps(kw), encoding="utf-8")

    def failing_slot(self, **control):
        """One slot's command, pointed at a control file **of its own**.

        Its own, because the stub counts its launches beside its control file: sharing one
        with the working slot would count both slots' launches together and make which unit
        died depend on the order the two were launched in. With no argument every reply
        comes back unusable, which is the shape that spends a unit's allowances on the unit
        rather than pausing the provider — a launch that exits non-zero instead trips the
        breaker, which is a pause and a different ending.
        """
        where = self.tmp / "failing-slot"
        where.mkdir(exist_ok=True)
        path = where / "control.json"
        path.write_text(json.dumps(control or {"mode": "refused"}), encoding="utf-8")
        return _stub_mode(self.stub, path)

    def stub_invocations(self):
        counter = self.tmp / "stub-count.txt"
        return len(counter.read_text(encoding="utf-8").splitlines()) if counter.exists() else 0

    # -- driving ------------------------------------------------------------ #
    def drive(self, *extra, expect=0, timeout=300):
        """Run the driver as a child, decoding its output as UTF-8 explicitly.

        Explicitly, because `text=True` decodes with the PARENT's preferred encoding — ASCII
        under the C locale this suite is also run in — and every message the driver prints
        can carry an em dash. A child rather than in-process because the signal
        and the ownership properties are about a process, and because the tests that kill
        it have to kill something."""
        proc = subprocess.run(
            [sys.executable, str(_DRIVER), "--job", str(self.job_path),
             "--rundir", str(self.rundir), "--adapter", str(self.adapter_path),
             "--poll", "0.1", "--capacity", "2", *extra],
            capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
        if expect is not None:
            self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def launch(self, *extra):
        """A driver as a long-lived child. The caller kills it; the pipe is closed here so a
        killed child leaves no unclosed handle behind it."""
        proc = subprocess.Popen(
            [sys.executable, str(_DRIVER), "--job", str(self.job_path),
             "--rundir", str(self.rundir), "--adapter", str(self.adapter_path),
             "--poll", "0.1", "--capacity", "2", *extra],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace")
        self.addCleanup(self._reap, proc)
        return proc

    @staticmethod
    def _reap(proc):
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=30)
        if proc.stdout is not None:
            with contextlib.suppress(OSError):
                proc.stdout.close()

    def plan_only(self):
        self.drive()
        self.assertTrue((self.rundir / "units.json").is_file())

    def run_object(self, **kw):
        """A :class:`Run` over this case's run directory, writing nowhere anyone reads."""
        import io
        return driver.Run(self.rundir, driver.load_adapter_config(self.adapter_path),
                          _SUPERVISOR, out=io.StringIO(), **kw)

    def unaccountable(self, grace=driver.GRACE_DEFAULT):
        """Every attempt whose outcome nobody can prove, as ``(unit, attempt)`` pairs."""
        found = []
        doc = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        for unit in doc["units"]:
            for attempt in driver.read_attempts(self.rundir, unit["id"], grace=grace):
                if attempt.state in (driver.UNCERTAIN, driver.ORPHAN_CLAIM):
                    found.append((unit["id"], attempt.name))
        return found

    # -- hand-built attempts ------------------------------------------------- #
    def attempt(self, unit, index, *, argv=True, status=None, disposition=None,
                resolution=None, spawn_time=None, deadline=60.0, transcript=None):
        path = self.rundir / "dispatch" / unit / f"a{index}"
        path.mkdir(parents=True, exist_ok=True)
        if argv:
            record = {"argv": ["x"], "adapter": "stub", "slot": "A", "account": "stub",
                      "generation": 0, "permission": "none", "cwd": str(self.rundir),
                      "kind": "reader", "probe": False,
                      "spawn_time": time.time() if spawn_time is None else spawn_time,
                      "deadline": deadline, "token": f"tok-{unit}-{index}",
                      "transcript": str(path / "transcript.md"), "unit": unit,
                      "attempt": f"a{index}"}
            if argv is not True:
                record.update(argv)
            (path / "argv.json").write_text(json.dumps(record), encoding="utf-8")
        if status is not None:
            (path / "status.txt").write_text(status, encoding="utf-8")
        if transcript is not None:
            (path / "transcript.md").write_text(transcript, encoding="utf-8")
        if disposition is not None:
            (path / "disposition.json").write_text(json.dumps(disposition), encoding="utf-8")
        if resolution is not None:
            (path / "resolution.json").write_text(json.dumps(resolution), encoding="utf-8")
        return path

    def fake_snapshot(self, **files):
        """A snapshot and the inventory that measured it, for a hand-built run directory.

        The driver checks the snapshot against `inventory.json` at startup and at every
        round boundary, so a directory that reaches the loop needs both — and a hand-built
        one that carried neither would be exercising a path no real run takes.
        """
        snapshot = self.rundir / "snapshot"
        snapshot.mkdir(parents=True, exist_ok=True)
        entries = []
        for name, text in (files or {"a.py": "print('hi')\n"}).items():
            raw = text.encode("utf-8")
            (snapshot / name).write_bytes(raw)
            entries.append({"path": name, "bytes": len(raw), "lines": raw.count(b"\n"),
                            "sha256": hashlib.sha256(raw).hexdigest()})
        (self.rundir / "inventory.json").write_text(
            json.dumps({"files": entries}), encoding="utf-8")
        return snapshot

    def fake_units(self, *units, stage="reading"):
        """A run directory with a listing, and each unit's two inputs beside it.

        Enough for every question about eligibility, allowances and landing, none of which
        reads a payload — but a spawn copies the brief and the schema into the worker's
        opaque input directory and refuses to launch a worker without them, so a listing
        with no files under `units/` would be a run directory no plan can produce.
        """
        self.rundir.mkdir(parents=True, exist_ok=True)
        (self.rundir / "units.json").write_text(json.dumps({
            "stage": stage, "slots": list(review_panel.SLOTS),
            "units": [{"id": uid, "kind": "reader", "slot": slot, "area": "area-01",
                       "lens": "a lens"} for uid, slot in units],
        }), encoding="utf-8")
        for uid, _slot in units:
            unit_dir = self.rundir / "units" / uid
            unit_dir.mkdir(parents=True, exist_ok=True)
            (unit_dir / review_panel.PAYLOAD_NAME).write_text(
                f"# {uid}\n\nRead the area and answer.\n", encoding="utf-8")
            (unit_dir / review_panel.SCHEMA_NAME).write_text(
                json.dumps({"type": "object"}), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. ownership
# --------------------------------------------------------------------------- #
_HOLDER = r'''
import sys, time, subprocess
sys.path.insert(0, sys.argv[1])
import review_panel_run as driver
lock = driver.RunLock(__import__("pathlib").Path(sys.argv[2]))
lock.acquire()
child = None
if len(sys.argv) > 3 and sys.argv[3] == "spawn":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             close_fds=False)
    print(child.pid, flush=True)
print("held", flush=True)
time.sleep(60)
'''


class TheRunHasOneOwner(_Case):
    """One lock for the whole run, held for the driver's lifetime and released by the
    kernel. Nothing anywhere reads a timestamp to decide ownership, so these are the only
    questions there are: can a second driver take it, and is it free when the first dies."""

    def holder(self, *extra):
        script = self.tmp / "holder.py"
        script.write_text(_HOLDER, encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(script), str(_SKILL_DIR),
             str(self.rundir) + driver.LOCK_SUFFIX, *extra],
            stdout=subprocess.PIPE, encoding="utf-8", errors="replace")
        self.addCleanup(self._stop, proc)
        lines = []
        while "held" not in lines:
            line = proc.stdout.readline()
            if not line:
                self.fail("the holder never took the lock")
            lines.append(line.strip())
        return proc, lines

    @staticmethod
    def _stop(proc):
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        if proc.stdout is not None:
            with contextlib.suppress(OSError):
                proc.stdout.close()

    def test_a_second_driver_is_refused_while_the_first_lives(self):
        proc, _ = self.holder()
        with self.assertRaises(driver.DriverError) as ctx:
            driver.RunLock(Path(str(self.rundir) + driver.LOCK_SUFFIX)).acquire()
        self.assertIn("held by a live driver", str(ctx.exception))
        self.assertIn(str(proc.pid), str(ctx.exception))

    def test_the_lock_is_free_the_instant_the_holder_is_killed(self):
        proc, _ = self.holder()
        proc.kill()
        proc.wait(timeout=10)
        lock = driver.RunLock(Path(str(self.rundir) + driver.LOCK_SUFFIX))
        lock.acquire()          # no takeover command, no confirmation, no age heuristic
        lock.release()

    @unittest.skipUnless(os.name == "posix", "the descriptor inheritance rule is POSIX here")
    def test_a_surviving_worker_does_not_hold_the_run_lock(self):
        """The driver dies; its supervisors do not. The lock has to be free anyway, or a
        run could never be resumed while anything it started was still alive — which is
        exactly the state a kill during a round leaves."""
        proc, lines = self.holder("spawn")
        child_pid = int(lines[0])
        proc.kill()
        proc.wait(timeout=10)
        try:
            os.kill(child_pid, 0)   # the worker is still alive
        except OSError:
            self.skipTest("the spawned worker did not outlive its parent")
        lock = driver.RunLock(Path(str(self.rundir) + driver.LOCK_SUFFIX))
        lock.acquire()
        lock.release()
        with contextlib.suppress(OSError):
            os.kill(child_pid, signal.SIGKILL)

    def test_locking_that_does_not_exclude_is_refused_at_startup(self):
        """A filesystem where two acquisitions both succeed has no exclusion at all, and a
        driver that trusted it would run beside another one believing it was alone."""
        with mock.patch.object(driver.RunLock, "_take", staticmethod(lambda fd: True)):
            with self.assertRaises(driver.DriverError) as ctx:
                driver.RunLock(Path(str(self.rundir) + driver.LOCK_SUFFIX)).acquire()
        self.assertIn("can be locked twice", str(ctx.exception))

    def test_a_host_with_no_file_locking_is_refused_before_anything_is_inspected(self):
        with mock.patch.object(driver.RunLock, "_locking_available",
                               staticmethod(lambda: "there is no locking here")):
            with self.assertRaises(driver.DriverError) as ctx:
                driver.RunLock(Path(str(self.rundir) + driver.LOCK_SUFFIX)).acquire()
        self.assertIn("exclusive ownership", str(ctx.exception))
        self.assertFalse(self.rundir.exists())

    def test_a_second_driver_is_refused_while_the_first_is_inside_planning(self):
        """Bootstrap is the window draft 2 left unowned: the lock was taken AFTER planning,
        so two drivers could both see one `.partial` directory and one delete the other's
        live planning. Modelled as the state that window actually is — the lock held and an
        uncommitted partial on disk."""
        partial = Path(str(self.rundir) + driver.PARTIAL_SUFFIX)
        partial.mkdir(parents=True)
        (partial / "job.json").write_text("{}", encoding="utf-8")
        self.holder()
        proc = self.drive("--go", expect=driver.EXIT_REFUSED)
        self.assertIn("held by a live driver", proc.stderr)
        self.assertTrue((partial / "job.json").is_file(),
                        "a refused driver deleted the planning directory of a live one")

    def test_the_lock_is_held_across_an_engine_stage(self):
        """A stage is called in-process, so the run stays owned while it runs. A stage run
        as a child would keep writing a run directory this lock says nobody owns."""
        import io
        self.drive()
        seen = []
        real = review_panel.main

        def watching(argv):
            if argv and argv[0] == "route":
                # A second DRIVER, not `status`: status writes nothing and deliberately
                # takes no lock, so it would answer here and prove nothing about ownership.
                probe = subprocess.run(
                    [sys.executable, str(_DRIVER), "--job", str(self.job_path),
                     "--rundir", str(self.rundir), "--adapter", str(self.adapter_path),
                     "--go"],
                    capture_output=True, encoding="utf-8", errors="replace", timeout=120)
                seen.append(probe)
            return real(argv)

        run = driver.Run(self.rundir, driver.load_adapter_config(self.adapter_path),
                         _SUPERVISOR, out=io.StringIO(), poll=0.05, capacity=2)
        with driver.RunLock(Path(str(self.rundir) + driver.LOCK_SUFFIX)):
            with mock.patch.object(review_panel, "main", watching):
                run.loop()
        self.assertTrue(seen, "the route stage never ran, so this asserts nothing")
        self.assertNotEqual(seen[0].returncode, 0)
        self.assertIn("held by a live driver", seen[0].stderr)

    @unittest.skipUnless(os.name == "posix", "a directory symlink is a POSIX shape here")
    def test_two_spellings_of_one_run_directory_take_one_lock(self):
        """A run reached through a symlink and through its real path is one run. Named from
        the spelling, the sibling lock is two locks: both drivers proceed, both claim
        attempts for the same unit, and exclusive attempt-directory creation only decides
        which of them wins each race."""
        self.plan_only()
        alias = self.tmp / "alias"
        os.symlink(self.rundir, alias)
        held = driver.RunLock(Path(str(self.rundir) + driver.LOCK_SUFFIX))
        held.acquire()
        self.addCleanup(held.release)
        proc = subprocess.run(
            [sys.executable, str(_DRIVER), "--job", str(self.job_path),
             "--rundir", str(alias), "--adapter", str(self.adapter_path), "--go"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(proc.returncode, driver.EXIT_REFUSED, proc.stdout + proc.stderr)
        self.assertIn("held by a live driver", proc.stderr)
        self.assertFalse((self.tmp / "alias.lock").exists(),
                         "the alias took a lock of its own")

    def test_the_holder_can_be_identified_while_it_holds_the_lock(self):
        """The pid in that refusal is the whole value of the line: it is what tells an
        operator which process to stop. Windows byte-range locks are MANDATORY, so a
        diagnostic written over the locked byte cannot be read back by the second process
        and the message named nobody there while naming the pid here. The contents live
        past the locked byte, and this reads them the way the refusal does — through a
        second descriptor, while the lock is held."""
        self.assertGreaterEqual(
            driver.RunLock._CONTENTS_AT, driver.RunLock._LOCK_BYTES,
            "the lock file's contents sit inside the range the lock covers, which a second "
            "process cannot read where byte-range locks are mandatory")
        lock = driver.RunLock(Path(str(self.rundir) + driver.LOCK_SUFFIX))
        lock.acquire()
        self.addCleanup(lock.release)
        fd = os.open(lock.path, os.O_RDONLY)
        try:
            os.lseek(fd, driver.RunLock._CONTENTS_AT, os.SEEK_SET)
            held = os.read(fd, 4096).decode("utf-8", "replace").strip("\x00").strip()
        finally:
            os.close(fd)
        self.assertIn(str(os.getpid()), held, held)

    def test_a_run_directory_that_cannot_be_resolved_is_refused(self):
        with mock.patch.object(Path, "resolve",
                               side_effect=RuntimeError("symlink loop")):
            with self.assertRaises(driver.DriverError) as ctx:
                driver._canonical(self.rundir, "run directory")
        self.assertIn("cannot resolve", str(ctx.exception))

    def test_the_lock_file_is_a_sibling_and_is_never_deleted(self):
        """A sibling because the engine refuses a run directory holding anything it did not
        write; never deleted because a lock file that gets removed is a lock two processes
        can both hold, one of them through a name that no longer exists."""
        self.plan_only()
        lock = Path(str(self.rundir) + driver.LOCK_SUFFIX)
        self.assertTrue(lock.is_file())
        self.assertEqual(lock.parent, self.rundir.parent)
        self.drive()
        self.assertTrue(lock.is_file())


# --------------------------------------------------------------------------- #
# 2. bootstrap
# --------------------------------------------------------------------------- #
class BootstrapNeverDeletesWhatItDidNotExamine(_Case):

    def test_a_directory_holding_no_units_json_is_refused_by_name_and_kept(self):
        self.rundir.mkdir(parents=True)
        shutil.copyfile(self.job_path, self.rundir / "job.json")
        proc = self.drive(expect=driver.EXIT_REFUSED)
        self.assertIn("refused by name", proc.stderr)
        self.assertTrue((self.rundir / "job.json").is_file())

    def test_an_unrelated_directory_at_the_target_path_is_refused_and_kept(self):
        self.rundir.mkdir(parents=True)
        (self.rundir / "somebody.txt").write_text("mine\n", encoding="utf-8")
        self.drive(expect=driver.EXIT_REFUSED)
        self.assertEqual((self.rundir / "somebody.txt").read_text(encoding="utf-8"), "mine\n")

    def test_an_uncommitted_partial_is_removed_and_planning_runs_again(self):
        partial = Path(str(self.rundir) + driver.PARTIAL_SUFFIX)
        partial.mkdir(parents=True)
        (partial / "half-planned.txt").write_text("x", encoding="utf-8")
        self.plan_only()
        self.assertFalse(partial.exists())

    def test_a_committed_run_beside_a_partial_wins_and_the_partial_goes(self):
        self.plan_only()
        partial = Path(str(self.rundir) + driver.PARTIAL_SUFFIX)
        partial.mkdir()
        before = (self.rundir / "units.json").read_bytes()
        self.drive()
        self.assertFalse(partial.exists())
        self.assertEqual((self.rundir / "units.json").read_bytes(), before)

    def test_a_refused_plan_commits_nothing_at_the_target_path(self):
        """Planning happens in `.partial` and is committed by a rename, so a refusal leaves
        no run directory at all — rather than one another driver would resume from."""
        self.job_path.write_text(json.dumps({"problem": "x"}), encoding="utf-8")
        proc = self.drive(expect=driver.EXIT_REFUSED)
        self.assertIn("plan refused this job", proc.stderr)
        self.assertFalse(self.rundir.exists())
        self.assertFalse(Path(str(self.rundir) + driver.PARTIAL_SUFFIX).exists())

    def test_the_interviews_notes_are_copied_in_after_planning(self):
        """`job-notes.json` records which job fields nobody stated, and the report marks them
        from it. Copied AFTER `plan`, which refuses a run directory holding anything but the
        job being loaded — a notes file put there first would refuse the run.
        """
        notes = {"root": "defaulted", "partition": "defaulted"}
        self.job_path.with_name("job-notes.json").write_text(json.dumps(notes),
                                                             encoding="utf-8")
        self.plan_only()
        landed = self.rundir / "job-notes.json"
        self.assertTrue(landed.is_file(), "the notes file did not reach the run directory")
        self.assertEqual(json.loads(landed.read_text(encoding="utf-8")), notes)

    def test_no_notes_beside_the_job_is_not_a_failure(self):
        """A job handed over ready-made has none, which is the ordinary case."""
        self.assertFalse(self.job_path.with_name("job-notes.json").exists())
        self.plan_only()
        self.assertFalse((self.rundir / "job-notes.json").exists())

    def test_without_go_nothing_is_dispatched_and_stdin_is_never_read(self):
        proc = subprocess.run(
            [sys.executable, str(_DRIVER), "--job", str(self.job_path),
             "--rundir", str(self.rundir), "--adapter", str(self.adapter_path)],
            capture_output=True, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pass --go to run it", proc.stdout)
        self.assertEqual(self.stub_invocations(), 0)
        self.assertEqual(list(self.rundir.glob("dispatch/*/a*")), [],
                         "a run without --go claimed an attempt")


# --------------------------------------------------------------------------- #
# the adapter config
# --------------------------------------------------------------------------- #
class TheAdapterConfigIsData(_Case):
    """Every spawn's argv arrives as data. The driver names no product anywhere, so the
    only thing it can do about a command line is refuse one it could not run."""

    def parse(self, mutate):
        raw = json.loads(self.adapter_path.read_text(encoding="utf-8"))
        mutate(raw)
        with self.assertRaises(driver.DriverError) as ctx:
            driver.parse_adapter_config(raw)
        return str(ctx.exception)

    def test_a_command_carrying_no_task_is_refused(self):
        def mutate(raw):
            raw["slots"]["A"]["read_only"]["command"] = ["prog", "--schema", "⟪schema⟫"]
        self.assertIn("carries no task", self.parse(mutate))

    def test_a_marker_the_driver_never_fills_is_refused_naming_it(self):
        def mutate(raw):
            raw["slots"]["B"]["write_capable"]["command"] = ["prog", "⟪prompt⟫", "⟪nonsense⟫"]
        message = self.parse(mutate)
        self.assertIn("⟪nonsense⟫", message)

    def test_a_missing_slot_is_refused(self):
        self.assertIn("missing slot", self.parse(lambda raw: raw["slots"].pop("B")))

    def test_a_missing_permission_mode_is_refused(self):
        def mutate(raw):
            raw["slots"]["A"].pop("write_capable")
        self.assertIn("write_capable", self.parse(mutate))

    def test_a_resume_with_a_different_configuration_is_refused(self):
        self.plan_only()
        self.write_adapter(A={"model": "a different model"})
        proc = self.drive(expect=driver.EXIT_REFUSED)
        self.assertIn("pins a different adapter configuration", proc.stderr)

    def test_the_dispatch_record_states_both_permission_modes_and_the_model(self):
        slots = driver.load_adapter_config(self.adapter_path)
        record = driver.dispatch_record(slots)
        self.assertEqual(record["rung"], review_panel.RUNG_TWO_RUNTIMES)
        self.assertIn("stub-model-A", record["slots"]["A"]["adapter"])
        self.assertIn("read-only units", record["slots"]["A"]["permission"])
        self.assertIn("write-capable units", record["slots"]["A"]["permission"])
        review_panel.parse_dispatch(record)   # the engine's own strict parse

    def test_one_runtime_on_both_slots_is_a_one_runtime_rung(self):
        slots = driver.load_adapter_config(self.adapter_path)
        slots["B"] = type(slots["B"])(runtime=slots["A"].runtime, model=slots["A"].model,
                                      account=slots["A"].account, adapter="a second context",
                                      modes=slots["B"].modes)
        self.assertEqual(driver.dispatch_record(slots)["rung"],
                         review_panel.RUNG_ONE_RUNTIME)


# --------------------------------------------------------------------------- #
# 5.1 extraction
# --------------------------------------------------------------------------- #
class TheClosingObjectOrNothing(unittest.TestCase):
    """The object must be the last non-whitespace content of the transcript. Reaching
    further back is how an earlier EXAMPLE gets landed as a unit's answer — an object that
    parses perfectly and says something nobody claimed."""

    def test_an_object_at_the_end_is_taken(self):
        self.assertEqual(driver.closing_object('chatter\n\n{"a": 1}\n'), '{"a": 1}')

    def test_trailing_content_means_there_is_no_closing_object(self):
        self.assertIsNone(driver.closing_object('{"a": 1}\nand one more thing\n'))

    def test_a_closing_code_fence_is_allowed(self):
        self.assertEqual(driver.closing_object('here:\n```json\n{"a": 1}\n```\n'),
                         '{"a": 1}')

    def test_a_valid_earlier_object_is_never_reached_for(self):
        text = 'For example:\n{"findings": []}\n\nand now the real one: {"broken"\n'
        self.assertIsNone(driver.closing_object(text))

    def test_the_object_is_returned_byte_for_byte(self):
        text = 'x\n{\n  "a":   1,\n  "b": [2,3]\n}'
        self.assertEqual(driver.closing_object(text), text[2:])

    def test_an_earlier_object_is_not_taken_when_the_tail_also_ends_in_a_brace(self):
        """The guard that cannot be the `}` at the end: this tail ends in one, and the only
        thing separating the answer from an earlier object is that the decode has to consume
        the transcript to its last character."""
        self.assertIsNone(driver.closing_object('{"a": 1} and then {not json}\n'))

    def test_a_bare_array_or_scalar_is_not_a_closing_object(self):
        self.assertIsNone(driver.closing_object("[1, 2]"))
        self.assertIsNone(driver.closing_object("42"))


# --------------------------------------------------------------------------- #
# 3.2 attempt states
# --------------------------------------------------------------------------- #
class AnOutcomeIsAStatusRecordNotAFile(_Case):
    """`status.txt` is created empty by the redirect, the supervisor creates its findings
    file empty when it claims it and deletes it again on a failure, and its display log's
    end marker is written before the transcript is routed. So the predicate is a complete,
    valid status record and nothing else."""

    def setUp(self):
        super().setUp()
        self.fake_units(("u1", "A"))

    def state(self, **kw):
        path = self.attempt("u1", 0, **kw)
        return driver.read_attempt(self.rundir, "u1", path, grace=120.0).state

    def test_an_empty_status_file_is_not_a_decided_attempt(self):
        self.assertEqual(self.state(status=""), driver.RUNNING)

    def test_a_truncated_status_line_is_not_a_decided_attempt(self):
        self.assertEqual(self.state(status='{"status": "o'), driver.RUNNING)

    def test_a_complete_status_line_is_decided_however_much_precedes_it(self):
        self.assertEqual(self.state(status='noise\n{"status": "ok", "reason": null}\n'),
                         driver.DECIDED)

    def test_a_transcript_is_never_a_completion_signal(self):
        self.assertEqual(self.state(transcript='{"findings": [], "summary": "x"}'),
                         driver.RUNNING)

    def test_an_incomplete_spawn_record_is_an_orphan_claim(self):
        self.assertEqual(self.state(argv=False), driver.ORPHAN_CLAIM)

    def test_past_the_deadline_and_the_grace_an_attempt_is_uncertain(self):
        self.assertEqual(self.state(spawn_time=time.time() - 10_000), driver.UNCERTAIN)

    def test_grace_authorizes_nothing_it_only_quarantines(self):
        """The shape of a supervisor that died without writing its status while its worker
        lived on. Nothing here may re-spawn the unit or land an answer for it: the worker
        may be running yet, and an outcome nobody can prove is not a failure to publish."""
        (self.rundir / "units" / "u1").mkdir(parents=True, exist_ok=True)
        self.attempt("u1", 0, spawn_time=time.time() - 10_000)
        run = self.run_object()
        run.adopt()
        self.assertFalse(run.eligible("u1"))
        self.assertIn("uncertain", run.quarantined("u1"))
        self.assertIsNone(driver.landed(self.rundir, "u1"))
        self.assertFalse((self.rundir / "units" / "u1" / "error.txt").exists())
        self.assertEqual(len(driver.attempt_dirs(self.rundir, "u1")), 1)


# --------------------------------------------------------------------------- #
# 3.3 one active attempt per unit
# --------------------------------------------------------------------------- #
class OneAuthorizedActiveAttemptPerUnit(_Case):

    def setUp(self):
        super().setUp()
        self.fake_units(("u1", "A"), ("u2", "B"))

    def test_spare_capacity_does_not_start_a_second_attempt_while_one_runs(self):
        """Capacity is not authorization. A retry is authorized by the preceding attempt's
        disposition and by nothing else, so another unit finishing must never start one."""
        self.attempt("u1", 0)
        run = self.run_object(capacity=4)
        self.assertFalse(run.eligible("u1"))
        self.assertEqual(run.reserving()["A"], 1)

    def test_an_adoption_pass_does_not_make_a_running_unit_eligible(self):
        self.attempt("u1", 0)
        run = self.run_object()
        run.adopt()
        self.assertFalse(run.eligible("u1"))

    def test_a_decided_but_unadjudicated_attempt_blocks_a_retry(self):
        self.attempt("u1", 0, status='{"status": "error", "reason": "x"}')
        self.assertFalse(self.run_object().eligible("u1"))

    def test_a_retry_is_authorized_by_the_previous_disposition(self):
        self.attempt("u1", 0, status='{"status": "error", "reason": "x"}',
                     disposition={"outcome": driver.WORKER_FAILED,
                                  "intended_publication": "none"})
        self.assertTrue(self.run_object().eligible("u1"))

    def test_an_accepted_attempt_ends_the_unit(self):
        self.attempt("u1", 0, disposition={"outcome": driver.ACCEPTED,
                                           "intended_publication": "result"})
        self.assertFalse(self.run_object().eligible("u1"))

    def test_an_uncertain_attempt_keeps_its_capacity_reservation(self):
        """The worker may still be alive, so the slot it is using is still spoken for."""
        self.attempt("u1", 0, spawn_time=time.time() - 10_000)
        self.assertEqual(self.run_object().reserving()["A"], 1)

    def test_the_slot_and_the_writer_count_never_disagree(self):
        """One predicate, asked by both. Two spellings of "may still be running" let an
        operator-failed writer with no attestation hold a slot while the count of running
        writers ignored it — so a second reproduction could start beside a worker that may
        still have had the ports, caches and credentials the two of them share."""
        self.fake_units(("probe-A", "A"))
        doc = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        doc["units"][0]["kind"] = review_panel.PROBE_KIND
        (self.rundir / "units.json").write_text(json.dumps(doc), encoding="utf-8")
        path = self.attempt("probe-A", 0, spawn_time=time.time() - 10_000,
                            resolution={"action": "fail", "reason": "cannot be proven",
                                        "stopped_confirmed": False},
                            disposition={"attempt": "a0",
                                         "outcome": driver.OPERATOR_FAILED,
                                         "stopped_confirmed": False,
                                         "intended_publication": "error"})
        run = self.run_object()
        self.assertEqual(run.reserving()["A"], 1)
        self.assertEqual(run.writers_running(), 1,
                         "a slot was reserved for a writer the writer count ignored")
        # A late status proves that worker finished, and BOTH have to let go of it.
        (path / "status.txt").write_text('{"status": "ok"}\n', encoding="utf-8")
        run = self.run_object()
        self.assertEqual(run.reserving()["A"], 0,
                         "a reservation outlived the proof that its worker finished")
        self.assertEqual(run.writers_running(), 0)


# --------------------------------------------------------------------------- #
# 3.5 a disposition is not a landing
# --------------------------------------------------------------------------- #
class OneSelectedTerminalPublication(_Case):

    def setUp(self):
        super().setUp()
        self.fake_units(("u1", "A"))
        (self.rundir / "units" / "u1").mkdir(parents=True, exist_ok=True)

    def accepted(self, reply='{"findings": [], "summary": "x"}'):
        path = self.attempt("u1", 0, status='{"status": "ok"}',
                            disposition={"attempt": "a0", "outcome": driver.ACCEPTED,
                                         "intended_publication": "result"})
        (path / "reply.json").write_text(reply, encoding="utf-8")
        return path

    def test_a_kill_between_an_accepted_disposition_and_the_landing_is_replayed(self):
        self.accepted()
        run = self.run_object()
        self.assertFalse(run.terminal("u1"))
        run.adopt()
        self.assertTrue(run.terminal("u1"))
        self.assertEqual(
            (self.rundir / "units" / "u1" / "result.json").read_text(encoding="utf-8"),
            '{"findings": [], "summary": "x"}')

    def test_the_replay_is_idempotent_across_two_restarts(self):
        self.accepted()
        for _ in range(3):
            self.run_object().adopt()
        landed = driver.landed(self.rundir, "u1")
        self.assertEqual(landed["attempt"], "a0")
        self.assertEqual(landed["publication"], "result")
        self.assertEqual(len(driver.attempt_dirs(self.rundir, "u1")), 1,
                         "a replayed publication started another attempt")

    def test_a_publication_with_no_record_is_not_terminal(self):
        """A file existing is not a landing: the record has to name the disposition that
        produced it, or a crash between the two reads as done with nothing behind it."""
        (self.rundir / "units" / "u1" / "result.json").write_text("{}", encoding="utf-8")
        self.assertIsNone(driver.landed(self.rundir, "u1"))

    def test_a_record_with_no_publication_is_not_terminal(self):
        (self.rundir / "dispatch" / "u1").mkdir(parents=True, exist_ok=True)
        (self.rundir / "dispatch" / "u1" / "landed.json").write_text(
            json.dumps({"publication": "result", "attempt": "a0"}), encoding="utf-8")
        self.assertIsNone(driver.landed(self.rundir, "u1"))

    def test_a_unit_never_holds_both_a_result_and_an_error(self):
        (self.rundir / "units" / "u1" / "result.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(driver.DriverError) as ctx:
            driver.publish(self.rundir, "u1", None, "error", "no")
        self.assertIn("exactly one of complete or failed", str(ctx.exception))

    def test_an_exhausted_unit_lands_an_error_naming_every_attempt(self):
        for index in range(2):
            self.attempt("u1", index, status='{"status": "ok"}',
                         disposition={"attempt": f"a{index}", "outcome": driver.NO_REPLY,
                                      "reason": "no closing object",
                                      "intended_publication":
                                          "error" if index else "none"})
        run = self.run_object()
        run.adopt()
        self.assertTrue(run.terminal("u1"))
        text = (self.rundir / "units" / "u1" / "error.txt").read_text(encoding="utf-8")
        self.assertIn("a0", text)
        self.assertIn("a1", text)

    def test_a_landed_unit_is_not_re_adjudicated(self):
        path = self.accepted()
        self.run_object().adopt()
        before = (path / "disposition.json").read_bytes()
        self.attempt("u1", 1, status='{"status": "ok"}')   # a late status arrives
        self.run_object().adopt()
        self.assertEqual((path / "disposition.json").read_bytes(), before)
        self.assertEqual(driver.landed(self.rundir, "u1")["attempt"], "a0")


# --------------------------------------------------------------------------- #
# 5.3 allowances
# --------------------------------------------------------------------------- #
class WhatThisPhaseMustNotDestroy(_Case):
    """Retention and its budget are the storage phase's. What this phase owes that work is
    not to delete what it exists to keep: a copy whose landed verdict names a run that was
    actually performed. Deferring a deletion costs disk; the other mistake costs the
    reproduction."""

    def setUp(self):
        super().setUp()
        self.fake_units(("verify-area-01-A", "B"))
        doc = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        doc["units"][0]["kind"] = review_panel.VERIFIER_KIND
        (self.rundir / "units.json").write_text(json.dumps(doc), encoding="utf-8")
        (self.rundir / "units" / "verify-area-01-A").mkdir(parents=True, exist_ok=True)

    def landed_with(self, evidence):
        token = "tok-verify-area-01-A-0"
        work = self.rundir / "work" / token
        work.mkdir(parents=True)
        (work / "reproduce.sh").write_text("echo hi\n", encoding="utf-8")
        (self.rundir / "in" / token).mkdir(parents=True)
        path = self.attempt("verify-area-01-A", 0, status='{"status": "ok"}',
                            disposition={"attempt": "a0", "outcome": driver.ACCEPTED,
                                         "intended_publication": "result"})
        (path / "reply.json").write_text(json.dumps(
            {"verdicts": [{"candidate": "cand-001", "status": "reproduced",
                           "evidence": evidence}], "summary": "x"}), encoding="utf-8")
        run = self.run_object()
        run.adopt()
        self.assertTrue(run.terminal("verify-area-01-A"))
        return work, run

    def test_a_copy_whose_verdict_names_an_executed_run_is_kept(self):
        work, run = self.landed_with(
            {"argv": ["bash", "reproduce.sh"], "cwd": ".", "exit_status": 1,
             "output": "boom\n", "truncated": False, "run_kind": "executed",
             "shows": "Shows the failure."})
        self.assertTrue(work.is_dir(), "a reproduction was deleted with its unit")
        self.assertTrue((work / "reproduce.sh").is_file())
        self.assertIn("work/", status_text(run))

    def test_a_copy_whose_verdict_names_only_a_reading_is_released(self):
        work, _run = self.landed_with(
            {"argv": ["grep", "-c", "x", "a.py"], "cwd": ".", "exit_status": 1,
             "output": "0\n", "truncated": False, "run_kind": "documentary",
             "shows": "Shows the name appears nowhere."})
        self.assertFalse(work.exists())

    def test_a_reply_that_cannot_be_read_keeps_its_copy(self):
        token = "tok-verify-area-01-A-0"
        work = self.rundir / "work" / token
        work.mkdir(parents=True)
        path = self.attempt("verify-area-01-A", 0, status='{"status": "ok"}',
                            disposition={"attempt": "a0", "outcome": driver.ACCEPTED,
                                         "intended_publication": "result"})
        (path / "reply.json").write_text("{ truncated", encoding="utf-8")
        self.run_object().adopt()
        self.assertTrue(work.is_dir(), "a copy was deleted on an answer nobody could read")

    def test_the_input_directory_goes_either_way(self):
        work, _run = self.landed_with(
            {"argv": ["bash", "reproduce.sh"], "cwd": ".", "exit_status": 1,
             "output": "boom\n", "truncated": False, "run_kind": "executed",
             "shows": "Shows the failure."})
        self.assertTrue(work.is_dir())
        self.assertFalse((self.rundir / "in" / "tok-verify-area-01-A-0").exists(),
                         "the payload copy is not evidence and is not kept")


def status_text(run):
    import io
    return driver.status_report(run)


class ReplayStableBudgets(_Case):

    def setUp(self):
        super().setUp()
        self.fake_units(("u1", "A"))
        (self.rundir / "units" / "u1").mkdir(parents=True, exist_ok=True)

    def dispositions(self, *outcomes):
        for index, outcome in enumerate(outcomes):
            self.attempt("u1", index, status='{"status": "ok"}',
                         disposition={"attempt": f"a{index}", "outcome": outcome,
                                      "intended_publication": "none"})

    def budget(self):
        run = self.run_object()
        return driver.replay([a.disposition for a in run.attempts("u1")
                              if a.disposition is not None], run.granted("u1"))

    def test_one_refusal_leaves_the_reply_allowance_with_a_re_dispatch(self):
        self.dispositions(driver.REFUSED)
        self.assertFalse(self.budget().exhausted)
        self.assertTrue(self.run_object().eligible("u1"))

    def test_two_reply_charges_exhaust_it(self):
        self.dispositions(driver.REFUSED, driver.NO_REPLY)
        self.assertTrue(self.budget().exhausted)
        self.assertFalse(self.run_object().eligible("u1"))

    def test_two_worker_failures_still_leave_a_re_dispatch(self):
        self.dispositions(driver.WORKER_FAILED, driver.LAUNCH_FAILED)
        self.assertFalse(self.budget().exhausted)

    def test_three_failure_charges_exhaust_it(self):
        self.dispositions(driver.WORKER_FAILED, driver.LAUNCH_FAILED, driver.WORKER_FAILED)
        self.assertTrue(self.budget().exhausted)

    def test_mixed_charges_exhaust_at_four_charging_attempts(self):
        self.dispositions(driver.REFUSED, driver.WORKER_FAILED, driver.WORKER_FAILED)
        self.assertFalse(self.budget().exhausted)
        self.dispositions(driver.REFUSED, driver.WORKER_FAILED, driver.WORKER_FAILED,
                          driver.INFRASTRUCTURE)
        self.assertFalse(self.budget().exhausted,
                         "an infrastructure fault counted toward the charging ceiling")

    def test_infrastructure_and_provider_faults_charge_nothing(self):
        """A quota failure on the fourth launch must not both charge nothing and exhaust
        the unit — the contradiction the charging count exists to keep out."""
        self.dispositions(driver.INFRASTRUCTURE, driver.PROVIDER_UNAVAILABLE,
                          driver.INFRASTRUCTURE, driver.PROVIDER_UNAVAILABLE)
        budget = self.budget()
        self.assertEqual(budget.charging, 0)
        self.assertFalse(budget.exhausted)
        self.assertTrue(self.run_object().eligible("u1"))

    def test_the_launch_ceiling_quarantines_rather_than_failing_the_unit(self):
        """Driven through a real adjudication rather than hand-written dispositions, so the
        intention the ceiling produces is the one the code computes: repeated infrastructure
        faults are not this unit's answer, and publishing one would make them it."""
        self.dispositions(*([driver.INFRASTRUCTURE] * (driver.LAUNCH_CEILING - 1)))
        self.attempt("u1", driver.LAUNCH_CEILING - 1,
                     status='{"status": "error", "reason": "routing failed: [Errno 28]"}')
        run = self.run_object()
        run.adopt()
        self.assertTrue(self.budget().at_ceiling)
        self.assertFalse(run.eligible("u1"))
        self.assertIn("hard stop", run.quarantined("u1"))
        self.assertFalse((self.rundir / "units" / "u1" / "error.txt").exists(),
                         "repeated infrastructure faults were published as the unit's answer")
        self.assertIsNone(driver.landed(self.rundir, "u1"))

    def test_a_grant_raises_the_ceiling_and_replay_reaches_the_same_number(self):
        self.dispositions(*([driver.INFRASTRUCTURE] * driver.LAUNCH_CEILING))
        driver.resolve_unit(self.rundir, "u1", grant=2, fail=False, reason="the host is back")
        run = self.run_object()
        self.assertEqual(run.granted("u1"), 2)
        self.assertFalse(self.budget().at_ceiling)
        self.assertTrue(run.eligible("u1"))
        self.assertEqual(self.run_object().granted("u1"), 2, "the grant did not replay")

    def test_exhaustion_outranks_the_non_charging_ceiling(self):
        """A unit can reach both at once — six faults that charge nothing, then two replies
        the engine will not take. Asked about the ceiling first, it is quarantined instead
        of landing the error it has earned, granting launches does not repair it, and it is
        neither eligible nor terminal."""
        self.dispositions(*([driver.INFRASTRUCTURE] * 6))
        for index, outcome in ((6, driver.NO_REPLY), (7, driver.REFUSED)):
            self.attempt("u1", index, status='{"status": "ok"}',
                         transcript="nothing here\n",
                         disposition={"attempt": f"a{index}", "outcome": outcome,
                                      "reason": "no closing object",
                                      "intended_publication": driver._intended(
                                          [{"outcome": driver.INFRASTRUCTURE}] * 6
                                          + [{"outcome": driver.NO_REPLY}]
                                          + ([{"outcome": driver.REFUSED}] if index == 7
                                             else []), 0)})
        budget = self.budget()
        self.assertTrue(budget.exhausted)
        self.assertTrue(budget.at_ceiling, "the fixture does not reach both at once")
        run = self.run_object()
        run.adopt()
        self.assertTrue(run.terminal("u1"),
                        "a unit that spent its allowances was quarantined instead")
        self.assertTrue((self.rundir / "units" / "u1" / "error.txt").exists())
        self.assertIsNone(run.quarantined("u1"))

    def test_an_operator_retry_does_not_raise_the_ceiling(self):
        self.dispositions(*([driver.INFRASTRUCTURE] * (driver.LAUNCH_CEILING - 1)))
        self.attempt("u1", driver.LAUNCH_CEILING - 1,
                     disposition={"attempt": "a7", "outcome": driver.OPERATOR_RETRY,
                                  "intended_publication": "none"})
        self.assertTrue(self.budget().at_ceiling)
        self.assertIn("hard stop", self.run_object().quarantined("u1"))


# --------------------------------------------------------------------------- #
# 8. operator reconciliation
# --------------------------------------------------------------------------- #
class OperatorReconciliation(_Case):

    def setUp(self):
        super().setUp()
        self.fake_units(("u1", "A"))
        (self.rundir / "units" / "u1").mkdir(parents=True, exist_ok=True)

    def dispositions_at_ceiling(self):
        """The state `resolve-unit` exists for: a unit quarantined by the launch ceiling,
        with nothing charged against its allowances."""
        for index in range(driver.LAUNCH_CEILING):
            self.attempt("u1", index, status='{"status": "error", "reason": "ENOSPC"}',
                         disposition={"attempt": f"a{index}",
                                      "outcome": driver.INFRASTRUCTURE,
                                      "intended_publication": "none"})
        self.assertIsNotNone(self.run_object().quarantined("u1"))

    def test_retry_requires_the_attestation(self):
        self.attempt("u1", 0, spawn_time=time.time() - 10_000)
        with self.assertRaises(driver.DriverError) as ctx:
            driver.resolve_attempt(self.rundir, "u1", "a0", action="retry",
                                   reason="it is gone", stopped_confirmed=False)
        self.assertIn("--stopped-confirmed", str(ctx.exception))

    def test_retry_with_the_attestation_authorizes_one_more_attempt(self):
        self.attempt("u1", 0, spawn_time=time.time() - 10_000)
        driver.resolve_attempt(self.rundir, "u1", "a0", action="retry",
                               reason="I killed it myself", stopped_confirmed=True)
        run = self.run_object()
        run.adopt()
        self.assertIsNone(run.quarantined("u1"))
        self.assertTrue(run.eligible("u1"))

    def test_fail_requires_the_attestation_like_retry_and_then_releases_the_reservation(self):
        """A `--fail` that keeps the capacity reservation without the attestation -- because
        execution may continue -- is a reservation nothing can release. At the default of
        one worker per slot that holds the slot for the rest of the run: every later unit
        on it refused on every resume, the only way out failing each by hand. The operator
        has the same two answers either way, so the command asks for one of them up front."""
        self.attempt("u1", 0, spawn_time=time.time() - 10_000)
        with self.assertRaises(driver.DriverError) as ctx:
            driver.resolve_attempt(self.rundir, "u1", "a0", action="fail",
                                   reason="the answer cannot be proven",
                                   stopped_confirmed=False)
        self.assertIn("--stopped-confirmed", str(ctx.exception))
        self.assertFalse((self.rundir / "dispatch" / "u1" / "a0" / driver.RESOLUTION_NAME).exists(),
                         "a refusal wrote a record")
        driver.resolve_attempt(self.rundir, "u1", "a0", action="fail",
                               reason="I killed it myself", stopped_confirmed=True)
        run = self.run_object()
        run.adopt()
        self.assertTrue(run.terminal("u1"))
        self.assertEqual(run.reserving()["A"], 0, "the reservation outlived the attestation")

    def test_a_resolution_takes_precedence_over_a_status_arriving_later(self):
        path = self.attempt("u1", 0, spawn_time=time.time() - 10_000)
        driver.resolve_attempt(self.rundir, "u1", "a0", action="fail",
                               reason="unprovable", stopped_confirmed=True)
        (path / "status.txt").write_text('{"status": "ok"}\n', encoding="utf-8")
        self.run_object().adopt()
        disposition = json.loads((path / "disposition.json").read_text(encoding="utf-8"))
        self.assertEqual(disposition["outcome"], driver.OPERATOR_FAILED)
        self.assertTrue(disposition["superseded_status"])

    def test_a_resolution_cannot_overwrite_a_disposition(self):
        self.attempt("u1", 0, status='{"status": "ok"}',
                     disposition={"attempt": "a0", "outcome": driver.REFUSED,
                                  "intended_publication": "none"})
        with self.assertRaises(driver.DriverError) as ctx:
            driver.resolve_attempt(self.rundir, "u1", "a0", action="fail",
                                   reason="change my mind", stopped_confirmed=True)
        self.assertIn("written once", str(ctx.exception))

    def test_resolve_unit_fail_publishes_without_rewriting_a_disposition(self):
        path = self.attempt("u1", 0, status='{"status": "ok"}',
                            disposition={"attempt": "a0", "outcome": driver.INFRASTRUCTURE,
                                         "intended_publication": "none"})
        before = (path / "disposition.json").read_bytes()
        driver.resolve_unit(self.rundir, "u1", grant=None, fail=True, reason="give up")
        self.assertTrue(driver.landed(self.rundir, "u1"))
        self.assertEqual((path / "disposition.json").read_bytes(), before)
        self.assertIn("give up",
                      (self.rundir / "units" / "u1" / "error.txt").read_text(encoding="utf-8"))

    def test_an_attempt_still_inside_its_deadline_cannot_be_resolved(self):
        """A resolution supersedes any status that arrives after it, so writing one over a
        live attempt throws away the outcome it is about to report — and with --retry starts
        a second worker beside it."""
        self.attempt("u1", 0)
        with self.assertRaises(driver.DriverError) as ctx:
            driver.resolve_attempt(self.rundir, "u1", "a0", action="fail",
                                   reason="impatient", stopped_confirmed=True)
        self.assertIn("running", str(ctx.exception))
        self.assertFalse((self.rundir / "dispatch" / "u1" / "a0" / "resolution.json").exists())
        # And the operator who does know says so with the number the run reads states with.
        driver.resolve_attempt(self.rundir, "u1", "a0", action="fail", reason="I killed it",
                               stopped_confirmed=True, grace=-100_000)
        self.assertTrue((self.rundir / "dispatch" / "u1" / "a0" / "resolution.json").exists())

    def test_resolve_unit_fail_records_its_decision_before_it_publishes(self):
        """The record first, the publication second — the order every other terminal outcome
        already uses. Publishing straight out left an `error.txt` adoption could attribute
        to nothing: a kill before `landed.json` and the unit is not terminal, while at the
        launch ceiling it stays quarantined for ever."""
        self.dispositions_at_ceiling()
        order = []
        real_publish = driver.publish
        real_write = driver._write_json

        def watching_write(path, obj):
            if path.name == driver.UNIT_RESOLUTION_NAME:
                order.append("record")
            return real_write(path, obj)

        def watching_publish(*args, **kwargs):
            order.append("publish")
            return real_publish(*args, **kwargs)

        with mock.patch.object(driver, "_write_json", watching_write), \
                mock.patch.object(driver, "publish", watching_publish):
            driver.resolve_unit(self.rundir, "u1", grant=None, fail=True,
                                reason="nobody can run this host")
        self.assertEqual(order[:2], ["record", "publish"])
        self.assertIsNotNone(driver.unit_resolution(self.rundir, "u1"))
        self.assertTrue(self.run_object().terminal("u1"))

    def test_a_kill_between_the_operator_decision_and_its_error_is_replayed(self):
        self.dispositions_at_ceiling()
        with mock.patch.object(driver, "publish", side_effect=RuntimeError("killed")):
            with self.assertRaises(RuntimeError):
                driver.resolve_unit(self.rundir, "u1", grant=None, fail=True,
                                    reason="nobody can run this host")
        run = self.run_object()
        self.assertFalse(run.terminal("u1"))
        self.assertFalse(run.eligible("u1"),
                         "a unit the operator failed was authorized more work")
        run.adopt()
        self.assertTrue(run.terminal("u1"), "adoption could not finish the publication")
        self.assertIn("nobody can run this host",
                      (self.rundir / "units" / "u1" / "error.txt").read_text(
                          encoding="utf-8"))
        self.assertIsNone(run.quarantined("u1"))

    def test_a_half_made_landing_is_reconciled_before_the_decision_is_taken(self):
        """A unit whose accepted result was published while its landing record was not
        looks unlanded. Committing the operator's decision first and only then finding the
        result file left a durable intention every later adoption picked up and refused on,
        with no command able to undo it — one unit in that state stopped the run. The
        command reconciles first, and then has nothing to decide."""
        path = self.attempt("u1", 0, status='{"status": "ok"}',
                            disposition={"attempt": "a0", "outcome": driver.ACCEPTED,
                                         "intended_publication": "result"})
        (path / "reply.json").write_text('{"findings": [], "summary": "x"}',
                                         encoding="utf-8")
        # The interrupted state: the result is on disk, the landing record is not.
        (self.rundir / "units" / "u1" / "result.json").write_text(
            '{"findings": [], "summary": "x"}', encoding="utf-8")
        self.assertIsNone(driver.landed(self.rundir, "u1"))
        with self.assertRaises(driver.DriverError) as ctx:
            driver.resolve_unit(self.rundir, "u1", grant=None, fail=True, reason="give up")
        self.assertIn("already landed", str(ctx.exception))
        self.assertIsNone(driver.unit_resolution(self.rundir, "u1"),
                          "a refused command left an intention adoption cannot carry out")
        # And the run carries on: the landing is finished, not blocked.
        run = self.run_object()
        run.adopt()
        self.assertTrue(run.terminal("u1"))
        self.assertEqual(driver.landed(self.rundir, "u1")["attempt"], "a0")

    def test_a_publication_nothing_accounts_for_is_rejected_not_decided_over(self):
        """The other half of reconcile-or-reject: an answer on disk that no disposition
        explains. It is refused by name with nothing written, rather than decided over."""
        (self.rundir / "dispatch" / "u1").mkdir(parents=True, exist_ok=True)
        (self.rundir / "units" / "u1" / "result.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(driver.DriverError) as ctx:
            driver.resolve_unit(self.rundir, "u1", grant=None, fail=True, reason="give up")
        self.assertIn("result.json", str(ctx.exception))
        self.assertIn("move that file aside", str(ctx.exception))
        self.assertIsNone(driver.unit_resolution(self.rundir, "u1"))

    def test_an_operator_decision_is_written_once(self):
        self.dispositions_at_ceiling()
        driver.resolve_unit(self.rundir, "u1", grant=None, fail=True, reason="give up")
        with self.assertRaises(driver.DriverError):
            driver.resolve_unit(self.rundir, "u1", grant=None, fail=True, reason="again")

    def test_a_resolution_needs_a_reason(self):
        self.attempt("u1", 0)
        with self.assertRaises(driver.DriverError):
            driver.resolve_attempt(self.rundir, "u1", "a0", action="fail", reason="  ",
                                   stopped_confirmed=True)


# --------------------------------------------------------------------------- #
# 3.4 adjudication from a real supervisor status
# --------------------------------------------------------------------------- #
class EveryCategoryIsReadFromTheStatusFirst(_Case):

    def setUp(self):
        super().setUp()
        self.fake_units(("u1", "A"))
        (self.rundir / "units" / "u1").mkdir(parents=True, exist_ok=True)

    def adjudicate(self, status, transcript=None):
        path = self.attempt("u1", 0, status=status, transcript=transcript)
        run = self.run_object()
        attempt = driver.read_attempt(self.rundir, "u1", path, grace=120.0)
        return driver.adjudicate(run, attempt)

    def test_a_storage_fault_is_infrastructure_and_never_the_unit_s_answer(self):
        record = self.adjudicate(
            '{"status": "error", "reason": "routing failed: [Errno 28] ENOSPC"}')
        self.assertEqual(record["outcome"], driver.INFRASTRUCTURE)

    def test_any_other_failure_is_the_worker_s(self):
        record = self.adjudicate('{"status": "error", "reason": "reviewer exited 1"}')
        self.assertEqual(record["outcome"], driver.WORKER_FAILED)

    def test_an_ok_status_with_no_transcript_is_infrastructure_not_a_failed_launch(self):
        record = self.adjudicate('{"status": "ok"}')
        self.assertEqual(record["outcome"], driver.INFRASTRUCTURE)

    def test_an_ok_status_with_no_closing_object_is_no_reply(self):
        record = self.adjudicate('{"status": "ok"}', transcript="I gave up.\n")
        self.assertEqual(record["outcome"], driver.NO_REPLY)

    def test_a_reply_the_engine_refuses_is_refused_not_accepted(self):
        record = self.adjudicate('{"status": "ok"}',
                                 transcript='{"findings": "not a list", "summary": "x"}')
        self.assertEqual(record["outcome"], driver.REFUSED)

    def test_a_disposition_is_written_once_and_carries_one_outcome(self):
        """**Written once, counted.** Examining one adjudication and reading the file back
        says nothing about immutability: a path that commits a provisional record and then
        unlinks and replaces it passes that reading exactly. So this counts the writes, and
        then asks a second adoption pass to leave the bytes alone."""
        writes = []
        real = driver._write_json

        def counting(path, obj):
            if path.name == driver.DISPOSITION_NAME:
                writes.append(str(path))
            return real(path, obj)

        with mock.patch.object(driver, "_write_json", counting):
            record = self.adjudicate('{"status": "error", "reason": "x"}')
        self.assertIn(record["outcome"], driver.OUTCOMES)
        on_disk = self.rundir / "dispatch" / "u1" / "a0" / "disposition.json"
        self.assertEqual(json.loads(on_disk.read_text(encoding="utf-8"))["outcome"],
                         record["outcome"])
        self.assertEqual(len(writes), 1, f"the disposition was written {len(writes)} times")
        before = on_disk.read_bytes()
        with mock.patch.object(driver, "_write_json", counting):
            self.run_object().adopt()
            self.run_object().adopt()
        self.assertEqual(on_disk.read_bytes(), before, "a later pass rewrote a disposition")
        self.assertEqual(len(writes), 1, "a later pass wrote the disposition again")

    def test_a_launch_that_never_started_writes_its_disposition_once(self):
        """The one outcome the supervisor cannot record. It was committed provisionally and
        then unlinked and replaced, which makes `immutable` a claim replay cannot rely on: a
        kill between the two leaves a unit whose last disposition says publish nothing while
        its allowances are spent — neither eligible nor terminal, and no pass can finish it."""
        writes = []
        real = driver._write_json

        def counting(path, obj):
            if path.name == driver.DISPOSITION_NAME:
                writes.append(dict(obj))
            return real(path, obj)

        # Two failures already charged, so this third one is what exhausts the unit and the
        # intention it must carry is `error` — the value the provisional write got wrong.
        for index in range(2):
            self.attempt("u1", index, status='{"status": "error", "reason": "x"}',
                         disposition={"attempt": f"a{index}",
                                      "outcome": driver.WORKER_FAILED,
                                      "intended_publication": "none"})
        run = self.run_object()
        with mock.patch.object(driver.subprocess, "Popen",
                               side_effect=OSError("no such supervisor")):
            with mock.patch.object(driver, "_write_json", counting):
                run.spawn(self.run_object().unit_row("u1"))
        self.assertEqual(len(writes), 1,
                         f"the launch failure wrote its disposition {len(writes)} times")
        self.assertEqual(writes[0]["outcome"], driver.LAUNCH_FAILED)
        self.assertEqual(writes[0]["intended_publication"], "error",
                         "the one write committed an intention replay cannot act on")
        # And the unit finishes from that record alone, with no further adjudication.
        run = self.run_object()
        run.adopt()
        self.assertTrue(run.terminal("u1"))


# --------------------------------------------------------------------------- #
# the loop, end to end
# --------------------------------------------------------------------------- #
class AFullRun(_Case):
    """One complete run with the stub, and the questions that can be asked of it. Shared
    setup rather than one run per question: the run is the expensive part and every
    assertion below is read-only."""

    @classmethod
    def setUpClass(cls):
        cls._layout = _lay_out(Path(tempfile.mkdtemp(prefix="rp-driver-full-")))
        case = cls("run_it")
        case.__dict__.update(cls._layout)
        case.write_adapter()
        case.drive("--go")

    def setUp(self):   # deliberately NOT _Case.setUp: the run is the class's, made once
        self.__dict__.update(self._layout)

    def run_it(self):  # pragma: no cover - a name for the setUpClass instance only
        raise AssertionError("not a test")

    @classmethod
    def tearDownClass(cls):
        _discard(cls._layout["tmp"])

    def units_doc(self):
        return json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))

    def test_the_run_reaches_a_report_and_the_marker_says_so(self):
        self.assertEqual(self.units_doc()["stage"], review_panel.REPORTED_STAGE)
        for name in ("report.md", "report.html", "findings.json", "dispatch.json"):
            self.assertTrue((self.rundir / name).is_file(), name)
        # `report` refuses without this file and parses it by name, so what the driver
        # writes has to satisfy the engine's own parse and not merely exist.
        record = review_panel.parse_dispatch(json.loads(
            (self.rundir / review_panel.DISPATCH_FILE_NAME).read_text(encoding="utf-8")))
        self.assertIn(record.rung, review_panel.RUNGS)

    def test_every_unit_landed_exactly_one_publication(self):
        for unit in self.units_doc()["units"]:
            record = driver.landed(self.rundir, unit["id"])
            self.assertIsNotNone(record, unit["id"])
            here = self.rundir / "units" / unit["id"]
            self.assertNotEqual((here / "result.json").exists(),
                                (here / "error.txt").exists(), unit["id"])

    def test_a_completed_resume_returns_immediately_and_spawns_nothing(self):
        before = self.stub_invocations()
        proc = self.drive("--go")
        self.assertIn("reported:", proc.stdout)
        self.assertEqual(self.stub_invocations(), before,
                         "a reported run dispatched something")

    def test_no_worker_visible_argument_carries_the_unit_name(self):
        """A verification unit is named for the area and the slot that RAISED its
        candidates, so the finder's identity is in the unit's own directory name. Every
        path a worker is handed — its payload, its schema and its working directory — is an
        opaque token instead."""
        names = [unit["id"] for unit in self.units_doc()["units"]]
        checked = 0
        for record in self.rundir.glob("dispatch/*/a*/argv.json"):
            argv = json.loads(record.read_text(encoding="utf-8"))
            worker = argv["argv"][argv["argv"].index("--") + 1:]
            for part in [*worker, argv["cwd"]]:
                for name in names:
                    self.assertNotIn(name, part, f"{record}: {part}")
            checked += 1
        self.assertGreater(checked, 4, "no spawn record was inspected")

    def test_the_driver_never_spawns_the_engine(self):
        """The engine's stages are called in-process, so no engine work can outlive the
        process that owns the run. A stage run as a child would keep writing a run
        directory whose lock says nobody owns it."""
        for record in self.rundir.glob("dispatch/*/a*/argv.json"):
            argv = json.loads(record.read_text(encoding="utf-8"))
            self.assertNotIn("review_panel.py", " ".join(argv["argv"]))

    def test_the_progress_log_names_every_spawn_and_every_landing(self):
        text = (self.rundir / "dispatch" / "progress.log").read_text(encoding="utf-8")
        self.assertIn("spawning", text)
        self.assertIn("landed", text)

    def test_status_is_read_only_and_answers_while_a_run_is_owned(self):
        """It writes nothing and takes no lock. The moment an operator most wants it is
        while a run is going, which is exactly when the lock is held — a status that refused
        then would only ever answer about runs nobody is working on."""
        holder = driver.RunLock(Path(str(self.rundir) + driver.LOCK_SUFFIX))
        holder.acquire()
        self.addCleanup(holder.release)
        before = {path: path.stat().st_mtime_ns
                  for path in sorted(self.rundir.rglob("*")) if path.is_file()}
        proc = subprocess.run([sys.executable, str(_DRIVER), "status", str(self.rundir)],
                              capture_output=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stage: reported", proc.stdout)
        after = {path: path.stat().st_mtime_ns
                 for path in sorted(self.rundir.rglob("*")) if path.is_file()}
        self.assertEqual(before, after)


class RecoveryTheOwnerIsTheOnlyOneThatCanDo(_Case):
    """Phase 7 took a per-stage claim in the engine and disclosed the cost: a hard-killed
    stage strands it and the next run refuses by name. It also said what would fix it — one
    process that owns the run end to end. This is that process."""

    def test_a_stranded_engine_claim_is_cleared_before_the_stage_runs(self):
        self.plan_only()
        stranded = self.rundir / f"route{review_panel.STAGE_LOCK_SUFFIX}"
        stranded.write_text("", encoding="utf-8")
        proc = self.drive("--go")
        self.assertIn("reported:", proc.stdout)
        self.assertIn("cleared route.lock", proc.stdout)
        self.assertFalse(stranded.exists())

    def test_only_the_engine_s_own_claims_are_cleared(self):
        """Enumerated, never swept: a `.lock` this driver does not put on its list is not
        its to remove, whoever left it."""
        self.plan_only()
        mine = self.rundir / "somebody-else.lock"
        mine.write_text("not the engine's", encoding="utf-8")
        for stage in driver.ENGINE_STAGE_CLAIMS:
            (self.rundir / f"{stage}{review_panel.STAGE_LOCK_SUFFIX}").write_text(
                "", encoding="utf-8")
        cleared = driver.clear_engine_claims(self.rundir)
        self.assertEqual(sorted(cleared),
                         sorted(f"{s}{review_panel.STAGE_LOCK_SUFFIX}"
                                for s in driver.ENGINE_STAGE_CLAIMS))
        self.assertTrue(mine.is_file())

    def test_nothing_slow_happens_between_the_claim_and_its_record(self):
        """A write-capable unit's working directory is a copy of the whole snapshot. Made
        after the claim, that copy IS the window between claiming an attempt and describing
        it — so a kill anywhere in it leaves a claim nobody can describe, which quarantines
        the unit and stops the run for an operator. The window has to be two adjacent
        writes, not a directory copy."""
        self.plan_only()
        run = self.run_object()
        probe = run.unit_row("probe-A")
        self.assertIn(probe["kind"], driver.WRITE_CAPABLE_KINDS)
        with mock.patch.object(driver.shutil, "copytree",
                               side_effect=OSError("killed mid-copy")):
            with self.assertRaises(driver.DriverError) as ctx:
                run.spawn(probe)
        self.assertIn("working directory", str(ctx.exception))
        self.assertEqual(driver.attempt_dirs(self.rundir, "probe-A"), [],
                         "a claim nobody can describe was left behind by a failed copy")

    def test_a_relative_supervisor_path_is_resolved_before_it_is_validated(self):
        """`is_file()` answers about the caller's directory and `Popen` runs with the run
        directory as its own, so a relative spelling that exists beside the caller names
        nothing beside the run — and every attempt then produces no status at all."""
        beside = self.tmp / "sup"
        beside.mkdir()
        shutil.copyfile(_SUPERVISOR, beside / "review_runner.py")
        proc = subprocess.run(
            [sys.executable, str(_DRIVER), "--job", str(self.job_path),
             "--rundir", str(self.rundir), "--adapter", str(self.adapter_path),
             "--supervisor", "sup/review_runner.py", "--go",
             "--poll", "0.1", "--capacity", "2", "--grace", "0"],
            cwd=str(self.tmp), capture_output=True, encoding="utf-8", errors="replace",
            timeout=300)
        self.assertEqual(proc.returncode, driver.EXIT_OK, proc.stdout + proc.stderr)
        self.assertIn("reported:", proc.stdout)
        recorded = json.loads(
            next(self.rundir.glob("dispatch/*/a0/argv.json")).read_text(encoding="utf-8"))
        self.assertTrue(Path(recorded["argv"][1]).is_absolute(), recorded["argv"][1])


class TheLoopAndItsStops(_Case):

    def test_a_zero_unit_final_round_completes(self):
        """A round that legitimately plans nothing is a committed round: nothing keys on a
        unit directory existing, so a run whose readers raise no finding still reports."""
        self.write_control(no_findings=True)
        proc = self.drive("--go")
        self.assertIn("reported:", proc.stdout)
        doc = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["stage"], review_panel.REPORTED_STAGE)
        self.assertFalse([u for u in doc["units"] if u["kind"] == "verifier"])

    def test_a_killed_driver_resumes_with_no_unit_run_twice_and_none_lost(self):
        """**The two numbers this needs are the attempt's deadline and the grace, and they
        are not the same lever.**

        Killing a driver mid-round can land between an attempt's record and its launch. That
        attempt then has a complete `argv.json` and no process at all, so nothing will ever
        write its status — and the resumed driver cannot know that, which is exactly right:
        an attempt whose outcome nobody can prove is never re-spawned and never landed. What
        it does instead is hold it as `running` until `spawn_time + deadline + grace`.

        So a grace long enough to keep a loaded host from calling a slow LIVE attempt
        uncertain is also long enough to stall this resume for the same duration. The
        deadline is what separates them: the supervisor bounds a live attempt at its own
        deadline and reports, so a short deadline classifies a dead attempt quickly while a
        generous grace still absorbs a slow report. Five seconds and sixty, against a stub
        that answers in under one.

        And when the kill did orphan an attempt, the resume stops and names it rather than
        finishing — which is the design, not a failure. The way out is the operator command,
        so the test takes it: that is the documented recovery, and waiting out a timer would
        be testing the clock."""
        self.write_adapter(deadline=5.0)
        self.write_control(sleep=0.4)
        proc = self.launch("--go", "--grace", "60")
        deadline = time.time() + 120
        while time.time() < deadline:
            if list(self.rundir.glob("units/*/result.json")):
                break
            time.sleep(0.05)
        else:
            proc.kill()
            self.fail("nothing landed before the kill window closed")
        proc.kill()
        proc.wait(timeout=30)
        proc.stdout.close()
        landed_before = {p.parent.name for p in self.rundir.glob("units/*/result.json")}
        self.assertTrue(landed_before)
        self.write_control()
        resumed = self.drive("--go", "--grace", "60", expect=None, timeout=300)
        if resumed.returncode == driver.EXIT_STOPPED:
            self.assertIn("stopped:", resumed.stdout)
            stranded = self.unaccountable(grace=60.0)
            self.assertTrue(stranded, resumed.stdout)
            for unit_id, attempt in stranded:
                driver.resolve_attempt(
                    self.rundir, unit_id, attempt, action="fail",
                    reason="the driver was killed between this attempt's record and its "
                           "launch, so nothing ever started",
                    stopped_confirmed=True, grace=60.0)
            resumed = self.drive("--go", "--grace", "60", expect=None, timeout=300)
        self.assertEqual(resumed.returncode, driver.EXIT_OK,
                         resumed.stdout + resumed.stderr)
        doc = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["stage"], review_panel.REPORTED_STAGE)
        for unit in doc["units"]:
            accepted = [a for a in driver.read_attempts(self.rundir, unit["id"], grace=120.0)
                        if (a.disposition or {}).get("outcome") == driver.ACCEPTED]
            self.assertLessEqual(len(accepted), 1,
                                 f"{unit['id']} was answered twice")
            self.assertIsNotNone(driver.landed(self.rundir, unit["id"]), unit["id"])
        for unit_id in landed_before:
            self.assertEqual(driver.landed(self.rundir, unit_id)["publication"], "result",
                             f"{unit_id} lost the answer it had before the kill")

    @unittest.skipUnless(
        os.name == "posix",
        "SIGTERM is not delivered on Windows: the usual way to send one terminates the "
        "process rather than asking it anything, so a handler-based drain cannot run "
        "there at all. That is a real limitation of the platform and not of this test — "
        "the portable request is the drain file, which "
        "test_a_drain_request_file_stops_claiming_on_every_platform covers everywhere")
    def test_sigterm_drains_and_the_run_resumes(self):
        self.write_control(sleep=0.5)
        proc = self.launch("--go")
        deadline = time.time() + 120
        while time.time() < deadline and not list(self.rundir.glob("dispatch/*/a0/argv.json")):
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=120)
        self.assertEqual(proc.returncode, driver.EXIT_OK, out)
        self.assertIn("drained on request", out)
        doc = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["stage"], review_panel.READING_STAGE,
                         "a drained run rolled into the next round")
        self.write_control()
        self.drive("--go")
        self.assertEqual(
            json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))["stage"],
            review_panel.REPORTED_STAGE)

    def test_a_drain_stops_the_batch_it_interrupts(self):
        """`SIGTERM` arriving during the first spawn must stop the rest of that batch. Read
        once before iterating, this program announces that nothing more will be claimed and
        then fills the whole configured capacity anyway."""
        self.drive()
        run = self.run_object(capacity=8, poll=0.01)
        started = []

        def spawning(self_run, unit, probe=False):
            started.append(unit["id"])
            self_run.draining = True     # the signal lands inside the first claim
            # True, as a real claim reports: a stub that said otherwise would stop the
            # batch through the caller's abort branch and this would pass whether or not
            # the stop condition is asked before each claim at all.
            return True

        with mock.patch.object(driver.Run, "spawn", spawning):
            run.dispatch_round((review_panel.READER_KIND, review_panel.AUDITOR_KIND,
                                review_panel.PROBE_KIND))
        self.assertEqual(len(started), 1,
                         f"a drained run claimed {len(started)} units in one batch")

    def test_a_drain_during_preparation_claims_nothing(self):
        """Moving the working copy before the claim closed one window and opened another:
        the copy can take minutes, so a stop request arriving inside it was answered by
        claiming and launching a worker that then ran for its whole deadline. The real
        preparation runs here — a test that replaces `spawn()` wholesale cannot see this
        window at all, which is why the batch test missed it."""
        self.drive()
        run = self.run_object(capacity=4, poll=0.01)
        prepared = []
        real = driver.prepare_worker_paths

        def preparing(rundir, unit, token):
            made = real(rundir, unit, token)
            prepared.append(token)
            run.draining = True      # the request lands inside the copy
            return made

        with mock.patch.object(driver, "prepare_worker_paths", preparing):
            run.dispatch_round((review_panel.READER_KIND, review_panel.AUDITOR_KIND,
                                review_panel.PROBE_KIND))
        self.assertEqual(len(prepared), 1, "the batch carried on after the stop request")
        self.assertEqual(list(self.rundir.glob("dispatch/*/a*")), [],
                         "a worker was claimed and launched after the run was asked to stop")
        self.assertEqual(self.stub_invocations(), 0)
        # And the preparation it abandoned is taken back, rather than left to accumulate.
        for token in prepared:
            for where in ("in", "out", "work"):
                self.assertFalse((self.rundir / where / token).exists(), where)

    def test_a_full_disk_during_preparation_is_a_resumable_stop_not_a_refusal(self):
        """The copy of the snapshot is the largest write this program makes, so the disk
        filling inside it is the ordinary way a run meets ENOSPC. Reported as a refusal
        (exit 2) it read as "do not retry"; it is the volume's failure and resumable (exit 3):
        free space, run the same command again. Any other OSError keeps its refusal."""
        self.drive()
        run = self.run_object()
        kinds = (review_panel.READER_KIND, review_panel.AUDITOR_KIND, review_panel.PROBE_KIND)

        def full(rundir, unit, token):
            raise OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(driver, "prepare_worker_paths", full):
            with self.assertRaises(driver.StorageFault):
                run.dispatch_round(kinds)

        def denied(rundir, unit, token):
            raise OSError(errno.EACCES, "Permission denied")
        run = self.run_object()
        with mock.patch.object(driver, "prepare_worker_paths", denied):
            with self.assertRaises(driver.DriverError) as ctx:
                run.dispatch_round(kinds)
        self.assertNotIsInstance(ctx.exception, driver.RunPaused)

    def test_an_aborted_spawn_says_so_to_its_caller(self):
        """A spawn that stops on a drain request is not work in flight and not work
        refused — it is work not started, and the caller has to be able to tell it from a
        claim. Asserted on the return value itself, because at the loop level an abort and
        a stop at the next unit look the same from outside."""
        self.drive()
        run = self.run_object()
        real = driver.prepare_worker_paths

        def preparing(rundir, unit, token):
            made = real(rundir, unit, token)
            run.draining = True
            return made

        with mock.patch.object(driver, "prepare_worker_paths", preparing):
            claimed = run.spawn(run.unit_row("area-01-A1"))
        self.assertIs(claimed, False, "an aborted spawn reported itself as a claim")
        self.assertEqual(driver.attempt_dirs(self.rundir, "area-01-A1"), [])

    def test_a_drain_request_file_stops_claiming_on_every_platform(self):
        """The portable half of the drain contract. Windows delivers no POSIX signal, so a
        handler-based drain works on some of the supported platforms and not others; the
        file works on all of them and means the same thing."""
        self.write_control(sleep=0.5)
        flag = Path(str(self.rundir) + driver.DRAIN_SUFFIX)
        proc = self.launch("--go")
        deadline = time.time() + 120
        while time.time() < deadline and not list(self.rundir.glob("dispatch/*/a0/argv.json")):
            time.sleep(0.05)
        flag.write_text("", encoding="utf-8")   # the request, while the run is going
        out, _ = proc.communicate(timeout=180)
        self.assertEqual(proc.returncode, driver.EXIT_OK, out)
        self.assertIn("draining", out)
        self.assertIn(flag.name, out)
        self.assertEqual(
            json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))["stage"],
            review_panel.READING_STAGE, "a drained run rolled into the next round")
        # The request stands until somebody resumes, and the resume is what clears it —
        # left in place it would drain every later run the moment it started.
        self.assertTrue(flag.exists())
        self.write_control()
        self.drive("--go")
        self.assertFalse(flag.exists(), "a resumed run kept draining on an old request")
        self.assertEqual(
            json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))["stage"],
            review_panel.REPORTED_STAGE)

    def test_a_request_made_while_the_run_is_planning_survives(self):
        """Clearing the stale request is right; clearing it late is not. Planning copies a
        whole snapshot, so a request made during that is a request against THIS driver —
        and cleared afterwards it is deleted and the work dispatched anyway. Ownership is
        the line: before it a request is somebody else's run's, after it this one's.

        The window is inside planning, which is why waiting for `argv.json` cannot see it."""
        import io
        flag = Path(str(self.rundir) + driver.DRAIN_SUFFIX)
        real = driver._engine_stage

        def planning(*argv):
            if argv and argv[0] == "plan":
                flag.write_text("", encoding="utf-8")   # the operator, mid-plan
            return real(*argv)

        out = io.StringIO()
        with mock.patch.object(driver, "_engine_stage", planning), \
                contextlib.redirect_stdout(out):
            code = driver.main(["run", "--job", str(self.job_path),
                                "--rundir", str(self.rundir),
                                "--adapter", str(self.adapter_path), "--go",
                                "--poll", "0.1"])
        self.assertEqual(code, driver.EXIT_OK, out.getvalue())
        self.assertIn("draining", out.getvalue())
        self.assertTrue(flag.exists(), "the request this driver was given was deleted")
        self.assertEqual(self.stub_invocations(), 0,
                         "a run dispatched work after being asked to stop")
        self.assertEqual(list(self.rundir.glob("dispatch/*/a*")), [])

    def test_a_new_run_directory_under_a_parent_that_does_not_exist_yet_is_accepted(self):
        """The lock is the run directory's sibling, and bootstrap -- which makes the run
        directory -- runs under it. A fresh `--rundir reviews/run1` with no `reviews/` yet
        was refused at "cannot open the run lock" before anything could create it."""
        import io
        rundir = self.tmp / "reviews" / "run1"
        self.assertFalse(rundir.parent.exists())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = driver.main(["run", "--job", str(self.job_path), "--rundir", str(rundir),
                                "--adapter", str(self.adapter_path), "--poll", "0.1"])
        self.assertNotIn("cannot open the run lock", err.getvalue() + out.getvalue())
        self.assertTrue(rundir.parent.is_dir(), "the parent was not made for the lock")
        self.assertNotEqual(code, driver.EXIT_REFUSED, err.getvalue() + out.getvalue())

    @unittest.skipUnless(os.name == "posix", "a directory symlink is a POSIX shape here")
    def test_the_documented_request_path_works_for_the_spelling_given(self):
        """Ownership canonicalizes the run directory, which is right. But a suffix appended
        to an alias names a different sibling rather than the alias's target, so an operator
        following `<rundir>.drain` for the `--rundir` they typed creates a file the
        canonical watcher never looks at. Both are watched, and the canonical one is printed
        before it matters."""
        self.drive()
        alias = self.tmp / "alias"
        os.symlink(self.rundir, alias)
        self.write_control(sleep=0.5)
        proc = subprocess.Popen(
            [sys.executable, str(_DRIVER), "--job", str(self.job_path),
             "--rundir", str(alias), "--adapter", str(self.adapter_path), "--go",
             "--poll", "0.1", "--capacity", "2"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace")
        self.addCleanup(self._reap, proc)
        deadline = time.time() + 120
        while time.time() < deadline and not list(self.rundir.glob("dispatch/*/a0/argv.json")):
            time.sleep(0.05)
        # The documented path for the spelling the operator used, not the canonical one.
        (self.tmp / ("alias" + driver.DRAIN_SUFFIX)).write_text("", encoding="utf-8")
        out, _ = proc.communicate(timeout=180)
        self.assertEqual(proc.returncode, driver.EXIT_OK, out)
        self.assertIn("draining", out)
        self.assertIn("alias" + driver.DRAIN_SUFFIX, out)
        # And the canonical path was named where an operator would see it in time.
        # BOTH sides resolved. The driver prints the canonical path deliberately — ownership
        # canonicalizes — and `self.rundir` is whatever the temporary directory was spelled
        # as, which on macOS is `/var/...` for a `/private/var/...` run and on Windows an
        # 8.3 short name. Asserting the unspelled form tests the host, not the driver.
        canonical = self.rundir.resolve()
        self.assertIn(f"To stop this run: create {canonical}{driver.DRAIN_SUFFIX}", out)
        # A resume clears the spelling it is given, or the request would drain it at once.
        # Resumed through the same alias, because a driver only knows the spelling it was
        # handed: the canonical run never watches `alias.drain`, so a canonical resume
        # leaves it inert rather than obeying it.
        self.write_control()
        resumed = subprocess.run(
            [sys.executable, str(_DRIVER), "--job", str(self.job_path),
             "--rundir", str(alias), "--adapter", str(self.adapter_path), "--go",
             "--poll", "0.1", "--capacity", "2"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=300)
        self.assertEqual(resumed.returncode, driver.EXIT_OK,
                         resumed.stdout + resumed.stderr)
        self.assertFalse((self.tmp / ("alias" + driver.DRAIN_SUFFIX)).exists(),
                         "a resume left the request behind to drain the next run")
        self.assertEqual(
            json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))["stage"],
            review_panel.REPORTED_STAGE)

    @unittest.skipUnless(os.name == "posix", "a directory symlink is a POSIX shape here")
    def test_status_names_the_request_path(self):
        """**Asked through an alias on purpose.** The path `status` prints is the canonical
        one, and on a host whose temporary directory is not a link the two spellings are the
        same string — so the assertion would pass without the driver canonicalizing anything
        and only macOS and Windows would ever see it fail. Through a link they differ
        everywhere, and both sides are resolved."""
        self.drive()
        alias = self.tmp / "alias"
        os.symlink(self.rundir, alias)
        proc = subprocess.run([sys.executable, str(_DRIVER), "status", str(alias)],
                              capture_output=True, encoding="utf-8", errors="replace",
                              timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        canonical = self.rundir.resolve()
        self.assertNotEqual(str(canonical), str(alias),
                            "the two spellings are identical, so this asserts nothing")
        self.assertIn(f"create {canonical}{driver.DRAIN_SUFFIX}", proc.stdout)
        # The negative bites on every host, including one where the canonical spelling and
        # `self.rundir` happen to be the same string: printing the path it was GIVEN would
        # send an operator to a file this run does not watch from `status`.
        self.assertNotIn(f"create {alias}{driver.DRAIN_SUFFIX}", proc.stdout)

    def test_a_spent_budget_stops_claiming_and_extend_releases_it(self):
        # Exactly zero, not a very small number: the budget is a stop-CLAIMING threshold
        # checked once per pass, so a threshold of a fraction of a millisecond is one a
        # pass can legitimately start a wave of work inside.
        proc = self.drive("--go", "--max-hours", "0", expect=driver.EXIT_STOPPED)
        self.assertIn("time budget", proc.stdout)
        self.assertEqual(self.stub_invocations(), 0, "a spent budget still claimed work")
        spent = json.loads((self.rundir / "budget.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(spent["seconds"], 0.0)
        # **The limit is KEPT on the resume.** Dropping it removes the ceiling rather than
        # testing the extension, so the old shape would have passed with `--extend` doing
        # nothing at all. An hour of credit against a limit of nothing is an hour.
        self.drive("--go", "--max-hours", "0", "--extend", "1")
        self.assertEqual(
            json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))["stage"],
            review_panel.REPORTED_STAGE)
        budget = json.loads((self.rundir / "budget.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(budget["extended"], 3600.0, places=1,
                               msg="the extension was not persisted as credit of its own")

    def test_an_extension_larger_than_the_spend_is_granted_whole(self):
        """Taken off the accumulated spend and clamped at zero, an extension can never be
        worth more than what has already been spent: an hour spent and two hours granted
        bought one hour."""
        self.plan_only()
        import io
        obj = driver.Run(self.rundir, driver.load_adapter_config(self.adapter_path),
                         _SUPERVISOR, out=io.StringIO(), max_hours=1.0)
        obj._budget_base = 1.0          # a second spent, against a limit of an hour
        obj.extend(2.0)
        self.assertAlmostEqual(obj._extended, 7200.0, places=1)
        self.assertFalse(obj.over_budget())
        obj._budget_base = 1.0 + 3600.0 + 7100.0
        self.assertFalse(obj.over_budget(), "credit granted was not credit available")
        obj._budget_base = 1.0 + 3600.0 + 7300.0
        self.assertTrue(obj.over_budget())

    def test_a_quarantined_unit_stops_the_run_and_is_named(self):
        """An attempt whose outcome cannot be proven is never re-spawned and never landed,
        so the round cannot complete and the run stops for reconciliation. Built as a claim
        with no spawn record — the state a kill between the two leaves — because that is
        the one an operator actually meets."""
        self.drive()
        (self.rundir / "dispatch" / "area-01-A1" / "a0").mkdir(parents=True)
        proc = self.drive("--go", expect=driver.EXIT_STOPPED)
        self.assertIn("stopped: area-01-A1", proc.stdout)
        self.assertIn("orphan-claim", proc.stdout)
        self.assertIn("resumable", proc.stdout)
        self.assertFalse((self.rundir / "units" / "area-01-A1" / "error.txt").exists(),
                         "a unit nobody can account for was landed as failed")
        self.assertEqual(len(driver.attempt_dirs(self.rundir, "area-01-A1")), 1,
                         "a unit with an unaccountable attempt was spawned again")

    def test_an_attempt_that_can_never_report_is_resolved_and_the_run_finishes(self):
        """The state a kill between an attempt's record and its launch leaves: a complete
        `argv.json` and no process, so nothing will ever write its status. The driver holds
        it as `running` until its deadline and grace pass, then quarantines its unit and
        stops — it is never re-spawned and never landed, because its outcome cannot be
        proven. This is the documented way out, end to end: the operator attests that
        nothing is running, and the resume finishes the run."""
        self.write_adapter(deadline=5.0)
        self.drive()
        # A record with no process behind it, spawned far enough in the past that its own
        # deadline and the grace have gone by.
        self.attempt("area-01-A1", 0, spawn_time=time.time() - 3600, deadline=5.0)
        stopped = self.drive("--go", "--grace", "60", expect=driver.EXIT_STOPPED)
        self.assertIn("stopped: area-01-A1", stopped.stdout)
        self.assertIn("uncertain", stopped.stdout)
        self.assertEqual(self.unaccountable(grace=60.0), [("area-01-A1", "a0")])
        driver.resolve_attempt(self.rundir, "area-01-A1", "a0", action="fail",
                               reason="nothing was ever started", stopped_confirmed=True,
                               grace=60.0)
        self.drive("--go", "--grace", "60")
        doc = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["stage"], review_panel.REPORTED_STAGE)
        self.assertTrue((self.rundir / "units" / "area-01-A1" / "error.txt").exists(),
                        "the resolved unit landed no answer at all")
        self.assertEqual(len(driver.attempt_dirs(self.rundir, "area-01-A1")), 1,
                         "an attempt nobody could account for was spawned again")

    def test_a_slot_held_by_an_unaccountable_attempt_stops_rather_than_spinning(self):
        """The reservation an `uncertain` or `orphan-claim` attempt keeps is one nothing
        releases without an operator. At a capacity of one that leaves other units of the
        same slot eligible and permanently unable to start — so a loop that waits for "no
        unit is eligible" waits for ever, on a run that should stop and name the attempt
        nobody can account for."""
        self.drive()
        (self.rundir / "dispatch" / "area-01-A1" / "a0").mkdir(parents=True)
        proc = self.drive("--go", "--capacity", "1", expect=driver.EXIT_STOPPED, timeout=90)
        self.assertIn("stopped: area-01-A1", proc.stdout)
        # The other slot-A units are open, not failed: nothing was landed for them.
        for unit_id in ("audit-area-01-A", "probe-A"):
            self.assertIsNone(driver.landed(self.rundir, unit_id), unit_id)

    def test_a_run_whose_replies_are_refused_lands_errors_and_carries_on(self):
        """A unit that spends its allowances lands `error.txt` and the round completes with
        it — the engine reports failed units. Only a QUARANTINED unit stops the run
        mid-round.

        Here EVERY reply is refused, so the reading round completes with an error for every
        unit, and a round that raised nothing leaves every later round with no unit in it at
        all. Nothing more is spawned and the run ends in the refusal — asserted on what was
        SPAWNED rather than on the marker, which says only how many empty stages the engine
        walked through afterwards.
        """
        self.write_control(mode="refused")
        proc = self.drive("--go", expect=driver.EXIT_REFUSED)
        reading = {unit["id"] for unit in json.loads(
            (self.rundir / "units.json").read_text(encoding="utf-8"))["units"]
            if unit["kind"] in (review_panel.READER_KIND, review_panel.AUDITOR_KIND,
                                review_panel.PROBE_KIND)}
        self.assertTrue(list(self.rundir.glob("units/*/error.txt")))
        self.assertIn("landed no unit", proc.stderr)
        self.assertEqual(
            [], sorted({path.parts[-3] for path in self.rundir.glob("dispatch/*/*/argv.json")}
                       - reading),
            "a unit outside the reading round was spawned after it failed whole")

    def test_a_dead_slot_is_refused_before_the_rounds_that_cannot_rescue_it(self):
        """**The refusal costs what it has to and nothing more.** A slot whose every unit
        failed cannot be brought back by clustering or synthesis — neither round is
        addressed to it, and neither changes what it landed — so spending two more rounds
        of model time before saying so buys the operator nothing.

        Slot A answers throughout and every one of slot B's replies is refused, so each of
        its units spends its allowances and publishes an error. The verification round still
        runs, because every candidate A raised is addressed to B and B is where a recovery
        would show: that is the case the next test pins.
        """
        mode = self.failing_slot()
        self.write_adapter(B={"read_only": mode, "write_capable": mode})
        proc = self.drive("--go", expect=driver.EXIT_REFUSED)
        self.assertIn("slot B landed no unit", proc.stderr)
        self.assertEqual(
            [], list(self.rundir.glob(f"dispatch/{review_panel.CLUSTER_UNIT_PREFIX}*/*/argv.json")),
            "a clustering unit was dispatched after the run could no longer be reported")
        self.assertFalse((self.rundir / "units" / review_panel.SYNTHESIS_UNIT_ID).exists(),
                         "the synthesis round was planned for a run already past saving")
        self.assertEqual(
            json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))["stage"],
            review_panel.CLUSTERED_STAGE,
            "the verification round did not finish, so the refusal came too early")

    def test_a_run_killed_between_reading_and_routing_resumes_to_a_report(self):
        """**The same recovery, across a kill.** A resumed run enters the loop at the
        boundary the kill left it on, and at that boundary the reading round is finished and
        `route` has not run — so the verification unit that answers for the silent slot does
        not exist yet. A guard that reads the units as they stand there sees a slot with
        nothing landed and nothing pending and strands a run one step from the round that
        rescues it, which no uninterrupted run ever reveals.
        """
        mode = self.failing_slot(refuse_for=["findings"])
        self.write_adapter(B={"read_only": mode, "write_capable": mode})
        self.plan_only()
        killed = self.run_object(poll=0.05)
        # The kill lands where the marker still says `reading` and every reading unit has
        # published: the engine stage that would commit the round is the call being cut.
        with mock.patch.object(driver, "_engine_stage",
                               side_effect=KeyboardInterrupt("killed before route")):
            with self.assertRaises(KeyboardInterrupt):
                killed.loop()
        # A real kill takes this process down and the workers' exit statuses with it. Here
        # the loop is cut inside a live interpreter, so its finished children are collected
        # by hand rather than left for the garbage collector to warn about.
        for child in killed._children:
            child.wait()
        doc = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["stage"], review_panel.READING_STAGE,
                         "the kill did not land before the round was committed")
        self.assertEqual(
            [], [unit["id"] for unit in doc["units"]
                 if driver.landed(self.rundir, unit["id"]) is None],
            "the kill landed before the reading round finished, which is a different case")
        self.assertEqual(
            [], [unit["id"] for unit in doc["units"]
                 if unit["slot"] == "B"
                 and (driver.landed(self.rundir, unit["id"]) or {}).get("publication")
                 == "result"],
            "slot B answered a reading unit, so this run is not the case being pinned")
        # Resumed, with nothing patched: the same run directory has to reach its report.
        self.assertEqual(self.run_object(poll=0.05).loop(), driver.EXIT_OK)
        self.assertEqual(
            json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))["stage"],
            review_panel.REPORTED_STAGE)

    def test_a_slot_silent_in_reading_is_not_refused_while_a_verifier_could_land(self):
        """The other half, and the one that decides where the check goes. A slot whose
        readers all failed has landed nothing at the end of the reading round — and the
        verification round is exactly where it gets another unit, because every candidate
        the other slot raised is addressed to it. Refusing on "landed nothing" alone would
        end that run before the round that rescues it.
        """
        # Every unit answering `findings` — B's readers and its auditors — comes back
        # unusable; its verifiers, which answer `verdicts`, come back clean.
        mode = self.failing_slot(refuse_for=["findings"])
        self.write_adapter(B={"read_only": mode, "write_capable": mode})
        proc = self.drive("--go")
        self.assertIn("reported:", proc.stdout)
        record = review_panel.parse_dispatch(json.loads(
            (self.rundir / review_panel.DISPATCH_FILE_NAME).read_text(encoding="utf-8")))
        self.assertIn(record.rung, review_panel.RUNGS)


# --------------------------------------------------------------------------- #
# 6. providers — one incident per generation, a pause, a probe, no fallback
# --------------------------------------------------------------------------- #
OUTAGE = "usage limit reached for this account"


class _Providers(_Case):
    """Two slots on one account, both declaring what that provider's refusal looks like.

    One account on purpose: a provider is what refuses, not a slot, so the two have to pause
    together and these are the tests that would notice if they did not.
    """

    ACCOUNT = "one-account"

    def setUp(self):
        super().setUp()
        self.write_adapter(
            A={"account": self.ACCOUNT, "provider_fault_patterns": ["usage limit"]},
            B={"account": self.ACCOUNT, "provider_fault_patterns": ["usage limit"]})
        self.fake_units(("u1", "A"), ("u2", "A"), ("u3", "B"))
        for unit in ("u1", "u2", "u3"):
            (self.rundir / "units" / unit).mkdir(parents=True, exist_ok=True)

    def failing(self, unit, index, *, detail=OUTAGE, generation=0, probe=False, slot="A",
                reason="reviewer exited 1", spawn_time=None, source="message"):
        """One attempt that reported a terminal failure, described the way the supervisor
        describes one: `reason` is the supervisor's own account of what it saw,
        `terminal_detail` is the reviewer's, and `terminal_detail_source` says which of the
        supervisor's three extraction rules produced it."""
        status = {"status": "error", "reason": reason, "terminal_detail": detail,
                  "terminal_detail_source": None if detail is None else source}
        return self.attempt(unit, index, spawn_time=spawn_time,
                            argv={"generation": generation, "probe": probe, "slot": slot,
                                  "account": self.ACCOUNT},
                            status=json.dumps(status))

    def adopted(self, **kw):
        run = self.run_object(probe_backoff=0.0, **kw)
        run.adopt()
        return run

    def state(self, run):
        return run.providers()[self.ACCOUNT]

    def close_generation(self, closed):
        path = self.rundir / "dispatch" / f"{driver.GENERATIONS_PREFIX}{self.ACCOUNT}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"closed": closed}), encoding="utf-8")


class OneIncidentHoweverManyAttemptsReportIt(_Providers):
    """An incident is grouped by the generation an attempt was LAUNCHED in.

    Keying it on the first failing attempt groups nothing: at four hundred units the normal
    case is a batch already in flight when the outage begins, and none of those had an
    incident open to belong to. These are the tests that tell the two designs apart.
    """

    def test_every_attempt_in_flight_when_an_outage_begins_is_one_incident(self):
        for index, unit in enumerate(("u1", "u2", "u3")):
            self.failing(unit, 0, slot="A" if unit != "u3" else "B")
        run = self.adopted()
        state = self.state(run)
        self.assertTrue(state.paused, "three provider refusals paused nothing")
        self.assertEqual(state.generation, 0)
        self.assertEqual(state.drain, "",
                         "later reports of one outage were read as a recurrence")
        for unit in ("u1", "u2", "u3"):
            self.assertEqual(
                [a.disposition["outcome"] for a in run.attempts(unit)],
                [driver.PROVIDER_UNAVAILABLE], unit)

    def test_a_provider_failure_charges_the_unit_nothing(self):
        self.failing("u1", 0)
        run = self.adopted()
        budget = driver.replay([a.disposition for a in run.attempts("u1")], 0)
        self.assertEqual(budget.charging, 0)
        self.assertFalse(budget.exhausted)
        self.assertTrue(run.eligible("u1"),
                        "a unit the provider never answered spent an allowance")

    def test_both_slots_of_one_account_pause_together(self):
        self.failing("u1", 0, slot="A")
        # Capacity 2, so slot A's own limit is not what stops the second unit of this
        # account and the pause is the only thing left that can.
        run = self.adopted(capacity=2)
        reserved, probed, claims = run.reserving(), set(), []
        for unit in run.units():          # exactly what the round's pass does, in order
            claim, probe = run.claim_decision(unit, reserved, 0, run.providers(), probed)
            if not claim:
                continue
            claims.append((unit["id"], probe))
            if probe:
                probed.add(self.ACCOUNT)
            reserved[unit["slot"]] = reserved.get(unit["slot"], 0) + 1
        # One claim across BOTH slots, and it is the probe: the account is what refused, so
        # a unit on the other slot is no more claimable than the one that met the outage.
        self.assertEqual(claims, [("u1", True)], f"the pause let {claims} through")

    def test_a_late_old_generation_failure_is_not_a_recurrence(self):
        """A probe closed generation 0 and the run went on. A failure from an attempt
        launched before that recovery is the closed incident's, however late it lands."""
        self.close_generation(1)
        self.failing("u1", 0, generation=0)
        run = self.adopted()
        state = self.state(run)
        self.assertEqual(state.generation, 1)
        self.assertEqual(state.paused, "", "a closed incident's report re-paused a "
                                           "provider that had recovered")
        self.assertEqual(state.drain, "")

    def test_a_failure_launched_after_the_recovery_is_a_recurrence(self):
        self.close_generation(1)
        self.failing("u1", 0, generation=1)
        run = self.adopted()
        self.assertIn("recurrence", self.state(run).drain)
        self.assertTrue(run.draining, "a recurrence did not stop the run")


class ProbesAskOneQuestionAtATime(_Providers):
    """A paused provider is retried by one probe — an ordinary attempt, marked at spawn.

    The outcomes are not symmetrical and that is the point: an answer of any kind means the
    provider works, the provider refusing the probe means there is nothing to wait for, and
    anything else says nothing at all and is the only case that gets another go.
    """

    def probe(self, unit, index, *, outcome=None, generation=0, spawn_time=None,
              status=True):
        path = self.attempt(
            unit, index, spawn_time=spawn_time, deadline=1.0,
            argv={"generation": generation, "probe": True, "slot": "A",
                  "account": self.ACCOUNT},
            status='{"status": "ok"}' if status else None,
            disposition=None if outcome is None else
            {"attempt": f"a{index}", "outcome": outcome, "intended_publication": "none"})
        return path

    def test_an_answer_of_any_kind_closes_the_generation(self):
        for outcome in (driver.ACCEPTED, driver.REFUSED, driver.NO_REPLY):
            with self.subTest(outcome=outcome):
                self.setUp()
                self.failing("u1", 0)
                self.probe("u2", 0, outcome=outcome)
                run = self.adopted()
                self.assertEqual(run.generation(self.ACCOUNT), 1,
                                 "a probe that got an answer left the incident open")
                self.assertEqual(self.state(run).paused, "")

    def test_a_probe_the_provider_refuses_stops_the_run(self):
        self.failing("u1", 0)
        self.probe("u2", 0, outcome=driver.PROVIDER_UNAVAILABLE)
        run = self.adopted()
        self.assertIn("still unavailable", self.state(run).drain)
        self.assertTrue(run.draining)

    def test_a_launch_failed_probe_says_nothing_and_another_goes_out(self):
        self.failing("u1", 0)
        self.probe("u2", 0, outcome=driver.LAUNCH_FAILED, status=False)
        run = self.adopted()
        state = self.state(run)
        self.assertEqual(state.uninformative_probes, 1)
        self.assertEqual(state.drain, "", "one uninformative probe ended the run")
        self.assertTrue(state.paused, "a probe that said nothing resumed the provider")
        self.assertTrue(driver.probe_due(state, time.time(), 0.0))

    def test_three_probes_that_say_nothing_end_the_run(self):
        self.failing("u1", 0)
        for index, outcome in enumerate((driver.WORKER_FAILED, driver.INFRASTRUCTURE,
                                         driver.LAUNCH_FAILED)):
            self.probe("u2", index, outcome=outcome)
        run = self.adopted()
        self.assertEqual(self.state(run).uninformative_probes, 3)
        self.assertIn("without saying whether", self.state(run).drain)
        self.assertTrue(run.draining)

    def test_an_uncertain_probe_blocks_any_replacement_until_it_is_reconciled(self):
        """It may still be running, so a second probe beside it is the one thing the
        attempt protocol refuses everywhere else. It does not count toward the three
        either, because it never finished."""
        self.failing("u1", 0)
        self.probe("u2", 0, status=False, spawn_time=time.time() - 10_000)
        run = self.adopted()
        state = self.state(run)
        self.assertTrue(state.probe_unaccountable)
        self.assertEqual(state.uninformative_probes, 0,
                         "a probe that never finished was counted as one that answered")
        self.assertFalse(driver.probe_due(state, time.time(), 0.0),
                         "a replacement probe went out beside one that may still be live")
        # Reconciled by the operator, and only then does a replacement become due.
        driver.resolve_attempt(self.rundir, "u2", "a0", action="fail",
                              reason="the host it ran on is gone", stopped_confirmed=True)
        run = self.adopted()
        self.assertFalse(self.state(run).probe_unaccountable)
        self.assertTrue(driver.probe_due(self.state(run), time.time(), 0.0))

    def test_the_backoff_holds_the_round_open_rather_than_calling_it_stuck(self):
        """A paused provider claims nothing and has nothing in flight, which is exactly
        what a finished round looks like. Reported as stuck, every one of its units is
        named as a defect when the truth is that the run is waiting to ask one question."""
        self.failing("u1", 0)
        self.probe("u2", 0, outcome=driver.WORKER_FAILED, spawn_time=time.time())
        run = self.run_object(probe_backoff=10_000.0)
        run.adopt()
        self.assertFalse(run._in_flight(run.units()), "something is still running")
        self.assertTrue(run._waiting_to_probe(run.units()),
                        "the round would end and call every unit of a paused provider "
                        "stuck, while the back-off it is serving has not elapsed")
        # And it is the back-off alone that holds it: due now, the round ends and the
        # claiming pass is what starts the next probe.
        run = self.run_object(probe_backoff=0.0)
        run.adopt()
        self.assertFalse(run._waiting_to_probe(run.units()))


class UnexplainedFailuresPauseAProviderOnce(_Providers):
    """Unclassifiable is not benign. A terminal failure that named nothing is the worker's
    once — two bad answers happen — and the provider's twice, because two failures that
    explain nothing are far likelier to be an outage than two bad replies."""

    def unexplained(self, unit, index, **kw):
        return self.failing(unit, index, detail=None, **kw)

    def test_one_unexplained_failure_is_the_workers(self):
        self.unexplained("u1", 0)
        run = self.adopted()
        self.assertEqual(run.attempts("u1")[0].disposition["outcome"],
                         driver.WORKER_FAILED)
        self.assertEqual(self.state(run).paused, "")

    def test_two_on_one_slot_pause_the_provider(self):
        self.unexplained("u1", 0)
        self.unexplained("u2", 0)
        run = self.adopted()
        self.assertIn("explained nothing", self.state(run).paused)

    def test_the_same_pair_cannot_re_pause_a_provider_that_recovered(self):
        self.unexplained("u1", 0)
        self.unexplained("u2", 0)
        self.adopted()
        self.close_generation(1)
        run = self.adopted()
        self.assertEqual(self.state(run).generation, 1)
        self.assertEqual(self.state(run).paused, "",
                         "a historical pair re-paused a provider that has since recovered")

    def test_a_supervisor_that_was_never_asked_counts_nothing(self):
        """The count is of failures that explained NOTHING, which a status line carrying no
        detail field at all does not say. Counting its silence would pause a provider over
        a flag nobody passed."""
        for index, unit in enumerate(("u1", "u2")):
            self.attempt(unit, 0,
                         argv={"generation": 0, "probe": False, "slot": "A",
                               "account": self.ACCOUNT},
                         status='{"status": "error", "reason": "reviewer exited 1"}')
        run = self.adopted()
        self.assertEqual(self.state(run).paused, "")
        self.assertNotIn("unexplained", run.attempts("u1")[0].disposition)

    def test_an_event_name_is_not_the_failure_explaining_itself(self):
        """The supervisor's last extraction rule answers with the terminal event's TYPE, so
        a failure that wrote its reason to stderr arrives carrying the string
        `turn.failed` — a name, and no account of anything. Read as detail it makes every
        such failure classifiable, the count never reaches two, and the breaker never fires
        on the one shape of outage it was built for: four hundred units charged one at a
        time against an account that is already refusing."""
        for unit in ("u1", "u2"):
            self.failing(unit, 0, detail="turn.failed", source="event-type")
        run = self.adopted()
        self.assertTrue(run.attempts("u1")[0].disposition.get("unexplained"),
                        "an event name was read as the failure's own error text")
        self.assertIn("explained nothing", self.state(run).paused)

    def test_a_detail_the_supervisor_took_from_a_message_still_explains_something(self):
        """The other half, or the rule above would pause a provider over every failure: a
        reviewer that said why it failed said it, and two of those are two bad answers."""
        for unit in ("u1", "u2"):
            self.failing(unit, 0, detail="the reviewer could not parse its own payload",
                         source="message")
        run = self.adopted()
        self.assertNotIn("unexplained", run.attempts("u1")[0].disposition)
        self.assertEqual(self.state(run).paused, "")

    def test_a_declared_pattern_is_what_makes_a_failure_the_providers(self):
        """The wording is the provider's to change, so it lives in the configuration. A
        detail that matches nothing declared is an ordinary worker failure."""
        self.failing("u1", 0, detail="the reviewer could not parse its own payload")
        run = self.adopted()
        self.assertEqual(run.attempts("u1")[0].disposition["outcome"],
                         driver.WORKER_FAILED)
        self.assertEqual(self.state(run).paused, "")


class ADrainedProviderIsRetriedWhenTheRunIsStartedAgain(_Providers):
    """A drain is where this section's rules end, and nothing inside the run can undo one.

    The dispositions that drained it are immutable and belong to a generation only a
    successful probe advances — so a resumed run re-derives the same drain, declines to
    probe, finds nothing eligible and exits 0 having reviewed nothing. **A run that
    silently does nothing and reports success is the failure this phase exists to
    prevent.** Restarting the run is the operator asserting the provider is fixed, which is
    the assertion a probe goes out to test, so the reset is theirs to make and is recorded
    once rather than retaken every poll.
    """

    def drained(self):
        """A paused provider whose probe found it still refusing: the run stops."""
        self.failing("u1", 0)
        self.attempt("u2", 0, argv={"generation": 0, "probe": True, "slot": "A",
                                    "account": self.ACCOUNT},
                     status='{"status": "ok"}',
                     disposition={"attempt": "a0", "outcome": driver.PROVIDER_UNAVAILABLE,
                                  "intended_publication": "none"})
        run = self.adopted()
        self.assertTrue(self.state(run).drain, "the fixture did not stop the run")
        self.assertFalse(driver.probe_due(self.state(run), time.time(), 0.0),
                         "a stopped run would have probed anyway, and this proves nothing")

    def generations_record(self):
        return json.loads(
            (self.rundir / "dispatch" /
             f"{driver.GENERATIONS_PREFIX}{self.ACCOUNT}.json").read_text(encoding="utf-8"))

    def test_a_restart_closes_the_incident_the_last_run_stopped_on(self):
        self.drained()
        fresh = self.run_object(probe_backoff=0.0)
        fresh.reset_drained_providers()
        state = fresh.providers()[self.ACCOUNT]
        self.assertEqual(state.generation, 1)
        self.assertEqual(state.drain, "",
                         "a resume re-derived the drain it was resuming from")
        self.assertEqual(state.paused, "")
        self.assertTrue(fresh.eligible("u1"),
                        "the units of the provider stayed frozen for every later run")

    def test_the_reset_is_a_durable_record_and_not_a_decision_retaken_every_poll(self):
        """Replay has to reach the same state: a reset re-applied on each pass would walk
        the generation forward for as long as the run lasts, and every failure carrying the
        number it left behind would read as a closed incident's."""
        self.drained()
        run = self.run_object()
        run.reset_drained_providers()
        self.assertEqual(self.generations_record()["closed"], 1)
        self.assertIn("started again", self.generations_record()["closed_by"])
        run.reset_drained_providers()
        self.run_object().reset_drained_providers()
        self.assertEqual(self.generations_record()["closed"], 1)
        self.assertEqual(self.run_object().generation(self.ACCOUNT), 1)

    def test_a_provider_that_is_still_down_stops_the_run_again(self):
        """The reset is one more chance, not a loop: a failure launched after it carries the
        new generation, which is a recurrence."""
        self.drained()
        run = self.run_object(probe_backoff=0.0)
        run.reset_drained_providers()
        self.failing("u3", 0, generation=1, slot="B")
        run.adopt()
        self.assertIn("recurrence", run.providers()[self.ACCOUNT].drain)
        self.assertTrue(run.draining)

    def test_the_run_performs_the_reset_before_its_first_round(self):
        """Wired to the loop and nowhere else. Inside it, the same call would undo a
        recurrence as fast as the breaker could find one."""
        self.drained()
        run = self.run_object()
        with mock.patch.object(driver.Run, "_run_rounds",
                               return_value=driver.EXIT_OK) as rounds:
            self.assertEqual(run.loop(), driver.EXIT_OK)
        rounds.assert_called_once()
        self.assertEqual(run.generation(self.ACCOUNT), 1,
                         "the run started without closing the incident it stopped on")


class WhatTheSupervisorCouldNotHold(_Case):
    """`--max-capture-bytes` bounds what one attempt can make the supervisor keep, and the
    two ways that bound can bite are both infrastructure: they charge nothing and they are
    never a unit's answer."""

    def setUp(self):
        super().setUp()
        self.fake_units(("u1", "A"))
        (self.rundir / "units" / "u1").mkdir(parents=True, exist_ok=True)

    def adjudicate(self, status, transcript=None):
        path = self.attempt("u1", 0, status=status, transcript=transcript)
        attempt = driver.read_attempt(self.rundir, "u1", path, grace=120.0)
        return driver.adjudicate(self.run_object(), attempt)

    def test_a_capture_overflow_is_infrastructure_not_the_workers_failure(self):
        record = self.adjudicate(json.dumps({
            "status": "error", "terminal_detail": None,
            "reason": "capture overflow: one output line exceeded --max-capture-bytes"}))
        self.assertEqual(record["outcome"], driver.INFRASTRUCTURE)
        self.assertNotIn("unexplained", record,
                         "a bound this program set was counted against the provider")

    def test_a_reply_whose_closing_object_was_cut_is_infrastructure_not_no_reply(self):
        """Losing later content must never promote an earlier one, and it must not charge
        the unit's reply allowance either: nothing about this is the worker's answer."""
        record = self.adjudicate(
            json.dumps({"status": "ok", "capture_truncated": True}),
            transcript='[review_runner] 9 byte(s) dropped\n\n{"findings": [], "summ')
        self.assertEqual(record["outcome"], driver.INFRASTRUCTURE)

    def test_a_supervisor_that_could_not_start_a_worker_is_infrastructure_and_stops_the_run(self):
        """The slot's command names a program that is not there. The supervisor says so in
        its own words before any worker ran; filed as a worker failure that spent every
        unit's launch allowance on the slot and ended the run in the one refusal that cannot
        be resumed, taking the other slot's finished work with it. Nobody's answer: charged
        to nobody, and the run stops where the adapter can be fixed and the run continued."""
        path = self.attempt("u1", 0, status=json.dumps(
            {"status": "error", "reason": "reviewer CLI not found on PATH: codx"}))
        run = self.run_object()
        attempt = driver.read_attempt(self.rundir, "u1", path, grace=120.0)
        record = driver.adjudicate(run, attempt)
        self.assertEqual(record["outcome"], driver.INFRASTRUCTURE)
        self.assertNotIn("unexplained", record)
        self.assertIn("codx", run.pause_reason, "the run was not stopped with the reason")
        self.assertIn("adapter", run.pause_reason)
        # And a worker that ran and exited 1 is still the worker's own failure.
        path = self.attempt("u1", 1, status=json.dumps({"status": "error", "reason": "reviewer exited 1"}))
        run = self.run_object()
        record = driver.adjudicate(run, driver.read_attempt(self.rundir, "u1", path, grace=120.0))
        self.assertEqual(record["outcome"], driver.WORKER_FAILED)
        self.assertEqual(run.pause_reason, "")

    def test_a_capped_but_intact_reply_is_still_read_as_a_reply(self):
        """The notice is PREPENDED, so a transcript that still ends in its closing object is
        extracted from exactly as an uncapped one is. What this run directory holds no
        `areas.json` for, the engine then refuses — which is an answer about the REPLY, and
        the thing this establishes is that there was one to have an opinion about."""
        reply = '{"findings": [], "summary": "read the area"}'
        record = self.adjudicate(
            json.dumps({"status": "ok", "capture_truncated": True}),
            transcript=f"[review_runner] 9 byte(s) dropped\n\n{reply}")
        self.assertNotIn(record["outcome"], (driver.INFRASTRUCTURE, driver.NO_REPLY),
                         "a truncation notice at the front was read as a lost reply")
        self.assertEqual(
            (self.rundir / "dispatch" / "u1" / "a0" / "reply.json").read_text("utf-8"),
            reply, "the reply was not copied out byte for byte")

    def test_the_driver_asks_its_supervisor_for_both(self):
        """Without the detail a quota refusal arrives as "the reviewer exited 1" and every
        pending unit spends its allowance against an account that is already refusing."""
        run = self.run_object()
        recorded = {}
        with mock.patch.object(driver.subprocess, "Popen",
                               side_effect=OSError("not today")):
            run.spawn({"id": "u1", "kind": "reader", "slot": "A"})
        recorded = json.loads(
            (self.rundir / "dispatch" / "u1" / "a0" / "argv.json").read_text("utf-8"))
        self.assertIn("--status-detail", recorded["argv"])
        self.assertIn("--max-capture-bytes", recorded["argv"])


class AnOutageInARealRound(_Case):
    """The loop, the real supervisor and a provider that stops answering.

    The rules above are decided per attempt and tested per attempt; this is the one that
    asks whether they are wired to anything. It is the case the whole section exists for —
    hundreds of units spending their allowances against an account that is already
    refusing, and a report that then reads as a review which found nothing wrong.
    """

    def test_a_provider_that_refuses_pauses_rather_than_failing_every_unit(self):
        self.write_adapter(result_mode="stream-transcript")
        self.write_control(outage="usage limit reached for this account")
        for slot in review_panel.SLOTS:
            raw = json.loads(self.adapter_path.read_text(encoding="utf-8"))
            raw["slots"][slot]["provider_fault_patterns"] = ["usage limit"]
            self.adapter_path.write_text(json.dumps(raw), encoding="utf-8")
        proc = self.drive("--go", "--probe-backoff", "0", "--capacity", "1",
                          expect=driver.EXIT_OK, timeout=300)
        self.assertIn("is paused", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("draining", proc.stdout)
        # Nothing was charged and nothing was published: every unit is still open, which is
        # what makes the run resumable once the provider is back.
        self.assertEqual(list(self.rundir.glob("units/*/error.txt")), [],
                         "an outage was published as units that failed review")
        outcomes = set()
        for unit in json.loads(
                (self.rundir / "units.json").read_text(encoding="utf-8"))["units"]:
            for attempt in driver.read_attempts(self.rundir, unit["id"], grace=120.0):
                if attempt.disposition:
                    outcomes.add(attempt.disposition["outcome"])
        self.assertEqual(outcomes, {driver.PROVIDER_UNAVAILABLE},
                         f"an outage was adjudicated as something else: {outcomes}")
        # And a probe went out: one attempt, marked at spawn as the question being asked.
        probes = [attempt for unit in driver._units(self.rundir)
                  for attempt in driver.read_attempts(self.rundir, unit["id"], grace=120.0)
                  if (attempt.argv or {}).get("probe")]
        self.assertTrue(probes, "a paused provider was never probed at all")


class ThePathSaysWhatRanIt(_Case):
    """`dispatch.json`: the configuration always, and what executed beside it. The engine
    renders both strings verbatim, so an untruthful one is a sentence in the report."""

    def setUp(self):
        super().setUp()
        self.fake_units(("u1", "A"), ("u2", "B"))
        for unit in ("u1", "u2"):
            (self.rundir / "units" / unit).mkdir(parents=True, exist_ok=True)

    def land(self, unit, index=0):
        self.attempt(unit, index, status='{"status": "ok"}',
                     disposition={"attempt": f"a{index}", "outcome": driver.ACCEPTED,
                                  "intended_publication": "result"})
        (self.rundir / "units" / unit / review_panel.RESULT_NAME).write_text(
            '{"findings": [], "summary": "x"}', encoding="utf-8")
        (self.rundir / "dispatch" / unit / "landed.json").write_text(
            json.dumps({"publication": "result", "attempt": f"a{index}"}), encoding="utf-8")

    def record(self):
        run = self.run_object()
        return driver.dispatch_record(run.slots, driver.executed_provenance(run))

    def refusal(self):
        """The message a run that reached one slot is refused with. It carries the same
        provenance a record would have carried, because that is what says WHICH slot and
        what became of it."""
        with self.assertRaises(driver.DriverError) as ctx:
            self.record()
        return str(ctx.exception)

    def test_a_slot_that_never_executed_says_so_rather_than_saying_nothing(self):
        self.land("u1")
        message = self.refusal()
        self.assertIn("configured, never executed", message)
        self.assertIn("stub-model-B", message,
                      "the configured model went missing with the attempts")

    def test_a_slot_whose_every_attempt_failed_is_not_a_slot_that_landed(self):
        self.land("u1")
        for index in range(3):
            self.attempt("u2", index, status='{"status": "error"}',
                         disposition={"attempt": f"a{index}",
                                      "outcome": driver.WORKER_FAILED,
                                      "intended_publication": "none"})
        message = self.refusal()
        self.assertIn("3 attempt(s) executed, 0 unit(s) landed", message)

    def test_a_run_that_reached_one_slot_is_refused_and_names_the_slot(self):
        """**One usable slot is a refusal, not a rung.** Rule 3 is that a finding goes to a
        unit that did not raise it; a run whose second slot landed nothing checked every
        finding where it was raised. There is no sentence a report could write about that
        which is worth reading, so the run is refused by name and nothing is rendered."""
        self.land("u1")
        message = self.refusal()
        self.assertIn("B", message)
        self.assertIn("landed no unit", message)
        self.assertNotIn("rung", message.lower(),
                         "the refusal offered a rung for the thing that has none")

    def test_both_slots_landing_is_what_a_record_needs(self):
        """The other half of the same rule: the refusal is keyed to what LANDED, so a run
        where both slots answered a unit still gets its record."""
        self.land("u1")
        self.land("u2")
        review_panel.parse_dispatch(self.record())

    def test_a_launch_that_never_started_is_not_an_execution(self):
        """`argv.json` is written BEFORE the launch, so an attempt whose supervisor could
        not be started leaves a complete spawn record and ran nothing. Counted as an
        execution, a slot whose every launch raised reads as a slot that worked — and §6.4
        asks that exact slot to read as configured and never executed."""
        self.land("u1")
        for index in range(2):
            self.attempt("u2", index,
                         disposition={"attempt": f"a{index}", "unit": "u2",
                                      "outcome": driver.LAUNCH_FAILED,
                                      "intended_publication": "none"})
        message = self.refusal()
        self.assertIn("configured, never executed", message)
        self.assertIn("2 launch(es) never started", message)
        self.assertIn("stub-model-B", message)

    def test_an_attempt_nobody_can_show_started_is_not_an_execution_either(self):
        """A kill between the spawn record and the launch leaves a complete `argv.json`, no
        status, and an attempt an operator eventually fails. Counted as an execution — which
        every prepared attempt but an explicit `launch-failed` was — the run reports a
        supervisor that started nothing as one that ran. §6.4 asks for proven execution, so
        what cannot be proved is counted as itself.
        """
        self.land("u1")
        self.attempt("u2", 0, resolution={"action": "fail", "reason": "nobody could say",
                                          "stopped_confirmed": False},
                     disposition={"attempt": "a0", "unit": "u2",
                                  "outcome": driver.OPERATOR_FAILED,
                                  "intended_publication": "error"})
        ran = driver.executed_provenance(self.run_object())["B"]
        self.assertEqual((ran.prepared, ran.executed, ran.unknown, ran.landed),
                         (1, 0, 1, 0))
        message = self.refusal()
        self.assertIn("configured, never executed", message)
        self.assertIn("no record of whether the supervisor started", message)
        self.assertNotIn("1 launch(es) never started", message,
                         "an attempt nobody could account for was asserted not to have run")

    def test_a_status_record_is_what_proves_an_execution(self):
        """The other half: the supervisor's own record is proof it ran, whatever the
        adjudication made of it, so an ordinary failed attempt still counts as executed."""
        self.land("u1")
        self.attempt("u2", 0, status='{"status": "error"}',
                     disposition={"attempt": "a0", "unit": "u2",
                                  "outcome": driver.WORKER_FAILED,
                                  "intended_publication": "none"})
        ran = driver.executed_provenance(self.run_object())["B"]
        self.assertEqual((ran.executed, ran.unknown), (1, 0))
        self.assertNotIn("no record of whether", self.refusal())

    def test_a_published_error_is_not_a_unit_that_landed(self):
        """`error.txt` is a publication and it names the attempt that produced it. Read as a
        landing it says this slot answered a unit, and the run would be reported as two
        independent readings where one slot produced no answer at all."""
        self.land("u1")
        self.attempt("u2", 0, status='{"status": "error"}',
                     disposition={"attempt": "a0", "unit": "u2",
                                  "outcome": driver.WORKER_FAILED,
                                  "intended_publication": "error"})
        (self.rundir / "units" / "u2" / review_panel.ERROR_NAME).write_text(
            "this unit spent its allowances\n", encoding="utf-8")
        (self.rundir / "dispatch" / "u2" / "landed.json").write_text(
            json.dumps({"publication": "error", "attempt": "a0"}), encoding="utf-8")
        message = self.refusal()
        self.assertIn("1 attempt(s) executed, 0 unit(s) landed", message)
        self.assertIn("landed no unit", message)

    def test_two_slots_that_landed_keep_the_rung_they_were_configured_at(self):
        self.land("u1")
        self.land("u2")
        record = self.record()
        self.assertEqual(record["rung"], review_panel.RUNG_TWO_RUNTIMES)
        review_panel.parse_dispatch(record)

    def test_both_permission_modes_are_stated_and_the_unused_one_says_so(self):
        """Readers run read-only and a verifier runs in a writable copy, so one winner's
        permission cannot stand for the slot."""
        self.land("u1")
        self.land("u2")
        permission = self.record()["slots"]["A"]["permission"]
        self.assertIn("read-only units", permission)
        self.assertIn("write-capable units", permission)
        self.assertIn("(none ran)", permission,
                      "a slot that never ran a write-capable unit claimed it had")

    def test_two_models_on_one_runtime_are_refused_before_anything_is_planned(self):
        """The report describes a one-runtime run as checked by the same model, and that
        sentence would be false. Refused at startup, so the refusal does not arrive after a
        whole reading round has been spent."""
        shutil.rmtree(self.rundir)
        self.write_adapter(A={"runtime": "one-runtime"},
                           B={"runtime": "one-runtime", "model": "a different model"})
        proc = self.drive(expect=driver.EXIT_REFUSED)
        self.assertIn("different models", proc.stderr)
        self.assertFalse(self.rundir.exists(), "a refused configuration planned a run")
        self.assertFalse(self.rundir.with_name(self.rundir.name + ".partial").exists())


# --------------------------------------------------------------------------- #
# 7.1-7.2 storage — a fault of the volume is never a unit's answer
# --------------------------------------------------------------------------- #
ENOSPC_TEXT = "[Errno 28] No space left on device"


class AStorageFaultNeverAdjudicatesAUnit(_Case):
    """A full disk looks exactly like a failed worker, and charged as one it becomes the
    unit's answer: four of them publish "this unit failed" and the report reads as a review
    that found nothing there.

    So every write boundary is asked separately — the supervisor's transcript and status,
    and this program's own `argv.json`, disposition, breaker, budget, landing and marker.
    Each pauses the run, exits non-zero and resumable, and decides nothing.
    """

    def setUp(self):
        super().setUp()
        self.fake_units(("u1", "A"), ("u2", "B"))
        for unit in ("u1", "u2"):
            (self.rundir / "units" / unit).mkdir(parents=True, exist_ok=True)
        self.fake_snapshot()

    def failing_write(self, match, text=False):
        """Every write whose path holds `match` fails the way a full volume fails one.

        The engine's writers wrap the `OSError` in a refusal of their own, which is what
        this program actually catches, so the fake raises the wrapper rather than the bare
        error — the shape a real ENOSPC takes on the way here.
        """
        name = "_engine_write_text" if text else "_engine_write_json"
        real = getattr(driver, name)

        def fake(path, payload):
            if match in str(path):
                raise review_panel.InventoryError(f"cannot write {path}: {ENOSPC_TEXT}")
            return real(path, payload)

        return mock.patch.object(driver, name, fake)

    def accepted_attempt(self, unit, index=0):
        path = self.attempt(unit, index, status='{"status": "ok"}',
                            disposition={"attempt": f"a{index}", "unit": unit,
                                         "outcome": driver.ACCEPTED,
                                         "intended_publication": "result"})
        (path / "reply.json").write_text('{"findings": [], "summary": "x"}',
                                         encoding="utf-8")
        return path

    # -- the supervisor's side ---------------------------------------------- #
    def test_a_full_disk_in_the_supervisor_is_infrastructure_and_charges_nothing(self):
        """Injecting ENOSPC into the supervisor's transcript publication reaches this
        program as `status: error, reason: routing failed: [Errno 28]…`. Charged as a worker
        failure it would spend the unit's allowance on the host running out of room."""
        self.attempt("u1", 0, status=json.dumps(
            {"status": "error", "reason": f"routing failed: {ENOSPC_TEXT}"}))
        run = self.run_object()
        run.adopt()
        record = driver.read_attempts(self.rundir, "u1", grace=120.0)[0].disposition
        self.assertEqual(record["outcome"], driver.INFRASTRUCTURE)
        self.assertTrue(record["storage_fault"])
        budget = driver.replay([record], 0)
        self.assertEqual(budget.charging, 0)
        self.assertFalse(budget.exhausted)
        self.assertIsNone(driver.landed(self.rundir, "u1"),
                          "a storage fault became the unit's answer")
        self.assertTrue(run.pause_reason, "a storage fault did not pause the run")
        self.assertTrue(run.draining, "a paused run went on claiming")

    def test_storage_is_asked_before_the_provider_patterns(self):
        """A message can match both, and the order decides which. Read as the provider's,
        a full disk pauses an account, spends the run's three probes against it and drains
        — all while the thing to fix is the volume."""
        self.write_adapter(
            A={"account": "acct", "provider_fault_patterns": ["usage limit"]},
            B={"account": "acct", "provider_fault_patterns": ["usage limit"]})
        self.attempt("u1", 0, argv={"account": "acct", "slot": "A"}, status=json.dumps({
            "status": "error", "reason": "reviewer exited 1",
            "terminal_detail": f"usage limit note while writing: {ENOSPC_TEXT}"}))
        run = self.run_object()
        run.adopt()
        record = driver.read_attempts(self.rundir, "u1", grace=120.0)[0].disposition
        self.assertEqual(record["outcome"], driver.INFRASTRUCTURE,
                         "a full disk was read as the provider refusing")
        self.assertEqual(run.providers()["acct"].paused, "",
                         "a full disk paused a provider that never said anything")

    def test_a_read_that_failed_while_checking_a_reply_is_not_a_rejected_reply(self):
        """An ok status, a transcript ending in its closing object — and the check itself
        meets the disk: `check_result` reads `units.json` and the unit's own records, and an
        EIO on any of them arrives as the engine's refusal, which is the same exception type
        as a reply the engine rejected. Recorded as a refusal it spends the reply allowance,
        and twice it publishes `error.txt`: the volume's fault becomes the unit's answer.
        """
        self.attempt("u1", 0, status='{"status": "ok"}',
                     transcript='{"findings": [], "summary": "read the area"}')
        cause = OSError(errno.EIO, "Input/output error",
                        str(self.rundir / "units.json"))
        refusal = review_panel.ReviewPanelError(f"cannot read the unit listing: {cause}")
        refusal.__cause__ = cause
        run = self.run_object()
        with mock.patch.object(review_panel, "check_result", side_effect=refusal):
            run.adopt()
        record = driver.read_attempts(self.rundir, "u1", grace=120.0)[0].disposition
        self.assertEqual(record["outcome"], driver.INFRASTRUCTURE,
                         "a failed read was charged to the reply it was checking")
        self.assertTrue(record["storage_fault"])
        budget = driver.replay([record], 0)
        self.assertEqual(budget.reply_charges, 0)
        self.assertEqual(budget.charging, 0)
        self.assertIsNone(driver.landed(self.rundir, "u1"),
                          "a storage fault became the unit's answer")
        self.assertTrue(run.pause_reason, "a storage fault did not pause the run")

    def test_a_reply_the_engine_rejects_is_still_a_refusal(self):
        """The other half of the rule above: only the disk's failures are the disk's, and a
        reply the engine read and refused still spends the allowance it always did."""
        self.attempt("u1", 0, status='{"status": "ok"}',
                     transcript='{"findings": [], "summary": "read the area"}')
        with mock.patch.object(review_panel, "check_result", side_effect=
                               review_panel.ReviewPanelError("field 'findings' must be a list")):
            self.run_object().adopt()
        record = driver.read_attempts(self.rundir, "u1", grace=120.0)[0].disposition
        self.assertEqual(record["outcome"], driver.REFUSED)
        self.assertEqual(driver.replay([record], 0).reply_charges, 1)

    def test_a_worker_input_that_could_not_be_copied_stops_the_preparation(self):
        """The brief and the schema are the worker's whole task. A copy that failed and was
        suppressed launches a worker with no instructions or half of them, while every
        small record around it succeeds — and the missing or malformed answer it gives
        spends the unit's allowances. The unit is then charged for a file this program
        failed to put there."""
        run = self.run_object()
        unit = run.unit_row("u1")
        real = shutil.copyfile

        def full(src, dst, *args, **kw):
            if Path(dst).name == review_panel.PAYLOAD_NAME:
                raise OSError(errno.ENOSPC, "No space left on device", str(dst))
            return real(src, dst, *args, **kw)

        with mock.patch.object(driver.shutil, "copyfile", full):
            with self.assertRaises(driver.StorageFault) as ctx:
                run.spawn(unit)
        self.assertIn(review_panel.PAYLOAD_NAME, str(ctx.exception))
        self.assertEqual(driver.attempt_dirs(self.rundir, "u1"), [],
                         "an attempt was claimed for a worker with no brief")
        self.assertEqual(self.stub_invocations(), 0)

    def test_a_status_line_that_never_landed_adjudicates_nothing(self):
        """The other half of the supervisor's side: a status write that ENOSPC stopped
        leaves the file the redirect created and nothing in it. Absent, empty and truncated
        are one state, and none of them is an outcome."""
        path = self.attempt("u1", 0, status="", deadline=1.0,
                            spawn_time=time.time() - 3600)
        run = self.run_object()
        run.adopt()
        self.assertFalse((path / "disposition.json").exists())
        self.assertIsNone(driver.landed(self.rundir, "u1"))
        self.assertIn("a0", run.quarantined("u1") or "")

    # -- this program's own writes ------------------------------------------- #
    def test_a_failed_argv_write_stops_the_run_non_zero_and_claims_nothing(self):
        run = self.run_object(poll=0.05)
        with self.failing_write("argv.json"):
            code = run.loop()
        self.assertEqual(code, driver.EXIT_STOPPED)
        self.assertIn("argv.json", run.pause_reason)
        self.assertEqual(self.stub_invocations(), 0, "a worker ran after the pause")
        for unit in ("u1", "u2"):
            for attempt in driver.read_attempts(self.rundir, unit, grace=120.0):
                self.assertIsNone(attempt.disposition, f"{unit} was adjudicated")
            self.assertIsNone(driver.landed(self.rundir, unit))

    def test_a_failed_disposition_write_decides_nothing(self):
        path = self.attempt("u1", 0, status=json.dumps(
            {"status": "error", "reason": "reviewer exited 1"}))
        run = self.run_object()
        with self.failing_write("disposition.json"):
            with self.assertRaises(driver.StorageFault):
                run.adopt()
        self.assertFalse((path / "disposition.json").exists())
        self.assertIsNone(driver.landed(self.rundir, "u1"))
        self.assertFalse((self.rundir / "units" / "u1" / "error.txt").exists())

    def test_a_failed_publication_loses_no_answer(self):
        """The disposition already says what this unit's answer is. A publication the disk
        refused is one the next pass finishes — which is the whole of "a disposition is not
        a landing", met by a fault rather than by a kill."""
        self.accepted_attempt("u1")
        with self.failing_write("result.json", text=True):
            with self.assertRaises(driver.StorageFault):
                self.run_object().adopt()
        self.assertFalse((self.rundir / "units" / "u1" / "result.json").exists())
        self.assertIsNone(driver.landed(self.rundir, "u1"))
        self.run_object().adopt()
        self.assertEqual(driver.landed(self.rundir, "u1")["publication"], "result")

    def test_a_failed_landing_record_leaves_the_unit_unterminal_until_it_is_written(self):
        self.accepted_attempt("u1")
        with self.failing_write("landed.json"):
            with self.assertRaises(driver.StorageFault):
                self.run_object().adopt()
        self.assertTrue((self.rundir / "units" / "u1" / "result.json").exists())
        self.assertIsNone(driver.landed(self.rundir, "u1"),
                          "a publication with no record read as terminal")
        self.run_object().adopt()
        self.assertEqual(driver.landed(self.rundir, "u1")["attempt"], "a0")

    def test_a_failed_breaker_write_leaves_the_generation_where_it_was(self):
        """The breaker is derived and its file is a record of one closed incident. A close
        that could not be written must not be believed: the next pass would then read a
        generation nobody recorded and treat the outage's own reports as a recurrence."""
        run = self.run_object()
        with self.failing_write(driver.GENERATIONS_PREFIX):
            with self.assertRaises(driver.StorageFault):
                run._close_generation("stub-A", 0)
        self.assertEqual(run.generation("stub-A"), 0)

    def test_the_budget_write_is_not_swallowed(self):
        """`save_budget` suppresses an ordinary write failure, because a few minutes of
        accounting is not worth ending a run over. A storage fault is not that: the same
        volume holds every record this run is about to write."""
        run = self.run_object()
        with self.failing_write("budget.json"):
            with self.assertRaises(driver.StorageFault):
                run.save_budget(force=True)

    def test_a_stage_that_could_not_write_the_marker_pauses_rather_than_refusing(self):
        """The marker is the engine's own last write, so a full disk arrives as a stage
        refusal. Read as an ordinary refusal it would end the run non-resumably over
        something a gigabyte of free space fixes."""
        for unit in ("u1", "u2"):
            self.accepted_attempt(unit)
        run = self.run_object()
        run.adopt()
        message = f"cannot write {self.rundir / 'units.json'}: {ENOSPC_TEXT}"
        with mock.patch.object(driver, "_engine_stage", return_value=(2, "", message)):
            code = run.loop()
        self.assertEqual(code, driver.EXIT_STOPPED)
        self.assertIn("units.json", run.pause_reason)
        self.assertEqual(
            json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))["stage"],
            "reading", "the marker moved on a stage that could not write it")

    def test_a_pause_exits_non_zero_where_a_drain_exits_zero(self):
        """Both stop claiming and only one is a success. A caller scripting this cannot
        tell them apart from prose, and restarting a paused run walks back into the fault."""
        run = self.run_object(poll=0.05)
        with mock.patch.object(
                driver.Run, "adopt",
                side_effect=driver.StorageFault(self.rundir / "dispatch" / "u1" / "a0"
                                                / "disposition.json",
                                                OSError(errno.ENOSPC, "No space left"))):
            self.assertEqual(run.loop(), driver.EXIT_STOPPED)
        self.assertIn("disposition.json", run.pause_reason)
        drained = self.run_object(poll=0.05)
        drained.request_drain("the operator asked")
        self.assertEqual(drained.loop(), driver.EXIT_OK)

    def test_a_write_that_failed_for_any_other_reason_is_not_a_pause(self):
        """A value that will not serialize is a defect in this program, and pausing the run
        over it would hide it behind a message about the disk."""
        real = driver._engine_write_json

        def fake(path, payload):
            if "disposition.json" in str(path):
                raise review_panel.InventoryError(f"cannot write {path}: [Errno 13] denied")
            return real(path, payload)

        self.attempt("u1", 0, status=json.dumps({"status": "error", "reason": "exited 1"}))
        with mock.patch.object(driver, "_engine_write_json", fake):
            with self.assertRaises(review_panel.InventoryError):
                self.run_object().adopt()


# --------------------------------------------------------------------------- #
# 7.3 the snapshot — evidence only while it is unchanged
# --------------------------------------------------------------------------- #
class TheSnapshotIsEvidenceOnlyWhileItIsUnchanged(_Case):
    """A snapshot that any worker can write into is not evidence: a nominal reader can
    change the tree its siblings are still reading and the report's citations are checked
    against.

    Two halves, and the second is what holds everywhere: permissions make the mistake hard
    to make, and verification against the inventory's digests **and its file set** is what
    catches it when the platform's permissions do not stop it.
    """

    def setUp(self):
        super().setUp()
        self.plan_only()
        self.snapshot = self.rundir / "snapshot"
        self.files = sorted(p for p in self.snapshot.rglob("*") if p.is_file())
        self.assertTrue(self.files, "the planned run has no snapshot to check")

    def unlock(self, path):
        """Restore write permission, the way somebody working around the hardening would."""
        os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
        self.addCleanup(driver.harden_snapshot, self.snapshot)

    def delete(self, target):
        """Take one file out of the hardened snapshot, on either kind of platform.

        **Two permissions, not one.** A POSIX unlink asks the DIRECTORY for write and cares
        nothing for the file's own bit; Windows has no directory permission that authorizes
        it and refuses outright to delete a file carrying the read-only attribute. A case
        that restored only the directory's therefore removes the file here and raises
        `Access is denied` on the platform this hardening is hardest on — a red job about
        the fixture rather than about the check. `remove_tree` restores both before it
        deletes for exactly this reason, and this is the same courtesy.
        """
        self.unlock(target.parent)
        self.unlock(target)
        target.unlink()

    def test_every_snapshot_file_loses_write_permission(self):
        for path in self.files:
            self.assertFalse(os.stat(path).st_mode & 0o222, path)
            self.assertFalse(os.access(path, os.W_OK), path)

    def test_what_a_hardened_directory_guarantees_on_this_platform(self):
        """**The guarantee is not the same everywhere, and this asserts what is true here
        rather than skipping.** On POSIX a directory without its write bit accepts no new
        entry. On Windows `os.chmod` sets one attribute — read-only — which stops a file
        being rewritten and does not stop one being created, so the file-set check is what
        catches it there.
        """
        added = self.snapshot / "added-by-a-reader.py"
        if os.name == "posix":
            with self.assertRaises(PermissionError):
                added.write_text("x\n", encoding="utf-8")
            self.assertEqual(driver.verify_snapshot(self.rundir), [])
        else:
            added.write_text("x\n", encoding="utf-8")
            self.addCleanup(added.unlink)
            self.assertTrue(any("added-by-a-reader.py" in line
                                for line in driver.verify_snapshot(self.rundir)),
                            "a file added on a platform that allows it went unnoticed")

    def test_a_rewritten_file_is_caught(self):
        target = self.files[0]
        self.unlock(target)
        target.write_text("this is not what was measured\n", encoding="utf-8")
        rel = target.relative_to(self.snapshot).as_posix()
        self.assertIn(f"{rel} was rewritten after it was measured",
                      driver.verify_snapshot(self.rundir))
        with self.assertRaises(driver.DriverError) as ctx:
            self.run_object().check_snapshot()
        self.assertIn(rel, str(ctx.exception))

    def test_a_file_added_beside_the_measured_ones_is_caught(self):
        self.unlock(self.snapshot)
        (self.snapshot / "extra.py").write_text("import os\n", encoding="utf-8")
        self.assertIn("extra.py was added to the snapshot and was never measured",
                      driver.verify_snapshot(self.rundir))

    def test_a_deleted_file_is_caught(self):
        target = self.files[0]
        rel = target.relative_to(self.snapshot).as_posix()
        self.delete(target)
        self.assertIn(f"{rel} is gone from the snapshot",
                      driver.verify_snapshot(self.rundir))

    def test_a_restart_verifies_before_it_dispatches_anything(self):
        """Startup is the first round boundary. A run resumed over a changed snapshot must
        refuse before it spawns, or the units it dispatches are read against bytes the
        report will cite as something else."""
        target = self.files[0]
        self.unlock(target)
        target.write_text("changed between the runs\n", encoding="utf-8")
        before = self.stub_invocations()
        proc = self.drive("--go", expect=driver.EXIT_REFUSED)
        self.assertIn("no longer what inventory.json measured", proc.stderr)
        self.assertEqual(self.stub_invocations(), before,
                         "a changed snapshot still dispatched work")

    def linked(self, link, target):
        """A symlink inside the snapshot, or what this platform does instead.

        **Asserted rather than skipped.** Windows refuses `os.symlink` without the developer
        mode or the privilege, so a case that skipped there would report green about the one
        platform where the file-set check is doing the whole job. Where a link cannot be
        made, a plain file is put at the same path: a host that will not create a link
        cannot have one added to a snapshot either, and what is being asked — that an
        unmeasured entry beside the measured ones is caught — is the same question.
        """
        self.unlock(self.snapshot)
        try:
            os.symlink(target, link)
            return True
        except (OSError, NotImplementedError, AttributeError):
            link.write_text("stood in for a link this host will not create\n",
                            encoding="utf-8")
            return False

    def test_a_link_added_beside_the_measured_files_is_caught(self):
        """`snapshot_files` skipped symlinks, so an added one was invisible to a check whose
        whole job is to notice what arrived after the measurement — while changing what the
        tree imports and what a build does, with every measured digest still matching."""
        link = self.snapshot / "config.py"
        self.assertFalse(link.exists(), "the fixture already measures this name")
        was_a_link = self.linked(link, self.files[0])
        self.addCleanup(lambda: link.unlink(missing_ok=True))
        problems = driver.verify_snapshot(self.rundir)
        self.assertIn("config.py was added to the snapshot and was never measured",
                      problems, f"an added {'link' if was_a_link else 'file'} went unseen")
        with self.assertRaises(driver.DriverError) as ctx:
            self.run_object().check_snapshot()
        self.assertIn("config.py", str(ctx.exception))

    def test_a_measured_file_replaced_by_a_link_is_caught_without_being_followed(self):
        """A link is never followed for the digest: taken through it, a measured file
        replaced by a link to an identical copy outside the snapshot verifies, while every
        reader is reading a file the run never measured."""
        target = self.files[0]
        rel = target.relative_to(self.snapshot).as_posix()
        elsewhere = self.tmp / "identical-copy"
        elsewhere.write_bytes(target.read_bytes())
        self.delete(target)
        if not self.linked(target, elsewhere):
            # No link to be had here, so the swap this asks about cannot be made either.
            # What is left is the half that does hold: the bytes are now something the
            # inventory did not measure.
            target.write_text("not what was measured\n", encoding="utf-8")
            self.assertIn(f"{rel} was rewritten after it was measured",
                          driver.verify_snapshot(self.rundir))
            return
        self.assertIn(f"{rel} was replaced by a symlink after it was measured",
                      driver.verify_snapshot(self.rundir))

    def test_a_snapshot_file_the_host_would_not_read_pauses_rather_than_condemning_the_run(self):
        """A digest nobody could take is not a digest that differs. Reported as a
        modification it tells an operator to plan a new run and discard a completed one,
        over a fault a resume would step straight past."""
        target = self.files[0]
        real_open = open

        def refusing(path, *args, **kwargs):
            if driver._resolved(Path(path)) == driver._resolved(target):
                raise OSError(errno.EIO, "Input/output error", str(path))
            return real_open(path, *args, **kwargs)

        run = self.run_object(poll=0.05)
        with mock.patch.object(driver, "open", refusing, create=True):
            with self.assertRaises(driver.StorageFault) as ctx:
                run.check_snapshot()
            self.assertEqual(run.loop(), driver.EXIT_STOPPED)
        self.assertNotIn("Plan a new run", str(ctx.exception))
        self.assertIn(target.name, run.pause_reason)
        self.assertIn("could not be read", run.pause_reason)
        # And with the fault gone the same snapshot verifies, so what stopped the run was
        # the refused read and not a tree that had actually changed.
        self.assertEqual(driver.verify_snapshot(self.rundir), [])

    def test_a_snapshot_directory_that_cannot_be_listed_pauses_rather_than_reporting_losses(self):
        """The pair with `test_a_deleted_file_is_caught`, and the two must not be spelled the
        same. A directory skipped because the host would not list it reports every measured
        file under it as gone — which tells an operator to discard a completed run — while an
        ADDED file in that same directory is reported as nothing at all, and an addition is
        what this check exists to catch.
        """
        base = self.snapshot
        with _refusing_os("listdir", str(base), PermissionError(
                errno.EACCES, "Permission denied", str(base))):
            with self.assertRaises(driver.UnreadableRecord) as ctx:
                driver.verify_snapshot(self.rundir)
        self.assertIn(str(base), str(ctx.exception))
        # The half that is genuinely absent still answers as an absence, so the refusal above
        # is not this function having stopped telling deletions apart.
        target = self.files[0]
        self.delete(target)
        self.assertIn(f"{target.relative_to(self.snapshot).as_posix()} is gone from the "
                      f"snapshot", driver.verify_snapshot(self.rundir))

    def test_a_snapshot_entry_that_cannot_be_described_pauses_too(self):
        """One entry rather than a whole directory. Passed over, a measured file is reported
        deleted and an unmeasured one is reported not at all."""
        target = self.files[0]
        with _refusing_os("stat", str(target), OSError(
                errno.EIO, "Input/output error", str(target))):
            with self.assertRaises(driver.StorageFault) as ctx:
                driver.snapshot_files(self.snapshot)
        self.assertIn(target.name, str(ctx.exception))
        self.assertIn(target.relative_to(self.snapshot).as_posix(),
                      driver.snapshot_files(self.snapshot),
                      "the same entry is found with nothing refusing it")

    def test_an_entry_that_is_neither_file_link_nor_directory_is_named(self):
        """**What is true on this platform, asserted rather than skipped.** `plan` copies
        regular files and nothing else, so a fifo or a device node under the snapshot arrived
        afterwards and IS a change. Recorded as a file instead, the digest below would open
        it and a read of a fifo nobody writes to never returns. Where the host has no
        `mkfifo` — Windows — the case cannot be made, and what holds there is the half above
        it: an added ordinary entry is caught by the file set.
        """
        self.unlock(self.snapshot)
        spot = self.snapshot / "a-pipe"
        if not hasattr(os, "mkfifo"):
            spot.write_text("x\n", encoding="utf-8")
            self.addCleanup(spot.unlink)
            self.assertIn("a-pipe was added to the snapshot and was never measured",
                          driver.verify_snapshot(self.rundir))
            return
        os.mkfifo(spot)
        self.addCleanup(spot.unlink)
        with self.assertRaises(driver.DriverError) as ctx:
            driver.verify_snapshot(self.rundir)
        self.assertIn("a-pipe", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, driver.RunPaused,
                                 "a changed tree is a refusal, not a fault to resume past")

    def as_a_junction(self, target):
        """Answer every ``os.stat`` of ``target`` with Windows metadata for a junction.

        **Simulated, and said plainly.** A junction cannot be created on this platform at
        all, so the alternative to describing one is a case that skips on every host this
        suite runs on and reports green about the one platform where the defect is
        reachable. What is simulated is exactly the one field the decision is made from —
        ``st_reparse_tag``, which exists on Windows stat results and nowhere else — and the
        code under it has to work where the attribute is absent, which every other case
        here establishes. The Windows CI job is what exercises the real thing.
        """
        real = os.stat
        tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
        # **Both spellings, taken BEFORE the patch is armed, and compared as strings.**
        # `Path.resolve` reaches `os.stat` itself, so resolving inside the replacement makes
        # every stat in the process recurse into the thing being replaced — which is not a
        # failed assertion but a test that never ends.
        wanted = {str(target), str(driver._resolved(target))}

        class _Junction:
            def __init__(self, info):
                self._info = info
                self.st_reparse_tag = tag

            def __getattr__(self, name):
                return getattr(self._info, name)

        def stat_or_junction(path, *args, **kwargs):
            info = real(path, *args, **kwargs)
            if str(path) in wanted:
                return _Junction(info)
            return info

        return mock.patch.object(os, "stat", stat_or_junction)

    def test_a_junction_added_to_the_snapshot_is_caught_like_any_other_link(self):
        """A junction is a directory to every test but one. Walked as an ordinary directory
        it is descended into and never recorded, so an added one — an empty target is enough
        — leaves the file set exactly as it was measured and verifies clean, while what a
        build imports now comes from wherever it points."""
        self.unlock(self.snapshot)
        added = self.snapshot / "ext"
        added.mkdir()
        self.addCleanup(added.rmdir)
        self.assertEqual(driver.verify_snapshot(self.rundir), [],
                         "an empty directory is not itself a change")
        with self.as_a_junction(added):
            problems = driver.verify_snapshot(self.rundir)
        self.assertIn("ext was added to the snapshot and was never measured", problems)

    def test_a_measured_directory_replaced_by_a_junction_is_not_descended_into(self):
        """The bytes under a junction can hash exactly as they were measured and still be
        somebody else's files. The walk stops at it, so every measured file below is missing
        and the junction itself is an entry nobody measured — which is what a changed
        snapshot looks like, correctly."""
        inner = sorted(p for p in (self.snapshot / "engine").rglob("*") if p.is_file())
        self.assertTrue(inner, "the fixture has no nested directory to swap")
        with self.as_a_junction(self.snapshot / "engine"):
            problems = driver.verify_snapshot(self.rundir)
        self.assertIn("engine was added to the snapshot and was never measured", problems)
        for path in inner:
            rel = path.relative_to(self.snapshot).as_posix()
            self.assertIn(f"{rel} is gone from the snapshot", problems,
                          "a file under a junction was read as part of the snapshot")

    def test_hardening_does_not_reach_through_a_junction(self):
        """What is under a junction is outside the run. Descended into, this program takes
        write permission off files that are not its own and are not part of any snapshot —
        `rglob` leaves a symlinked directory alone and walks a junction, which is why the
        walk is written out."""
        self.unlock(self.snapshot)
        outside = self.tmp / "not-ours"
        outside.mkdir()
        (outside / "theirs.py").write_text("x = 1\n", encoding="utf-8")
        stand_in = self.snapshot / "ext"
        stand_in.mkdir()
        self.addCleanup(lambda: driver.remove_tree(stand_in, stand_in.parent))
        (stand_in / "theirs.py").write_text("x = 1\n", encoding="utf-8")
        with self.as_a_junction(stand_in):
            driver.harden_snapshot(self.snapshot)
        self.assertTrue(os.stat(stand_in / "theirs.py").st_mode & stat.S_IWUSR,
                        "hardening wrote through a junction into somebody else's tree")

    def test_a_reproduction_rewrites_its_own_copy_and_not_the_snapshot(self):
        """The copy is made writable explicitly, because the snapshot it came from is not.
        A verifier that could not rewrite a file could not run a reproduction at all."""
        units = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        unit = dict(units["units"][0], kind=review_panel.VERIFIER_KIND)
        _inbox, _outbox, work = driver.prepare_worker_paths(
            self.rundir, unit, driver.reserve_token(self.rundir))
        # Resolved on both sides: the copy and the snapshot are compared as directories,
        # and macOS answers /var with /private/var.
        self.assertNotEqual(driver._resolved(work), driver._resolved(self.snapshot))
        target = sorted(p for p in work.rglob("*") if p.is_file())[0]
        target.write_text("a reproduction rewrote this\n", encoding="utf-8")
        (work / "new-fixture.txt").write_text("written by the reproduction\n",
                                              encoding="utf-8")
        self.assertEqual(driver.verify_snapshot(self.rundir), [],
                         "a write inside a copy reached the snapshot")


class TheRoundThatPublishesIsVerifiedToo(_Case):
    """Verification at the top of a round covers the round it is about to start. The LAST
    round has no next one — the marker reaches `reported` and the loop returns — so a worker
    that altered the tree during it would be caught by nobody, and the report would be
    published with every citation pointing at bytes nothing measured.

    The window is not theoretical on Windows, where the read-only attribute stops a file
    being rewritten and does not stop one being created; the file set is what catches that.
    """

    def setUp(self):
        super().setUp()
        # One synthesizer round away from the report, with its unit already landed, so the
        # round dispatches nothing and the only thing between here and `report` is the
        # check this asks about. A verifier landed on the other slot beside it, because a
        # run that reached only one slot is refused before `report` and this class is about
        # the snapshot check rather than about that refusal.
        self.fake_units(("s1", "A"), ("v1", "B"), stage=review_panel.SYNTHESIZED_STAGE)
        doc = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        kinds = {"s1": review_panel.SYNTHESIZER_KIND, "v1": review_panel.VERIFIER_KIND}
        for unit in doc["units"]:
            unit["kind"] = kinds[unit["id"]]
        (self.rundir / "units.json").write_text(json.dumps(doc), encoding="utf-8")
        for unit, result in (("s1", '{"tiers": [], "defects": [], "summary": "x"}'),
                             ("v1", '{"verdicts": [], "summary": "x"}')):
            self.attempt(unit, 0, status='{"status": "ok"}',
                         disposition={"attempt": "a0", "unit": unit,
                                      "outcome": driver.ACCEPTED,
                                      "intended_publication": "result"})
            (self.rundir / "units" / unit / review_panel.RESULT_NAME).write_text(
                result, encoding="utf-8")
            (self.rundir / "dispatch" / unit / "landed.json").write_text(
                json.dumps({"publication": "result", "attempt": "a0"}), encoding="utf-8")
        self.snapshot = self.fake_snapshot()

    def reporting_stage(self):
        """A `report` that succeeds and advances the marker, as the engine's would.

        Advancing it matters: without the check, this run reaches `reported` and returns 0 —
        a report published against a snapshot somebody changed, which is the failure, and it
        is what makes this test's red unambiguous.
        """
        def staged(stage, rundir):
            doc = json.loads(
                (Path(rundir) / "units.json").read_text(encoding="utf-8"))
            doc["stage"] = driver.REPORTED_STAGE
            (Path(rundir) / "units.json").write_text(json.dumps(doc), encoding="utf-8")
            return 0, "", ""

        return mock.patch.object(driver, "_engine_stage", side_effect=staged)

    def test_a_file_a_worker_added_during_the_last_round_stops_the_report(self):
        run = self.run_object(poll=0.05)
        real = driver.Run.dispatch_round

        def dispatch_and_meddle(instance, kinds):
            real(instance, kinds)
            # A worker adding a file to the tree it was told to read. The directory is made
            # writable first because this platform's hardening would otherwise refuse the
            # create — which is the half of §7.3 that Windows does not provide.
            os.chmod(self.snapshot, os.stat(self.snapshot).st_mode | stat.S_IWUSR)
            (self.snapshot / "added-by-a-worker.py").write_text("x\n", encoding="utf-8")

        with mock.patch.object(driver.Run, "dispatch_round", dispatch_and_meddle), \
                self.reporting_stage() as staged:
            with self.assertRaises(driver.DriverError) as ctx:
                run.loop()
        self.assertIn("added-by-a-worker.py", str(ctx.exception))
        staged.assert_not_called()
        self.assertFalse((self.rundir / review_panel.DISPATCH_FILE_NAME).exists(),
                         "the run committed its provenance over a changed snapshot")
        self.assertEqual(
            json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))["stage"],
            review_panel.SYNTHESIZED_STAGE, "the report was published anyway")

    def test_an_unchanged_snapshot_reports_as_it_always_did(self):
        """The check has to be a check and not a stop: a round nobody meddled with reaches
        the report exactly as before."""
        run = self.run_object(poll=0.05)
        with self.reporting_stage() as staged:
            self.assertEqual(run.loop(), driver.EXIT_OK)
        self.assertEqual([call.args[0] for call in staged.call_args_list], ["report"])


# --------------------------------------------------------------------------- #
# 5.4 copies — the copy is the archive, and a protected one is not ours to remove
# --------------------------------------------------------------------------- #
EXECUTED = {"argv": ["bash", "reproduce.sh"], "cwd": ".", "exit_status": 1,
            "output": "boom\n", "truncated": False, "run_kind": "executed",
            "shows": "Shows the failure the candidate reports."}


class TheCopyIsTheArchive(_Case):
    """Deletion needs two facts, not one: the unit is terminal **and** that attempt's
    execution has ended. The supervisor detaches its worker, so a copy removed on
    terminality alone is a working directory taken out from under a live process.

    And what is kept is the whole copy: a verdict naming `bash reproduce.sh` does not name
    the fixture that script reads, so there is no partial archive to keep instead.
    """

    UNITS = ("verify-area-01-A", "verify-area-01-B", "verify-area-02-A")

    def setUp(self):
        super().setUp()
        self.fake_units(*[(unit, "B") for unit in self.UNITS])
        doc = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        for n, row in enumerate(doc["units"], 1):
            row["kind"] = review_panel.VERIFIER_KIND
            row["candidates"] = [f"cand-{n:03d}"]
            (self.rundir / "units" / row["id"]).mkdir(parents=True, exist_ok=True)
        (self.rundir / "units.json").write_text(json.dumps(doc), encoding="utf-8")
        (self.rundir / review_panel.CANDIDATES_FILE_NAME).write_text(json.dumps({
            "candidates": [
                {"id": "cand-001", "raised_by": [{"severity": "blocker"}]},
                {"id": "cand-002", "raised_by": [{"severity": "minor"}]},
                {"id": "cand-003", "raised_by": [{"severity": "nit"}]},
            ]}), encoding="utf-8")

    def copy_for(self, unit, *, evidence=EXECUTED, landed=True, status='{"status": "ok"}',
                 disposition=None, resolution=None, filler=0):
        """One attempt with a working copy on disk, landed by default.

        `filler` pads the copy so the retention cap has something to bite on; the fixture
        beside the script is what the verdict never names and the copy is kept for.
        """
        token = f"{abs(hash(unit)) % (1 << 60):016x}"
        work = self.rundir / "work" / token
        work.mkdir(parents=True)
        (work / "reproduce.sh").write_text("bash fixtures/data.txt\n", encoding="utf-8")
        (work / "fixtures").mkdir()
        (work / "fixtures" / "data.txt").write_text("x" * (filler or 1), encoding="utf-8")
        (self.rundir / "in" / token).mkdir(parents=True)
        record = disposition if disposition is not None else {
            "attempt": "a0", "unit": unit, "outcome": driver.ACCEPTED,
            "intended_publication": "result"}
        path = self.attempt(unit, 0, status=status, disposition=record,
                            resolution=resolution,
                            argv={"token": token, "kind": review_panel.VERIFIER_KIND,
                                  "slot": "B"})
        (path / "reply.json").write_text(json.dumps({
            "verdicts": [{"candidate": f"cand-{self.UNITS.index(unit) + 1:03d}",
                          "status": "reproduced", "evidence": evidence}],
            "summary": "checked"}), encoding="utf-8")
        if landed:
            (self.rundir / "units" / unit / review_panel.RESULT_NAME).write_text(
                '{"verdicts": [], "summary": "x"}', encoding="utf-8")
            (self.rundir / "dispatch" / unit / "landed.json").write_text(json.dumps(
                {"publication": "result", "attempt": "a0"}), encoding="utf-8")
        return work

    def record(self):
        return json.loads(
            (self.rundir / "dispatch" / "copies.json").read_text(encoding="utf-8"))

    def test_a_copy_is_kept_whole_including_what_the_verdict_never_named(self):
        work = self.copy_for(self.UNITS[0])
        self.run_object().adopt()
        self.assertTrue((work / "reproduce.sh").is_file())
        self.assertTrue((work / "fixtures" / "data.txt").is_file(),
                        "the fixture the reproduction reads went with the archive")
        kept = [row["unit"] for row in self.record()["retained"]]
        self.assertEqual(kept, [self.UNITS[0]])

    def test_the_retention_cap_drops_highest_severity_last_and_names_every_one(self):
        blocker = self.copy_for(self.UNITS[0], filler=4096)
        minor = self.copy_for(self.UNITS[1], filler=4096)
        nit = self.copy_for(self.UNITS[2], filler=4096)
        run = self.run_object(keep_repro_bytes=4200)
        run.adopt()
        self.assertTrue(blocker.is_dir(), "the most severe copy was dropped first")
        self.assertFalse(minor.exists())
        self.assertFalse(nit.exists())
        dropped = {row["unit"]: row["severity"] for row in self.record()["dropped"]}
        self.assertEqual(dropped, {self.UNITS[1]: "minor", self.UNITS[2]: "nit"})
        self.assertIn("dropped over the retention cap", status_text(run))

    def test_a_copy_inspected_before_the_route_record_exists_is_ranked_once_it_is_written(self):
        """Severity comes from `candidates.json`, which routing writes at the end of the
        reading round — so a run resumed mid-reading with a copy already on disk asks the
        question before there is anything to answer it with. The empty answer is correct at
        that moment and wrong a round later, and held, it makes the retention cap drop copies
        by unit name: a nit's reproduction kept and a blocker's deleted.

        The fixtures above all write the route record before the run is built, which is why
        none of them can see this. The severities here are deliberately upside down against
        the unit names, so name order and severity order disagree.
        """
        candidates = self.rundir / review_panel.CANDIDATES_FILE_NAME
        candidates.unlink()
        nit = self.copy_for(self.UNITS[0], filler=4096)       # cand-001
        blocker = self.copy_for(self.UNITS[2], filler=4096)   # cand-003
        run = self.run_object(keep_repro_bytes=4200)
        self.assertEqual(run.candidate_severities(), {},
                         "a route record that is not there ranked something anyway")
        candidates.write_text(json.dumps({"candidates": [
            {"id": "cand-001", "raised_by": [{"severity": "nit"}]},
            {"id": "cand-003", "raised_by": [{"severity": "blocker"}]},
        ]}), encoding="utf-8")
        run.adopt()
        self.assertTrue(blocker.is_dir(),
                        "the cap dropped the blocker's copy and kept the nit's")
        self.assertFalse(nit.exists())
        self.assertEqual([row["unit"] for row in self.record()["dropped"]],
                         [self.UNITS[0]])

    def test_a_copy_whose_execution_cannot_be_shown_to_have_ended_is_never_dropped(self):
        """An operator failed the attempt and did not attest that the worker had stopped.
        The unit is terminal and the directory is still not this program's to remove — and
        it is excluded from the cap, or a budget spent by a unit that never finished would
        delete the evidence of one that did.
        """
        work = self.copy_for(
            self.UNITS[0], status=None,
            # Documentary, deliberately: this copy is kept by the protection rule and by
            # nothing else. With executed evidence the retention rule would keep it too,
            # and the test would pass with the protection removed.
            evidence={"argv": ["grep", "-n", "x", "a.py"], "cwd": ".", "exit_status": 1,
                      "output": "", "truncated": False, "run_kind": "documentary",
                      "shows": "Shows the name appears nowhere."},
            resolution={"action": "fail", "reason": "nobody could account for it",
                        "stopped_confirmed": False},
            disposition={"attempt": "a0", "unit": self.UNITS[0],
                         "outcome": driver.OPERATOR_FAILED, "stopped_confirmed": False,
                         "intended_publication": "error"}, landed=False)
        run = self.run_object(keep_repro_bytes=1)
        run.adopt()
        run.adopt()          # carried across a second pass, and to the end of the run
        self.assertTrue((work / "reproduce.sh").is_file(),
                        "a working directory was removed out from under a live worker")
        record = self.record()
        self.assertEqual([row["unit"] for row in record["protected"]], [self.UNITS[0]])
        self.assertEqual(record["dropped"], [])
        self.assertIn("protected working directories", status_text(run))

    def test_a_kill_during_preparation_leaves_directories_the_next_pass_collects(self):
        """`in/<token>/` and `work/<token>/` are created before the attempt is claimed, so a
        kill in that window leaves a whole copy of the snapshot belonging to no unit. No
        landing, no cleanup and no operator command reaches it, because it is nobody's."""
        live = self.copy_for(self.UNITS[0])
        leaked = f"{7:016x}"
        for area in ("in", "out", "work"):
            (self.rundir / area / leaked).mkdir(parents=True)
            (self.rundir / area / leaked / "half-copied.py").write_text("x", encoding="utf-8")
        mine = self.rundir / "work" / "not-a-token-at-all"
        mine.mkdir(parents=True)
        run = self.run_object()
        run.adopt()
        for area in ("in", "out", "work"):
            self.assertFalse((self.rundir / area / leaked).exists(), area)
        self.assertTrue(live.is_dir(), "a copy an attempt names was swept up as leaked")
        self.assertTrue(mine.is_dir(),
                        "a directory that is not a token was deleted by the sweep")

    def test_a_copy_left_read_only_by_a_kill_is_still_reclaimed(self):
        """The copy is made from the hardened snapshot and made writable on the next
        statement, so a kill between the two leaves a read-only tree. A delete that
        tolerates every error cannot take one away — on POSIX a directory without its write
        bit yields no entry, and on Windows the attribute refuses outright — and it gives up
        without saying so, which is how copies accumulate for the life of the volume while
        the retention cap reports nothing wrong.
        """
        leaked = f"{11:016x}"
        for area in ("in", "work"):
            root = self.rundir / area / leaked
            (root / "nested").mkdir(parents=True)
            (root / "nested" / "carried-over.py").write_text("x", encoding="utf-8")
        # The hardening's own shape: every file and every directory loses user write, and
        # read and traverse are untouched, so the walk still works and only the removal is
        # refused. Applied deepest-first, or the parent refuses the child's chmod.
        for area in ("in", "work"):
            root = self.rundir / area / leaked
            for path in sorted(root.rglob("*"), reverse=True):
                path.chmod(path.stat().st_mode & ~0o222)
            root.chmod(root.stat().st_mode & ~0o222)
        self.run_object().adopt()
        for area in ("in", "work"):
            self.assertFalse((self.rundir / area / leaked).exists(),
                             f"{area}/ was left on disk because the tree was read-only; "
                             f"nothing downstream ever reports it and it is never retried")

    def test_an_unreadable_spawn_record_stops_the_sweep_instead_of_authorizing_it(self):
        """`argv.json` is the only thing that names an attempt's token. A read the host
        refuses — a failing device under the run directory — leaves a live worker's input,
        output and working directory named by nothing, which is exactly what a leak looks
        like: the sweep would delete all three out from under a worker that is still running
        in them. Deletion needs two facts and an unreadable record establishes neither.
        """
        work = self.copy_for(self.UNITS[0])
        token = work.name
        (self.rundir / "out" / token).mkdir(parents=True)
        real = driver._read_json

        def unreadable(path):
            # What `_read_json` returns for a file the host would not read: the same None it
            # returns for one that is not there. The file is still on disk.
            return None if path.name == "argv.json" else real(path)

        with mock.patch.object(driver, "_read_json", unreadable):
            self.run_object().adopt()
        for area in ("in", "out", "work"):
            self.assertTrue((self.rundir / area / token).is_dir(),
                            f"{area}/ was reclaimed on a record nobody could read")
        # And with the record readable again the sweep is unblocked, so what stopped it was
        # the unreadable record and not some other reason to keep the directories.
        leaked = f"{9:016x}"
        (self.rundir / "work" / leaked).mkdir(parents=True)
        self.run_object().adopt()
        self.assertFalse((self.rundir / "work" / leaked).exists())

    def test_the_removal_of_a_copy_is_recorded_before_the_tree_goes(self):
        """A copy deleted before its removal is recorded is evidence gone with no account of
        why — and nothing on disk to reconstruct one from, because what enumeration would
        have found is exactly what was deleted."""
        self.copy_for(self.UNITS[0], filler=4096)
        minor = self.copy_for(self.UNITS[1], filler=4096)
        real_rmtree = shutil.rmtree
        recorded_when_deleted = {}

        def watched(path, *args, **kw):
            recorded_when_deleted[Path(path).name] = [
                row.get("unit") for row in (self.record().get("dropped")
                                            if (self.rundir / "dispatch" /
                                                "copies.json").exists() else [])]
            return real_rmtree(path, *args, **kw)

        with mock.patch.object(driver.shutil, "rmtree", watched):
            self.run_object(keep_repro_bytes=4200).adopt()
        self.assertFalse(minor.exists())
        self.assertIn(self.UNITS[1], recorded_when_deleted.get(minor.name, []),
                      "the copy was deleted before anything said it had been")

    def test_a_kill_after_the_record_and_before_the_deletion_is_finished_next_pass(self):
        """The other side of the order: an interrupted deletion is an entry whose directory
        is still there, and the next pass completes it rather than deciding it again."""
        self.copy_for(self.UNITS[0], filler=4096)
        minor = self.copy_for(self.UNITS[1], filler=4096)
        # The kill: the intent is written and the process dies before the tree goes.
        with mock.patch.object(driver.shutil, "rmtree", lambda *args, **kw: None):
            self.run_object(keep_repro_bytes=4200).adopt()
        self.assertTrue(minor.is_dir(), "the fake kill did not stop the deletion")
        entries = [row for row in self.record()["dropped"] if row["unit"] == self.UNITS[1]]
        self.assertEqual(len(entries), 1, "a deletion was begun with no record of why")
        self.assertIs(entries[0]["removed"], False)
        self.run_object(keep_repro_bytes=4200).adopt()
        self.assertFalse(minor.exists(), "the half-made deletion was never finished")
        rows = [row for row in self.record()["dropped"] if row["unit"] == self.UNITS[1]]
        self.assertEqual(len(rows), 1, "the same deletion was recorded twice")
        self.assertIs(rows[0]["removed"], True)

    def test_a_retention_record_nobody_could_read_is_never_replaced(self):
        """The dropped list is the only account of what a cap removed, and the copies it
        names are gone. Read as "no record yet", a refused read is answered with a
        replacement carrying `dropped: []` — and the evidence it described cannot be
        reconstructed, because what enumeration would have found is what was deleted.
        """
        self.copy_for(self.UNITS[0], filler=4096)
        self.copy_for(self.UNITS[1], filler=4096)
        self.run_object(keep_repro_bytes=4200).adopt()
        before = (self.rundir / "dispatch" / "copies.json").read_text(encoding="utf-8")
        self.assertIn(self.UNITS[1], before)
        self.copy_for(self.UNITS[2], filler=4096)
        with _refusing("read_text", "copies.json",
                       OSError(errno.EIO, "Input/output error")):
            with self.assertRaises(driver.StorageFault):
                self.run_object(keep_repro_bytes=4200).adopt()
        self.assertEqual((self.rundir / "dispatch" / "copies.json").read_text("utf-8"),
                         before, "a deletion record was overwritten by a read that failed")

    def test_a_deletion_the_host_keeps_refusing_is_recorded_once(self):
        """A file still open under a copy — the ordinary Windows case — leaves the directory
        on disk, where the next pass enumerates it and decides to drop it again. Appended
        each time, one blocked deletion reads as many, and the reconciliation this record
        exists for is no longer idempotent."""
        self.copy_for(self.UNITS[0], filler=4096)
        blocked = self.copy_for(self.UNITS[1], filler=4096)
        with mock.patch.object(driver.shutil, "rmtree", lambda *a, **kw: None):
            for _ in range(3):
                self.run_object(keep_repro_bytes=4200).adopt()
        self.assertTrue(blocked.is_dir(), "the blocked removal did not stay blocked")
        entries = [row for row in self.record()["dropped"]
                   if row["unit"] == self.UNITS[1]]
        self.assertEqual(len(entries), 1,
                         f"one blocked deletion was recorded {len(entries)} times")
        self.assertIs(entries[0]["removed"], False)
        # And once the host lets it go, the same single entry is the one that closes.
        self.run_object(keep_repro_bytes=4200).adopt()
        self.assertFalse(blocked.exists())
        closed = [row for row in self.record()["dropped"] if row["unit"] == self.UNITS[1]]
        self.assertEqual(len(closed), 1)
        self.assertIs(closed[0]["removed"], True)

    def test_a_token_is_reserved_rather_than_merely_improbable(self):
        """Two attempts sharing a token share a working directory, and one would then be
        answering out of the other's tree. The exclusive create is what makes that
        impossible rather than unlikely.

        Asked of the reservation and then of a spawn, because the wiring is the half that
        matters: a spawn drawing a raw token would quietly hand the second attempt the
        first one's copy, and the reservation would be right and unused.
        """
        first = driver.reserve_token(self.rundir)
        self.assertTrue((self.rundir / "in" / first).is_dir())
        with mock.patch.object(driver, "_new_token", lambda: first):
            with self.assertRaises(driver.DriverError):
                driver.reserve_token(self.rundir)
        self.fake_snapshot()
        run = self.run_object()
        unit = run.unit_row(self.UNITS[0])
        # No worker is launched: the token is reserved before the launch, and a real
        # supervisor here would outlive the test. A launch that raises is adjudicated
        # `launch-failed`, which is a spawn that happened.
        with mock.patch.object(driver, "_new_token", lambda: "0123456789abcdef"), \
                mock.patch.object(driver.subprocess, "Popen",
                                  side_effect=OSError("not launched by this test")):
            self.assertTrue(run.spawn(unit))
            with self.assertRaises(driver.DriverError):
                run.spawn(unit)


# --------------------------------------------------------------------------- #
# the synthesizer is a writer too — the copy is containment, not scratch space
# --------------------------------------------------------------------------- #
class TheSynthesizerGetsACopyLikeEveryOtherWriter(_Case):
    """`references/synthesizer.md` hands its worker a working directory and tells it that
    directory is the only place it may write and is thrown away afterwards. That sentence
    is only true if this driver gives it a copy.

    Given the snapshot instead, the worker does what its brief says and the writes land in
    the tree every other unit of the run was measured against — and one slot's read-only
    mode does not stop it, because `references/dispatch.md` records that it blocks the edit
    tools and not a shell command the worker runs. The next snapshot check then refuses the
    whole run, after every reading and verification round has been paid for. The probe and
    the verifiers are already out of reach of this because they run in a copy; synthesis
    was the one round left standing in the original.
    """

    def setUp(self):
        super().setUp()
        self.fake_units(("s1", "A"), ("s2", "A"),
                        stage=review_panel.SYNTHESIZED_STAGE)
        doc = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        for row in doc["units"]:
            row["kind"] = review_panel.SYNTHESIZER_KIND
        (self.rundir / "units.json").write_text(json.dumps(doc), encoding="utf-8")
        self.snapshot = self.fake_snapshot()

    # -- fixtures ----------------------------------------------------------- #
    def prepared(self, unit):
        """One synthesis unit's worker directories, made the way a spawn makes them.

        Through `prepare_worker_paths` and never by hand: what is being asked is whether
        this driver gives the unit a copy at all, and a directory the test created would
        answer that question itself.
        """
        token = driver.reserve_token(self.rundir)
        _inbox, _outbox, work = driver.prepare_worker_paths(
            self.rundir, self.run_object().unit_row(unit), token)
        return token, work

    def landed_attempt(self, unit, token, *, reply, **over):
        record = {"attempt": "a0", "unit": unit, "outcome": driver.ACCEPTED,
                  "intended_publication": "result"}
        record.update(over.pop("disposition", {}))
        path = self.attempt(unit, 0, status=over.pop("status", '{"status": "ok"}'),
                            disposition=record,
                            argv={"token": token, "kind": review_panel.SYNTHESIZER_KIND,
                                  "slot": "A"}, **over)
        (path / "reply.json").write_text(reply, encoding="utf-8")
        (self.rundir / "units" / unit / review_panel.RESULT_NAME).write_text(
            '{"tiers": [], "defects": [], "summary": "x"}', encoding="utf-8")
        (self.rundir / "dispatch" / unit / "landed.json").write_text(
            json.dumps({"publication": "result", "attempt": "a0"}), encoding="utf-8")
        return path

    def record(self):
        """What the run says it is holding. An empty record where there is no file, so a
        run holding no copy at all fails the assertion below rather than raising under
        it."""
        path = self.rundir / "dispatch" / "copies.json"
        if not path.is_file():
            return {"protected": [], "retained": [], "dropped": []}
        return json.loads(path.read_text(encoding="utf-8"))

    # -- the working directory ---------------------------------------------- #
    def test_the_synthesis_unit_gets_a_disposable_copy_and_not_the_snapshot(self):
        _token, work = self.prepared("s1")
        # Resolved on both sides: macOS answers /var with /private/var and Windows hands
        # back 8.3 short names, so the two spellings of one directory differ as strings.
        self.assertNotEqual(driver._resolved(work), driver._resolved(self.snapshot),
                            "the synthesis worker was handed the pinned snapshot to "
                            "write in, which is the tree the whole run is measured against")
        self.assertEqual(driver._resolved(work.parent),
                         driver._resolved(self.rundir / "work"))
        (work / "a.py").write_text("the synthesizer rewrote this\n", encoding="utf-8")
        (work / "notes.md").write_text("and left this behind\n", encoding="utf-8")
        self.assertEqual(driver.verify_snapshot(self.rundir), [],
                         "a synthesis worker's writes reached the snapshot, which refuses "
                         "the run at the next check")

    def test_the_synthesis_unit_is_launched_with_the_slots_write_capable_command(self):
        """The copy is half of it. A worker launched under the read-only command line is
        one whose runtime refuses the writes its brief told it to make, so the round fails
        on a permission rather than on anything about the defects."""
        marker = "--this-one-may-write"
        mode = _stub_mode(self.stub, self.control)
        self.write_adapter(A={"write_capable": dict(
            mode, permission="the write-capable permission",
            command=[*mode["command"], marker])})
        run = self.run_object()
        # Nothing is launched: the record under test is written before the launch, and a
        # real supervisor started here would outlive the test.
        with mock.patch.object(driver.subprocess, "Popen",
                               side_effect=OSError("not launched by this test")):
            self.assertTrue(run.spawn(run.unit_row("s1")))
        record = json.loads((self.rundir / "dispatch" / "s1" / "a0" / "argv.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(record["permission"], "the write-capable permission",
                         "the synthesis unit ran under the slot's read-only permission")
        self.assertIn(marker, record["argv"])
        self.assertEqual(driver._resolved(Path(record["cwd"]).parent),
                         driver._resolved(self.rundir / "work"),
                         "the spawn recorded the snapshot as the working directory")

    # -- the copy, on the rules a verifier's copy already has ---------------- #
    def test_the_synthesis_unit_reserves_the_run_wide_writer_slot(self):
        """Serialized with the verifiers and the probe, run-wide: a synthesis worker that
        may write shares the ports, caches and credentials they share."""
        run = self.run_object()
        held, _probe = run.claim_decision(run.unit_row("s1"), {}, 1, run.providers(), set())
        self.assertFalse(held, "a synthesis unit was claimed beside a running writer")
        free, _probe = run.claim_decision(run.unit_row("s1"), {}, 0, run.providers(), set())
        self.assertTrue(free, "the writer count, and not something else, held it back")
        self.attempt("s2", 0, argv={"token": "0" * 16, "slot": "A",
                                    "kind": review_panel.SYNTHESIZER_KIND})
        self.assertEqual(self.run_object().writers_running(), 1,
                         "a synthesis attempt in flight was not counted as a writer")

    def test_the_synthesis_unit_is_not_claimed_below_the_disk_floor(self):
        """A copy is a whole snapshot, so the volume has to have room for one. Below the
        floor the unit is held back and charged nothing, never failed."""
        run = self.run_object(disk_floor=1 << 62)
        self.assertTrue(run.headroom())
        claim, _probe = run.claim_decision(run.unit_row("s1"), {}, 0,
                                           run.providers(), set())
        self.assertFalse(claim, "a synthesis unit was claimed with no room for its copy")
        self.assertIn("a working copy needs", run._blocked_on_disk)
        self.assertEqual(driver.read_attempts(self.rundir, "s1", grace=120.0), [],
                         "the volume became the unit's answer")

    def test_the_synthesis_copy_is_protected_while_its_worker_may_still_be_in_it(self):
        """The supervisor detaches its worker, so terminality alone is not enough to delete
        a directory a process may still be running in. An operator failed this attempt and
        could not say the worker had stopped."""
        token, work = self.prepared("s1")
        self.attempt("s1", 0, status=None,
                     argv={"token": token, "kind": review_panel.SYNTHESIZER_KIND,
                           "slot": "A"},
                     resolution={"action": "fail", "reason": "nobody could account for it",
                                 "stopped_confirmed": False},
                     disposition={"attempt": "a0", "unit": "s1",
                                  "outcome": driver.OPERATOR_FAILED,
                                  "stopped_confirmed": False,
                                  "intended_publication": "error"})
        run = self.run_object(keep_repro_bytes=1)
        run.adopt()
        run.adopt()          # carried across a second pass, and to the end of the run
        self.assertEqual([row["unit"] for row in self.record()["protected"]], ["s1"],
                         "the run is holding no copy for this unit, so there was none to "
                         "protect")
        self.assertTrue((work / "a.py").is_file(),
                        "a working directory was removed out from under a live worker")
        self.assertEqual(self.record()["dropped"], [],
                         "a protected copy was counted against the retention cap")

    def test_the_synthesis_copy_enters_the_retention_record_and_the_cap(self):
        """A reply that is not JSON keeps its copy — the asymmetry §5.4 states, and the
        same one a verifier's copy is kept by. Kept is not unbounded: the cap drops it and
        names it, so a reader who finds it missing has somewhere to look."""
        token, work = self.prepared("s1")
        self.landed_attempt("s1", token, reply="the worker answered in prose\n")
        run = self.run_object(keep_repro_bytes=1)
        run.adopt()
        dropped = [(row["unit"], row["removed"]) for row in self.record()["dropped"]]
        self.assertEqual(dropped, [("s1", True)],
                         "the synthesis copy was in no retention record, so nothing "
                         "bounds it and nothing accounts for it")
        self.assertFalse(work.exists())
        self.assertIn("dropped over the retention cap: s1", status_text(run))

    def test_the_synthesis_copy_goes_when_its_unit_lands(self):
        """The ordinary case: the reply names no run that was executed, so there is no
        archive to keep and the copy is released with the rest of the worker's directories.
        """
        token, work = self.prepared("s1")
        self.assertNotEqual(driver._resolved(work), driver._resolved(self.snapshot),
                            "the synthesis unit was never given a copy to throw away")
        self.landed_attempt(
            "s1", token,
            reply=json.dumps({"tiers": [], "defects": [], "summary": "synthesized"}))
        self.run_object().adopt()
        self.assertFalse((self.rundir / "work" / token).exists(),
                         "the disposable copy outlived the unit it was made for")
        self.assertFalse((self.rundir / "in" / token).exists())


# --------------------------------------------------------------------------- #
# 7.4 disk headroom — stop claiming, never fail a unit for the volume
# --------------------------------------------------------------------------- #
class DiskHeadroomStopsClaimingRatherThanFailing(_Case):

    def setUp(self):
        super().setUp()
        # At the verification stage, so the round this run enters is the one whose units
        # need a writable copy. A reading round would not ask the question at all.
        self.fake_units(("verify-area-01-A", "A"), ("u2", "B"),
                        stage=review_panel.VERIFICATION_STAGE)
        doc = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        doc["units"][0]["kind"] = review_panel.VERIFIER_KIND
        (self.rundir / "units.json").write_text(json.dumps(doc), encoding="utf-8")
        for unit in ("verify-area-01-A", "u2"):
            (self.rundir / "units" / unit).mkdir(parents=True, exist_ok=True)
        self.fake_snapshot()

    def test_the_retention_cap_is_enforced_before_the_round_is_held_back(self):
        """A copy over `--keep-repro-bytes` is already declared surplus, so the space it
        holds is reclaimable — and a round stopped for room that a sweep would have freed
        waits for an operator who has nothing to do. `sweep_copies` runs on the full pass
        only, because it measures every copy and a round's polls are scoped; short of room is
        the one moment worth paying for it, once."""
        run = self.run_object(disk_floor=1 << 62)
        swept = []
        run.sweep_copies = lambda: swept.append(True)
        run._swept_for_room = False
        run.claim_decision(run.unit_row("verify-area-01-A"), {}, 0, run.providers(), set())
        self.assertEqual(len(swept), 1, "the cap was never enforced before holding the round")
        # Once per round, not per poll: the sweep measures every copy on disk.
        run.claim_decision(run.unit_row("verify-area-01-A"), {}, 0, run.providers(), set())
        self.assertEqual(len(swept), 1, "the sweep ran again inside the same round")

    def test_the_sweep_runs_once_per_round_and_not_once_per_poll(self):
        """The same rule, asked of the loop that owns the flag rather than of one decision.

        A round is many polls, and the flag is reset where the round begins. Reset where
        each POLL begins instead, a round held back for room with work still in flight
        measured every copy on disk at every poll interval — which is the cost the full
        pass keeps off the polls on purpose, and the test above cannot see, because it never
        enters the loop."""
        run = self.run_object(disk_floor=1 << 62, poll=0.01)
        swept = []
        run.sweep_copies = lambda: swept.append(True)
        # Two polls: something is still in flight on the first look and gone on the second,
        # and the disk is short at both.
        run._in_flight = mock.Mock(side_effect=[True, False])
        run.dispatch_round((review_panel.VERIFIER_KIND,))
        self.assertEqual(len(swept), 1, f"the sweep ran {len(swept)} times in one round")

    def test_a_write_capable_unit_is_not_claimed_below_the_floor(self):
        run = self.run_object(disk_floor=1 << 62)
        self.assertTrue(run.headroom())
        claim, _probe = run.claim_decision(run.unit_row("verify-area-01-A"),
                                           {}, 0, run.providers(), set())
        self.assertFalse(claim)
        # The reader is untouched: nothing about it needs a copy.
        claim, _probe = run.claim_decision(run.unit_row("u2"), {}, 0,
                                           run.providers(), set())
        self.assertTrue(claim)

    def test_a_unit_held_back_for_room_charges_nothing_and_is_not_failed(self):
        run = self.run_object(disk_floor=1 << 62, poll=0.05)
        code = run.loop()
        self.assertEqual(code, driver.EXIT_STOPPED)
        self.assertIn("not enough room", run.pause_reason)
        unit = "verify-area-01-A"
        self.assertEqual(driver.read_attempts(self.rundir, unit, grace=120.0), [],
                         "a unit was claimed with no room to run it")
        self.assertIsNone(driver.landed(self.rundir, unit),
                          "the volume became the unit's answer")
        self.assertIn("no write-capable unit can be claimed", status_text(run))

    def test_a_unit_that_needs_no_copy_is_not_asked_about_the_disk(self):
        """A write-capable unit that has already landed needs no working copy. Asked about
        the volume anyway, it would record the run as held back for room nothing wanted —
        and a round where every remaining unit is finished would stop instead of ending."""
        unit = "verify-area-01-A"
        self.attempt(unit, 0, status='{"status": "ok"}',
                     disposition={"attempt": "a0", "outcome": driver.ACCEPTED,
                                  "intended_publication": "result"})
        (self.rundir / "units" / unit / review_panel.RESULT_NAME).write_text(
            '{"verdicts": [], "summary": "x"}', encoding="utf-8")
        (self.rundir / "dispatch" / unit / "landed.json").write_text(
            json.dumps({"publication": "result", "attempt": "a0"}), encoding="utf-8")
        run = self.run_object(disk_floor=1 << 62)
        run.claim_decision(run.unit_row(unit), {}, 0, run.providers(), set())
        self.assertEqual(run._blocked_on_disk, "",
                         "a finished unit was counted as one waiting for room")

    def test_a_host_that_will_not_answer_is_not_a_reason_to_stop(self):
        """`None` is not zero. A host that cannot report free space has not said the disk is
        full, and refusing on it would stop every write-capable unit of the run."""
        run = self.run_object()
        with mock.patch.object(driver, "free_bytes", lambda _path: None):
            self.assertEqual(run.headroom(), "")


# --------------------------------------------------------------------------- #
# the one rule under §§3.3, 5.3, 5.4 and 7: a read answers or it raises
# --------------------------------------------------------------------------- #
def _refusing(name, match, exc):
    """Patch ``name`` on ``Path`` so every call whose path holds ``match`` raises ``exc``.

    The host refusing one read, with the file still on disk — which is the whole distinction
    these tests are about, and the reason they inject rather than chmod: a directory made
    unreadable with a mode bit is not read-only to root, and half the suite's platforms do
    not mean by a mode bit what POSIX means.
    """
    real = getattr(Path, name)

    def refuse(self, *args, **kwargs):
        if match in str(self):
            raise exc
        return real(self, *args, **kwargs)

    return mock.patch.object(Path, name, autospec=True, side_effect=refuse)


def _refusing_os(name, match, exc):
    """Patch ``os.<name>`` so every call whose path holds ``match`` raises ``exc``.

    The `os` counterpart of :func:`_refusing`, and what the guards below have to be probed
    with. Whether something is there, and what it is, is asked of `os.stat` and `os.listdir`
    rather than of `Path.exists` and its siblings. Which refusals those suppress into a plain
    False has moved between interpreter releases and none of them has ever told a refusal
    from an absence, so a guard written on them cannot be probed at all: the injection lands
    somewhere the guard does not look, and the probe comes back green with the defect still
    there.

    **Both spellings of the path are matched, and that is not belt and braces.** A guard
    resolves its path before it asks the host about it, so the call this sees carries the
    RESOLVED name while a caller passes the name it holds — and the two differ wherever the
    host keeps a second spelling: Windows hands back 8.3 short names (`RUNNER~1`) and macOS
    answers `/var` with `/private/var`. Matching only what the caller passed misses every
    such call, and a probe that injects nothing is a test asserting the guard it never
    reached. The resolution happens here, before the patch is in place, so nothing inside
    the hook goes back to the host for it.
    """
    real = getattr(os, name)
    resolved = str(driver._resolved(Path(match)))

    def refuse(path, *args, **kwargs):
        spelt = str(path)
        if match in spelt or resolved in spelt:
            raise exc
        return real(path, *args, **kwargs)

    return mock.patch.object(os, name, autospec=True, side_effect=refuse)


class AStageReadThatFailedIsNotAFailedUnit(_Case):
    """The driver asks whether a stage met the volume only when the stage exits non-zero,
    so a stage that reads a landed `result.json` through a failing device has to refuse
    rather than record that unit as failed and exit 0. Recorded, the finding is gone from
    every later round and from the report, and nothing anywhere says why.
    """

    def landed_reading_round(self):
        """A planned run whose every reading unit has landed an answer, so the loop reaches
        `route` with nothing to dispatch. Real units and a real snapshot: the question is
        about the engine's own read, so a hand-built listing would test the wrong program.

        **The attempt record goes in beside the landing**, because that is the only shape a
        real run produces: `landed.json` names the disposition that published the answer,
        and a run whose slots have no proven execution anywhere is one the driver refuses at
        the boundary before it dispatches. Landing without one would be testing the storage
        fault against a run directory no run could have left.
        """
        self.plan_only()
        doc = json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))
        for unit in doc["units"]:
            (self.rundir / "units" / unit["id"] / review_panel.RESULT_NAME).write_text(
                json.dumps({"findings": [], "summary": "nothing to report here"}),
                encoding="utf-8")
            self.attempt(unit["id"], 0, status='{"status": "ok"}',
                         disposition={"attempt": "a0", "unit": unit["id"],
                                      "outcome": driver.ACCEPTED,
                                      "intended_publication": "result"})
            (self.rundir / "dispatch" / unit["id"] / "landed.json").write_text(
                json.dumps({"publication": "result", "attempt": "a0"}), encoding="utf-8")
        return [unit["id"] for unit in doc["units"]
                if unit["kind"] == review_panel.READER_KIND]

    def test_a_result_the_device_would_not_give_up_stops_the_stage_and_pauses_the_run(self):
        readers = self.landed_reading_round()
        self.assertTrue(readers, "the planned run has no reading unit to fail on")
        target = str(Path(review_panel.UNITS_DIR) / readers[0] / review_panel.RESULT_NAME)
        run = self.run_object(poll=0.05)
        with _refusing("read_text", target, OSError(errno.EIO, "Input/output error")):
            code, out, err = driver._engine_stage("route", str(self.rundir))
            self.assertNotEqual(code, 0, "the stage adjudicated a unit it could not read")
            self.assertTrue(driver._names_storage_fault(err or out),
                            f"the refusal does not name the fault: {err or out}")
            self.assertEqual(run.loop(), driver.EXIT_STOPPED)
        self.assertIn("could not read or write", run.pause_reason)
        self.assertEqual(
            json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))["stage"],
            review_panel.READING_STAGE,
            "the round was committed over a unit whose answer nobody could read")
        self.assertFalse((self.rundir / review_panel.CANDIDATES_FILE_NAME).exists(),
                         "candidates were written from a reading round with a hole in it")
        # With the device answering again the same stage commits, so what stopped it was the
        # failed read and not a run directory it was never going to accept.
        code, out, err = driver._engine_stage("route", str(self.rundir))
        self.assertEqual(code, 0, err or out)
        self.assertNotEqual(
            json.loads((self.rundir / "units.json").read_text(encoding="utf-8"))["stage"],
            review_panel.READING_STAGE)


    def test_a_guard_that_met_the_device_carries_the_number_it_is_classified_by(self):
        """A stage's refusal reaches this program as TEXT — the exception is gone by then —
        and a fault of the volume is told from every other refusal by reading it. `EIO`
        written as its translation alone is "Input/output error", which names nothing this
        matches, so a transient device fault examining a stage's output ends a completed run
        instead of pausing one that a resume would step straight past. The translation is
        also the C library's, and a locale may have changed it.
        """
        self.plan_only()
        job = review_panel.load_job(self.job_path)
        failing = OSError(errno.EIO, "Input/output error", str(self.rundir))
        with _refusing_os("lstat", str(self.rundir), failing):
            with self.assertRaises(review_panel.ReviewPanelError) as ctx:
                review_panel.check_rundir(self.rundir, job)
        self.assertTrue(driver._names_storage_fault(str(ctx.exception)),
                        f"the guard's refusal names no fault: {ctx.exception}")
        with _refusing("read_text", review_panel.UNITS_FILE_NAME, failing):
            with self.assertRaises(review_panel.ReviewPanelError) as ctx:
                review_panel.reported(self.rundir)
        self.assertTrue(driver._names_storage_fault(str(ctx.exception)),
                        f"the listing's refusal names no fault: {ctx.exception}")

    def test_a_refusal_that_is_not_the_volumes_is_still_a_refusal(self):
        """The other half, or every failed read would pause the run and none would refuse
        it. A permission is a stable fact of the file: the run stops, and it stops as the
        thing an operator has to go and change rather than as a disk to make room on."""
        self.plan_only()
        job = review_panel.load_job(self.job_path)
        denied = PermissionError(errno.EACCES, "Permission denied", str(self.rundir))
        with _refusing_os("lstat", str(self.rundir), denied):
            with self.assertRaises(review_panel.ReviewPanelError) as ctx:
                review_panel.check_rundir(self.rundir, job)
        self.assertFalse(driver._names_storage_fault(str(ctx.exception)),
                         f"a permission was read as the volume failing: {ctx.exception}")


class AnUnreadableThingIsNotAnAbsentThing(_Case):
    """Every read here answers or raises, and ``None``, ``[]`` and "skipped" mean the thing
    is genuinely not there.

    A refusal answered as an absence is a confident negative to a question nobody asked the
    disk, and each of these is one of the things that buys: a live worker's directories
    deleted, a unit charged for a volume, a deletion record replaced by an empty one, a
    read-only copy handed to a worker as a writable one.
    """

    def setUp(self):
        super().setUp()
        self.fake_units(("u1", "A"), ("u2", "B"))
        self.fake_snapshot()

    # -- the rule itself ----------------------------------------------------- #
    def test_absent_and_refused_are_two_different_answers(self):
        path = self.rundir / "dispatch" / "u1" / "a0" / "argv.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.assertIsNone(driver._read_json(path), "a file that is not there is not absent")
        path.write_text("{ this is not json", encoding="utf-8")
        self.assertIsNone(driver._read_json(path),
                          "a record a killed process half-wrote must still read as nothing")
        for cause, expected in ((OSError(errno.EIO, "Input/output error"),
                                 driver.StorageFault),
                                (PermissionError(errno.EACCES, "Permission denied"),
                                 driver.UnreadableRecord)):
            with self.subTest(errno=cause.errno):
                with _refusing("read_text", "argv.json", cause):
                    with self.assertRaises(expected):
                        driver._read_json(path)
        # Both are the same stop as far as a caller is concerned, and both are resumable.
        self.assertTrue(issubclass(driver.StorageFault, driver.RunPaused))
        self.assertTrue(issubclass(driver.UnreadableRecord, driver.RunPaused))

    def test_a_record_the_check_could_not_read_is_not_a_reply_the_engine_rejected(self):
        """§7.0's distinction at the one place it costs a unit its answer. `check_result`
        reads the run's own metadata, and a permission taken off `areas.json` — or a sharing
        violation on it — arrives here wearing the same exception type as a reply the engine
        refused. Charged as that it spends the reply allowance on two replies nobody ever
        looked at and then publishes `error.txt` as the unit's own answer, and restoring the
        access recovers none of it.

        Injected into the engine's real read rather than into `check_result` itself, because
        the defect is that the driver cannot tell those two failures apart, and a fake that
        raised the refusal directly would prove only that the fake was built right.
        """
        (self.rundir / "areas.json").write_text(json.dumps(
            {"areas": [{"id": "area-01", "files": ["a.py"]}]}), encoding="utf-8")
        reply = '{"findings": [], "summary": "read the area"}'
        self.attempt("u1", 0, status='{"status": "ok"}', transcript=reply)
        with _refusing("read_text", "areas.json",
                       PermissionError(errno.EACCES, "Permission denied")):
            with self.assertRaises(driver.UnreadableRecord):
                self.run_object().adopt()
        attempt = driver.read_attempts(self.rundir, "u1", grace=120.0)[0]
        self.assertIsNone(attempt.disposition,
                          "a read nobody could make adjudicated the attempt anyway")
        # The access restored, the same reply is checked and lands: nothing was spent and
        # nothing was decided while the host was refusing.
        self.run_object().adopt()
        record = driver.read_attempts(self.rundir, "u1", grace=120.0)[0].disposition
        self.assertEqual(record["outcome"], driver.ACCEPTED)
        self.assertEqual(driver.replay([record], 0).reply_charges, 0)

    def test_a_read_the_host_refused_exits_resumable_rather_than_refusing_the_run(self):
        """Exit 2 says this run cannot be continued and exit 3 says not yet. A caller
        scripting this restarts one and gives up on the other, so a fault that a resume
        steps straight past must not arrive as the code that means the run is over."""
        self.plan_only()
        with _refusing("read_text", "units.json",
                       OSError(errno.EIO, "Input/output error")):
            self.assertEqual(driver.main(
                ["--rundir", str(self.rundir), "--adapter", str(self.adapter_path),
                 "--job", str(self.job_path), "--go"]), driver.EXIT_STOPPED)

    # -- B2: the enumeration that finds the record --------------------------- #
    def test_an_enumeration_that_failed_is_not_a_unit_with_no_attempts(self):
        """`attempt_dirs` answering `[]` tells eligibility there is nothing running, the
        capacity count that nothing is reserved, and the reclamation sweep that no attempt
        names these directories. All three are wrong at once: a second worker starts on a
        unit that already has one, and the first one's input, output and working directories
        are deleted out from under it.
        """
        token = f"{5:016x}"
        for area in ("in", "out", "work"):
            (self.rundir / area / token).mkdir(parents=True)
        self.attempt("u1", 0, argv={"token": token})       # claimed, running, no status
        failing = OSError(errno.EIO, "Input/output error")
        with _refusing("iterdir", str(Path("dispatch") / "u1"), failing):
            with self.assertRaises(driver.StorageFault):
                driver.attempt_dirs(self.rundir, "u1")
            run = self.run_object(poll=0.05)
            self.assertEqual(run.loop(), driver.EXIT_STOPPED)
        self.assertIn("could not be enumerated", run.pause_reason)
        for area in ("in", "out", "work"):
            self.assertTrue((self.rundir / area / token).is_dir(),
                            f"{area}/ was reclaimed on an enumeration nobody could make")
        self.assertEqual(self.stub_invocations(), 0,
                         "a second attempt was claimed for a unit whose first was invisible")

    def test_a_grant_directory_that_cannot_be_listed_is_not_a_unit_with_no_grants(self):
        """An operator's grant of further launches is durable and replayed. Read as none,
        the unit stays quarantined at a ceiling the operator has already raised.

        Probed through `os.listdir`, because that is what the listing is: `Path.glob`
        swallows a directory it may not list and hands back the empty set, so a refusal
        aimed at it never reaches the code and the probe comes back green."""
        base = self.rundir / "dispatch" / "u1"
        base.mkdir(parents=True, exist_ok=True)
        (base / "grant-1.json").write_text(json.dumps({"launches": 4}), encoding="utf-8")
        run = self.run_object()
        self.assertEqual(run.granted("u1"), 4)
        # A directory that is genuinely not there holds no grants, and that half stays.
        self.assertEqual(self.run_object().granted("u2"), 0)
        with _refusing_os("listdir", str(base),
                          OSError(errno.EIO, "Input/output error", str(base))):
            with self.assertRaises(driver.StorageFault):
                self.run_object().granted("u1")

    # -- B3: a launch the volume stopped ------------------------------------- #
    def test_a_launch_the_volume_stopped_charges_no_failure_allowance(self):
        """§5.3 gives `launch-failed` a failure allowance because it means the supervisor
        could not start — not because the disk broke. Charged, three of them publish
        `error.txt` as this unit's answer while the thing to fix is the volume."""
        run = self.run_object()
        with mock.patch.object(driver.subprocess, "Popen", side_effect=OSError(
                errno.EIO, "Input/output error", str(self.rundir))):
            self.assertTrue(run.spawn(run.unit_row("u1")))
        record = driver.read_attempts(self.rundir, "u1", grace=120.0)[0].disposition
        self.assertEqual(record["outcome"], driver.INFRASTRUCTURE)
        self.assertTrue(record["storage_fault"])
        self.assertIs(record["launched"], False)
        budget = driver.replay([record], 0)
        self.assertEqual(budget.failure_charges, 0)
        self.assertEqual(budget.charging, 0)
        self.assertIsNone(driver.landed(self.rundir, "u1"),
                          "a failing volume became the unit's answer")
        self.assertTrue(run.pause_reason, "a storage fault at the launch did not pause")

    def test_a_launch_that_failed_for_any_other_reason_still_charges(self):
        """The other half, or the rule above would excuse every failed launch: a supervisor
        that is not where the configuration says it is has to spend the allowance §5.3 gives
        it, or the unit would retry against it for ever."""
        run = self.run_object()
        with mock.patch.object(driver.subprocess, "Popen",
                               side_effect=OSError(errno.ENOENT, "No such file")):
            self.assertTrue(run.spawn(run.unit_row("u1")))
        record = driver.read_attempts(self.rundir, "u1", grace=120.0)[0].disposition
        self.assertEqual(record["outcome"], driver.LAUNCH_FAILED)
        self.assertEqual(driver.replay([record], 0).failure_charges, 1)
        self.assertFalse(run.pause_reason, "a missing supervisor paused the whole run")

    def test_a_launch_that_never_happened_holds_no_slot_and_protects_no_copy(self):
        """Whatever it is adjudicated as, nothing started — so the attempt must not reserve
        its slot or carry a protected working directory to the end of the run, waiting on a
        worker that does not exist."""
        for exc, outcome in ((OSError(errno.EIO, "Input/output error"),
                              driver.INFRASTRUCTURE),
                             (OSError(errno.ENOENT, "No such file"),
                              driver.LAUNCH_FAILED)):
            with self.subTest(outcome=outcome):
                unit = "u1" if outcome == driver.INFRASTRUCTURE else "u2"
                run = self.run_object()
                with mock.patch.object(driver.subprocess, "Popen", side_effect=exc):
                    run.spawn(run.unit_row(unit))
                attempt = driver.read_attempts(self.rundir, unit, grace=120.0)[0]
                self.assertEqual(attempt.disposition["outcome"], outcome)
                self.assertTrue(driver._execution_ended(attempt))
                self.assertFalse(driver._reserves_capacity(attempt))

    def test_a_status_file_that_could_not_be_created_records_that_nothing_launched(self):
        """The spawn is not the only step that can fail before a worker exists: the status
        record the supervisor writes into is opened first. A failure there lands after the
        attempt directory and a complete `argv.json` are on disk, so a refusal that recorded
        nothing leaves an attempt that reads as running, then `uncertain`, and asks an
        operator to reconcile a worker that never existed."""
        run = self.run_object()
        real_open = driver.open if hasattr(driver, "open") else open

        def full(path, *args, **kwargs):
            if Path(path).name == driver.STATUS_NAME:
                raise OSError(errno.ENOSPC, "No space left on device", str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch.object(driver, "open", full, create=True):
            self.assertTrue(run.spawn(run.unit_row("u1")))
        attempt = driver.read_attempts(self.rundir, "u1", grace=120.0)[0]
        self.assertEqual(attempt.disposition["outcome"], driver.INFRASTRUCTURE)
        self.assertIs(attempt.disposition["launched"], False)
        self.assertEqual(driver.replay([attempt.disposition], 0).charging, 0)
        self.assertNotIn(("u1", attempt.name), self.unaccountable(),
                         "an attempt that never launched was left for an operator")
        self.assertTrue(run.pause_reason, "a full disk at the status file did not pause")

    # -- B4: the copy a write-capable unit runs in --------------------------- #
    def test_a_permission_that_could_not_be_restored_fails_the_preparation(self):
        """§5.4 requires the copy to be created writable. Suppressed, this returns success
        over a copy that is still read-only: the worker cannot write its own fixtures, and
        that failure is charged to it as its behavior."""
        unit = dict(self.run_object().unit_row("u1"), kind=review_panel.VERIFIER_KIND)
        # Hardened, as a committed run's snapshot is, so the only chmod that asks for write
        # permission is the restoration being tested — `copytree` copies the source's modes,
        # and those no longer carry it.
        driver.harden_snapshot(self.rundir / "snapshot")
        real_chmod = driver.os.chmod
        for cause, expected in ((OSError(errno.EROFS, "Read-only file system"),
                                 driver.StorageFault),
                                (PermissionError(errno.EPERM, "Operation not permitted"),
                                 driver.UnreadableRecord)):
            with self.subTest(errno=cause.errno):
                run = self.run_object()

                def refusing_write(path, mode, *args, _cause=cause, **kwargs):
                    if mode & stat.S_IWUSR:
                        raise _cause
                    return real_chmod(path, mode, *args, **kwargs)

                with mock.patch.object(driver.os, "chmod", refusing_write):
                    with self.assertRaises(expected):
                        run.spawn(unit)
                self.assertEqual(driver.attempt_dirs(self.rundir, "u1"), [],
                                 "an attempt was claimed over a copy that is not writable")
                self.assertEqual(self.stub_invocations(), 0)

    def test_a_file_that_vanished_under_the_walk_is_nothing_to_restore(self):
        """The other half: only an absence is passed over, and the copy is still made
        writable. A rule that raised on everything would fail a preparation whenever a
        temporary file went away between the walk and the chmod."""
        unit = dict(self.run_object().unit_row("u1"), kind=review_panel.VERIFIER_KIND)
        driver.harden_snapshot(self.rundir / "snapshot")
        real = driver.os.chmod
        gone = {"done": False}

        def vanishing(path, mode, *args, **kwargs):
            if not gone["done"] and mode & stat.S_IWUSR and Path(path).is_file():
                gone["done"] = True
                raise FileNotFoundError(errno.ENOENT, "No such file", str(path))
            return real(path, mode, *args, **kwargs)

        with mock.patch.object(driver.os, "chmod", vanishing):
            _inbox, _outbox, work = driver.prepare_worker_paths(
                self.rundir, unit, driver.reserve_token(self.rundir))
        self.assertTrue(gone["done"], "the vanishing file was never reached")
        self.assertTrue(os.access(work, os.W_OK))

    # -- B5: the questions asked of a path rather than of its contents -------- #
    def test_an_attempt_directory_nobody_can_describe_is_not_an_entry_that_is_no_attempt(self):
        """The guarded listing finds `a0`; this is the question one level in. Dropped here,
        the attempt is dropped from eligibility, from the capacity count and from the
        reclamation sweep — the same three wrong answers at once.
        """
        self.attempt("u1", 0)
        (self.rundir / "dispatch" / "u1" / "notes.txt").write_text("x", encoding="utf-8")
        # Genuinely not a directory: passed over, and that is right.
        self.assertEqual([p.name for p in driver.attempt_dirs(self.rundir, "u1")], ["a0"])
        a0 = self.rundir / "dispatch" / "u1" / "a0"
        with _refusing_os("stat", str(a0), PermissionError(
                errno.EACCES, "Permission denied", str(a0))):
            with self.assertRaises(driver.UnreadableRecord):
                driver.attempt_dirs(self.rundir, "u1")

    def test_a_terminal_file_nobody_can_describe_is_not_a_unit_with_no_answer(self):
        """A unit is exactly one of complete or failed, and both guards on that are asked of
        a path. Read as "nothing is there", a refusal lands the opposite file beside the one
        already on disk — the one state the protocol has no reading for."""
        target = self.rundir / "units" / "u1"
        target.mkdir(parents=True, exist_ok=True)
        (self.rundir / "dispatch" / "u1").mkdir(parents=True, exist_ok=True)
        # Nothing there yet, so the error lands: the absent half.
        driver.publish(self.rundir, "u1", None, "error", "spent its allowances\n")
        self.assertTrue((target / review_panel.ERROR_NAME).is_file())
        result = target / review_panel.RESULT_NAME
        with _refusing_os("stat", str(result), OSError(
                errno.EIO, "Input/output error", str(result))):
            with self.assertRaises(driver.StorageFault):
                driver.publish(self.rundir, "u1", None, "error", "again\n")

    def test_a_publication_nobody_can_describe_is_not_an_unlanded_unit(self):
        """`landed` proves the record by the file it names. Answered "not there", the
        adoption pass publishes the unit's answer a second time."""
        record = self.rundir / "dispatch" / "u1" / "landed.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"publication": "result", "attempt": "a0"}),
                          encoding="utf-8")
        # The record names a file that is genuinely not there: not landed, and the next pass
        # finishes the publication. That half must keep working.
        self.assertIsNone(driver.landed(self.rundir, "u1"))
        target = self.rundir / "units" / "u1"
        target.mkdir(parents=True, exist_ok=True)
        (target / review_panel.RESULT_NAME).write_text("{}", encoding="utf-8")
        self.assertIsNotNone(driver.landed(self.rundir, "u1"))
        with _refusing_os("stat", str(target / review_panel.RESULT_NAME),
                          PermissionError(errno.EACCES, "Permission denied")):
            with self.assertRaises(driver.UnreadableRecord):
                driver.landed(self.rundir, "u1")

    def test_a_reply_nobody_can_describe_never_decides_that_a_copy_is_not_evidence(self):
        """False from `keeps_evidence` DELETES the working copy the verdict reproduces from.
        A reply that is not JSON already keeps its copy for that reason; a reply the host
        refuses must not take the one path that skips the read entirely."""
        attempt = driver.Attempt(
            unit="u1", index=0, path=self.attempt("u1", 0), argv={"token": f"{7:016x}"},
            status=None, disposition=None, resolution=None, state=driver.RUNNING)
        # Genuinely no reply: nothing claims this copy is evidence.
        self.assertFalse(driver.keeps_evidence(attempt))
        reply = attempt.path / driver.REPLY_NAME
        reply.write_text("this is not json at all", encoding="utf-8")
        self.assertTrue(driver.keeps_evidence(attempt), "the unreadable-JSON half moved")
        with _refusing_os("stat", str(reply), PermissionError(
                errno.EACCES, "Permission denied", str(reply))):
            with self.assertRaises(driver.UnreadableRecord):
                driver.keeps_evidence(attempt)

    def test_a_drain_flag_nobody_can_describe_does_not_read_as_no_stop_requested(self):
        """The run's only control. Passed over, the driver keeps claiming after an operator
        asked it to stop — and the operator, having used the control exactly as documented,
        cannot tell that from a driver that has not looked yet."""
        run = self.run_object()
        self.assertFalse(run.stop_requested(), "an unrequested stop was read as requested")
        flag = run.drain_flag
        with _refusing_os("stat", str(flag), PermissionError(
                errno.EACCES, "Permission denied", str(flag))):
            with self.assertRaises(driver.UnreadableRecord):
                self.run_object().stop_requested()
        flag.write_text("", encoding="utf-8")
        self.addCleanup(flag.unlink)
        self.assertTrue(self.run_object().stop_requested())

    def test_a_grant_named_from_a_listing_that_under_counted_replaces_one(self):
        """`Path.glob` answers the empty set for a directory it may not list, so a guard
        written on it cannot refuse at all. The name of the next grant file comes off the
        same listing: under-counted, this grant is written over the one already there and an
        operator's raised ceiling silently goes away."""
        base = self.rundir / "dispatch" / "u1"
        base.mkdir(parents=True, exist_ok=True)
        driver.resolve_unit(self.rundir, "u1", grant=2, fail=False, reason="first")
        driver.resolve_unit(self.rundir, "u1", grant=3, fail=False, reason="second")
        self.assertEqual(len(driver.grant_files(base)), 2, "one grant overwrote the other")
        self.assertEqual(self.run_object().granted("u1"), 5)
        with _refusing_os("listdir", str(base), PermissionError(
                errno.EACCES, "Permission denied", str(base))):
            with self.assertRaises(driver.UnreadableRecord):
                driver.resolve_unit(self.rundir, "u1", grant=1, fail=False, reason="third")

    def test_a_resolution_is_refused_over_a_disposition_nobody_can_read(self):
        """A disposition is written once and never edited. Read as "no record is there", an
        operator's resolution is a second judgment over the first — and with `--retry`, a
        worker started beside the one that disposition had already accounted for."""
        path = self.attempt("u1", 0, disposition={"outcome": driver.WORKER_FAILED})
        with self.assertRaises(driver.DriverError) as ctx:
            driver.resolve_attempt(self.rundir, "u1", "a0", action="fail",
                                   reason="operator says so", stopped_confirmed=True)
        self.assertIn("already adjudicated", str(ctx.exception))
        (path / driver.DISPOSITION_NAME).unlink()
        with _refusing_os("stat", str(path / driver.DISPOSITION_NAME),
                          PermissionError(errno.EACCES, "Permission denied")):
            with self.assertRaises(driver.UnreadableRecord):
                driver.resolve_attempt(self.rundir, "u1", "a0", action="fail",
                                       reason="operator says so", stopped_confirmed=True)
        self.assertFalse((path / driver.RESOLUTION_NAME).exists(),
                         "a resolution was written on a question nobody answered")

    def test_a_deletion_is_recorded_done_only_when_the_directory_is_actually_gone(self):
        """The cap's record is the only account of a directory nothing can enumerate any
        more. An entry marked removed over a path the host would not describe says a tree is
        gone while it is still on disk, and nothing ever looks at it again."""
        token = f"{9:016x}"
        work = self.rundir / "work" / token
        record = self.rundir / "dispatch" / driver.COPIES_RECORD_NAME
        record.parent.mkdir(parents=True, exist_ok=True)

        def write_entry():
            record.write_text(json.dumps({"dropped": [
                {"token": token, "unit": "u1", "attempt": "a0"}]}), encoding="utf-8")

        # Genuinely gone: the entry closes.
        write_entry()
        self.run_object()._finish_dropping()
        self.assertTrue(json.loads(record.read_text(encoding="utf-8"))["dropped"][0]["removed"])
        # There and unexaminable: the run stops rather than closing the entry over it.
        write_entry()
        work.mkdir(parents=True, exist_ok=True)
        with _refusing_os("stat", str(work), PermissionError(
                errno.EACCES, "Permission denied", str(work))):
            with self.assertRaises(driver.UnreadableRecord):
                self.run_object()._finish_dropping()
        self.assertNotIn("removed",
                         json.loads(record.read_text(encoding="utf-8"))["dropped"][0])


class AnAliasDrainFlagIsClearedOnlyWhileItNamesThisRun(unittest.TestCase):
    """The stop control answers at two spellings, so it must clear both.

    `drain_flags` deliberately watches the spelling the operator typed as well as the
    canonical one — a control somebody can use exactly as documented and be ignored by is
    worse than no control. Clearing has to cover the same two, and it has to refuse the one
    case the alias makes possible: an alias repointed at a different run, whose flag is
    somebody else's live request.

    An alias normally lives in a DIFFERENT directory from the run it points at. That is what
    makes it an alias, and it is why the question has to be asked of where the alias leads
    rather than of which directory its flag sits in.
    """

    def _link(self, target, link):
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"cannot create a symlink here: {exc}")

    def test_an_alias_in_another_directory_is_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rundir = tmp / "runs" / "run"; rundir.mkdir(parents=True)
            elsewhere = tmp / "home"; elsewhere.mkdir()
            alias = elsewhere / "current"
            self._link(rundir, alias)
            flag = alias.with_name(alias.name + driver.DRAIN_SUFFIX)
            flag.write_text("", encoding="utf-8")
            driver.clear_drain_request(rundir, alias)
            self.assertFalse(
                flag.exists(),
                "a stale request at the operator's own spelling survived; `drain_flags` goes "
                "on watching it, so every resume through that spelling drains at once")

    def test_an_alias_now_pointing_at_another_run_keeps_its_flag(self):
        """The case the guard exists for: that flag is a live request against a run this
        driver does not own, and clearing it would dispatch straight through somebody's
        stop."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            mine = tmp / "runs" / "mine"; mine.mkdir(parents=True)
            theirs = tmp / "runs" / "theirs"; theirs.mkdir(parents=True)
            alias = tmp / "current"
            self._link(theirs, alias)
            flag = alias.with_name(alias.name + driver.DRAIN_SUFFIX)
            flag.write_text("", encoding="utf-8")
            driver.clear_drain_request(mine, alias)
            self.assertTrue(flag.exists(), "cleared a request made against another run")

    def test_the_canonical_flag_is_always_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            rundir = Path(tmp) / "run"; rundir.mkdir()
            flag = rundir.with_name(rundir.name + driver.DRAIN_SUFFIX)
            flag.write_text("", encoding="utf-8")
            driver.clear_drain_request(rundir, None)
            self.assertFalse(flag.exists())


class ASupervisorGetsItsOwnProcessGroup(unittest.TestCase):
    """Same intent, two spellings. `start_new_session` is POSIX-only; without its Windows
    counterpart every supervisor shared the console's group, so a Ctrl-C meant as a drain
    killed every in-flight worker and charged each a worker failure. The stop request this
    program honors is the drain file, which works everywhere."""

    def test_posix_starts_a_new_session(self):
        with mock.patch.object(driver.os, "name", "posix"):
            self.assertEqual(driver._own_process_group(), {"start_new_session": True})

    def test_windows_creates_a_new_process_group(self):
        with mock.patch.object(driver.os, "name", "nt"):
            kw = driver._own_process_group()
        self.assertNotIn("start_new_session", kw, "a POSIX-only keyword would make Popen raise")
        self.assertEqual(kw.get("creationflags"), 0x00000200, "CREATE_NEW_PROCESS_GROUP")


class ClearingAWorkingCopyStaysInsideIt(unittest.TestCase):
    """`remove_tree` makes a tree writable before removing it, and a chmod is not undone by
    the `rmtree` that follows — `rmtree` will not cross the link either, so anything the
    walk touched on the far side keeps whatever this did to it.

    A link test alone does not bound the walk. `S_ISLNK` names one kind of thing that leads
    out of a tree and the kinds differ by platform, so the bound is containment: a path that
    does not resolve back inside the root is not this run's to modify, whatever kind it is.
    """

    def test_a_file_reached_through_a_link_is_not_made_writable(self):
        """**This passes on POSIX with or without the containment check**, and is kept as the
        invariant rather than as a reproduction. `rglob` does not descend a symlinked
        directory here, so the walk never reaches the far side and there is nothing to chmod.
        The case the check exists for is a Windows directory junction, which is a reparse
        point rather than a symlink and which the walk does descend — and no test on this
        platform can construct one. The Windows job runs this file; it cannot make a junction
        either, so what covers that path is the check being written to need no claim about
        what kind of thing led out of the tree."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = tmp / "copy"; (root / "inner").mkdir(parents=True)
            outside = tmp / "not-ours"; outside.mkdir()
            victim = outside / "read-only.txt"
            victim.write_text("theirs", encoding="utf-8")
            victim.chmod(0o444)
            try:
                (root / "inner" / "link").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"cannot create a symlink here: {exc}")
            before = stat.S_IMODE(victim.stat().st_mode)
            driver.remove_tree(root, root.parent)
            self.assertTrue(victim.exists(), "removed a file outside the working copy")
            self.assertEqual(stat.S_IMODE(victim.stat().st_mode), before,
                             "made a file outside the working copy writable")

    def test_a_root_that_is_itself_a_link_is_neither_walked_nor_removed(self):
        """The half the resolved-root check could not cover, because it asked the tree
        whether it contained itself. A working copy replaced by a link to somewhere else
        answered yes for every path under the far end."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "run").mkdir()
            outside = tmp / "not-ours"; outside.mkdir()
            victim = outside / "read-only.txt"
            victim.write_text("theirs", encoding="utf-8")
            victim.chmod(0o444)
            root = tmp / "run" / "work"
            try:
                root.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"cannot create a symlink here: {exc}")
            before = stat.S_IMODE(victim.stat().st_mode)
            driver.remove_tree(root, tmp / "run")
            self.assertTrue(victim.exists(), "deleted through a redirected root")
            self.assertEqual(stat.S_IMODE(victim.stat().st_mode), before,
                             "made a file writable through a redirected root")

    def test_a_sibling_run_reached_through_a_redirected_root_is_untouched(self):
        """The boundary alone cannot catch this one. A run's `.partial` sits beside the run
        directory, so the directory that holds it holds every OTHER run too — a sibling
        really is inside it. What separates them is that the root does not SIT where it
        claims: it leads somewhere its own parent plus its own name does not."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            runs = tmp / "runs"; runs.mkdir()
            previous = runs / "previous"; previous.mkdir()
            hardened = previous / "snapshot.txt"
            hardened.write_text("another run's", encoding="utf-8")
            hardened.chmod(0o444)
            partial = runs / "new.partial"
            try:
                partial.symlink_to(previous, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"cannot create a symlink here: {exc}")
            before = stat.S_IMODE(hardened.stat().st_mode)
            driver.remove_tree(partial, partial.parent)
            self.assertTrue(hardened.exists(), "deleted another run's file")
            self.assertEqual(stat.S_IMODE(hardened.stat().st_mode), before,
                             "made another run's hardened snapshot writable")

    def test_a_root_that_cannot_be_resolved_is_neither_walked_nor_removed(self):
        """`_resolved` answers with the UNRESOLVED spelling where the host will not resolve,
        which is right where a comparison that cannot be made should simply not match. Here
        both sides fall back together: the root and its parent compare equal lexically, the
        boundary contains the lexical path, both questions pass, and the walk proceeds on a
        tree nothing could place. The reviewer's case is a host that permits traversal while
        refusing `readlink`; the property is the same whatever refused."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "copy"; root.mkdir()
            (root / "f.txt").write_text("mine", encoding="utf-8")
            def refuse(self, *a, **kw):
                raise OSError(errno.ELOOP, "Too many levels of symbolic links")
            with mock.patch.object(Path, "resolve", refuse):
                driver.remove_tree(root, root.parent)
            self.assertTrue((root / "f.txt").exists(),
                            "walked and removed a tree it could not place")

    def test_a_read_only_file_inside_is_still_removed(self):
        """The positive control: bounding the walk must not stop it doing its job."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "copy"; root.mkdir()
            locked = root / "locked.txt"
            locked.write_text("mine", encoding="utf-8")
            locked.chmod(0o444)
            driver.remove_tree(root, root.parent)
            self.assertFalse(root.exists(), "the working copy was not removed")


class AStorageFaultIsNamedByAWholeToken(unittest.TestCase):
    """`eio` is inside `fileio.c`, `audio` and a user called Deion. Matched as a substring,
    an engine rejection that quoted such a path paused the run for a full disk and sent the
    same unit out again on every resume until the launch ceiling. The four errno names are
    whole upper-case tokens; the phrases are several words each and stay as they were."""

    def test_a_path_that_happens_to_contain_the_letters_is_not_a_fault(self):
        for text in ("routing failed: Modules/_io/fileio.c", "run directory /srv/deion/run",
                     "reviewer wrote to /tmp/audio.wav", "field 'verdicts' is not a list"):
            with self.subTest(text=text):
                self.assertFalse(driver._names_storage_fault(text))

    def test_the_real_faults_are_still_named(self):
        for text in ("[Errno 28] No space left on device", "OSError: EIO while reading",
                     "errno 5", "Read-only file system", "Disk quota exceeded", "ENOSPC"):
            with self.subTest(text=text):
                self.assertTrue(driver._names_storage_fault(text))

    def test_a_number_that_merely_starts_with_a_fault_is_not_one(self):
        self.assertFalse(driver._names_storage_fault("errno 51"))


if __name__ == "__main__":
    unittest.main()
