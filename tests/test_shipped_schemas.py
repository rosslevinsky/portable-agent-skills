"""Contract tests for the structured-output schemas the skills ship.

These schemas are handed to a runtime's structured-output flag, so a change to one silently
changes what a spawned model is allowed to return — and the consumer that parses it is
somewhere else entirely. The tests below lock the properties each consumer depends on, and
exercise the discriminated union with real instances.

Stdlib only, like the engines: ``_validate`` is a deliberately small JSON Schema subset
covering exactly the keywords these schemas use. It is TEST-only — enforcement in production
is the CLI's, not ours.

Every file is read with an explicit UTF-8 encoding: the schemas contain em dashes, and the
platform default differs on Windows.
"""

import json
import re
import sys
import unittest
from pathlib import Path

_SKILLS = Path(__file__).resolve().parent.parent / "skills"

sys.path.insert(0, str(_SKILLS.parent / "scripts"))
import validate_cross_runtime as vcr  # noqa: E402
JUDGE_SCHEMA = _SKILLS / "plan-duel" / "judge-schema.json"
REVIEW_SCHEMA = _SKILLS / "diff-review" / "review-schema.json"
WORKER_SCHEMA = _SKILLS / "plan-run" / "references" / "phase-worker-schema.json"
READER_SCHEMA = _SKILLS / "review-panel" / "reader-schema.json"
VERIFIER_SCHEMA = _SKILLS / "review-panel" / "verifier-schema.json"
PROBE_SCHEMA = _SKILLS / "review-panel" / "probe-schema.json"
CLUSTERER_SCHEMA = _SKILLS / "review-panel" / "clusterer-schema.json"
MERGER_SCHEMA = _SKILLS / "review-panel" / "merger-schema.json"
MERGE_CHECKER_SCHEMA = _SKILLS / "review-panel" / "merge-checker-schema.json"
SYNTHESIZER_SCHEMA = _SKILLS / "review-panel" / "synthesizer-schema.json"
TIER_NAMES_SCHEMA = _SKILLS / "review-panel" / "tier-names-schema.json"
OVERVIEW_SCHEMA = _SKILLS / "review-panel" / "overview-schema.json"

_PY_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _validate(instance, schema, path="$"):
    """Return a list of violations of ``schema`` by ``instance`` (empty = valid)."""
    errors = []

    # anyOf is an assertion COMBINED with its siblings, not a replacement for them.
    # Returning early here would let a sibling constraint go unchecked and quietly make
    # the fixtures below pass against a schema a real validator would reject.
    if "anyOf" in schema:
        branch_errors = [_validate(instance, s, path) for s in schema["anyOf"]]
        if not any(not errs for errs in branch_errors):
            errors.append(
                f"{path}: matched none of the {len(branch_errors)} anyOf branches"
            )

    declared = schema.get("type")
    declared = [declared] if isinstance(declared, str) else (declared or [])
    if declared:
        ok = any(
            isinstance(instance, _PY_TYPES[t])
            and not (t == "integer" and isinstance(instance, bool))
            for t in declared
            if t in _PY_TYPES
        )
        if not ok:
            return [f"{path}: expected {declared}, got {type(instance).__name__}"]

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in {schema['enum']}")
    if isinstance(instance, str) and "minLength" in schema:
        if len(instance) < schema["minLength"]:
            errors.append(
                f"{path}: length {len(instance)} < minLength {schema['minLength']}"
            )
    if isinstance(instance, str) and "maxLength" in schema:
        if len(instance) > schema["maxLength"]:
            errors.append(
                f"{path}: length {len(instance)} > maxLength {schema['maxLength']}"
            )
    if isinstance(instance, int) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > maximum {schema['maximum']}")

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required '{key}'")
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in properties:
                    errors.append(f"{path}: unexpected property '{key}'")
        for key, sub in properties.items():
            if key in instance:
                errors.extend(_validate(instance[key], sub, f"{path}.{key}"))

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(
                f"{path}: {len(instance)} items < minItems {schema['minItems']}"
            )
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(_validate(item, schema["items"], f"{path}[{index}]"))

    return errors


class ValidatorSelfTest(unittest.TestCase):
    """The test-only validator must actually reject — else every test below is vacuous."""

    def test_detects_each_violation_kind(self):
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["a"],
            "properties": {
                "a": {"type": "integer", "minimum": 0, "maximum": 10},
                "b": {"type": "string", "enum": ["x"]},
            },
        }
        self.assertEqual(_validate({"a": 5}, schema), [])
        self.assertTrue(_validate({}, schema))                    # missing required
        self.assertTrue(_validate({"a": 5, "z": 1}, schema))      # additional property
        self.assertTrue(_validate({"a": "5"}, schema))            # wrong type
        self.assertTrue(_validate({"a": 99}, schema))             # above maximum
        self.assertTrue(_validate({"a": -1}, schema))             # below minimum
        self.assertTrue(_validate({"a": 5, "b": "y"}, schema))    # outside enum
        self.assertTrue(_validate({"a": True}, schema))           # bool is not an integer

    def test_anyof_requires_a_matching_branch(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertEqual(_validate("s", schema), [])
        self.assertEqual(_validate(3, schema), [])
        self.assertTrue(_validate([], schema))

    def test_anyof_does_not_suppress_sibling_constraints(self):
        # anyOf is combined with its siblings, not a replacement for them. A validator
        # that short-circuits here would pass instances a real one rejects, making
        # every union assertion below vacuous.
        schema = {"type": "string", "anyOf": [{"minLength": 1}, {"minLength": 5}]}
        self.assertEqual(_validate("ok", schema), [])
        self.assertTrue(_validate(3, schema))  # sibling "type" must still be checked

    def test_min_length_is_enforced(self):
        schema = {"type": "string", "minLength": 1}
        self.assertEqual(_validate("x", schema), [])
        self.assertTrue(_validate("", schema))

    def test_max_length_is_enforced(self):
        # Without it a schema's length limit is a keyword this validator skips, and a
        # fixture over the limit passes here while a runtime that enforces it refuses.
        schema = {"type": "string", "maxLength": 3}
        self.assertEqual(_validate("abc", schema), [])
        self.assertTrue(_validate("abcd", schema))

    def test_min_items_is_enforced(self):
        schema = {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}
        self.assertEqual(_validate(["x"], schema), [])
        self.assertTrue(_validate([], schema))        # below minItems
        self.assertTrue(_validate([""], schema))      # an item below its own minLength
        self.assertTrue(_validate([7], schema))       # an item of the wrong type


class PortabilityInvariants(unittest.TestCase):
    """Rules every shipped schema must follow to be accepted by BOTH runtimes."""

    def _all(self):
        return [JUDGE_SCHEMA, REVIEW_SCHEMA, WORKER_SCHEMA, READER_SCHEMA, VERIFIER_SCHEMA,
                PROBE_SCHEMA, CLUSTERER_SCHEMA, MERGER_SCHEMA, MERGE_CHECKER_SCHEMA,
                SYNTHESIZER_SCHEMA, TIER_NAMES_SCHEMA, OVERVIEW_SCHEMA]

    def test_no_dollar_schema_key_anywhere(self):
        # A draft-2020-12 $schema ref is accepted by one runtime and REJECTED by the
        # other before any model call ("no schema with key or ref ..."), so it must not
        # reappear in any shipped schema.
        def walk(node, where):
            if isinstance(node, dict):
                self.assertNotIn("$schema", node, f"$schema present at {where}")
                for key, value in node.items():
                    walk(value, f"{where}.{key}")
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, f"{where}[{index}]")

        for path in self._all():
            walk(_load(path), path.name)

    def test_root_is_an_object_never_a_union(self):
        # Both runtimes reject a schema whose ROOT is anyOf (400 before any model call).
        for path in self._all():
            document = _load(path)
            with self.subTest(schema=path.name):
                self.assertEqual(document.get("type"), "object")
                self.assertNotIn("anyOf", document)

    def test_objects_are_closed_and_fully_required(self):
        # The strictest structured-output mode requires every declared property and
        # forbids extras; a schema that omits either is accepted here but not there.
        def walk(node, where):
            if isinstance(node, dict):
                if node.get("type") == "object" and "properties" in node:
                    self.assertIs(
                        node.get("additionalProperties"), False,
                        f"{where}: object without additionalProperties:false",
                    )
                    self.assertEqual(
                        set(node.get("required", [])), set(node["properties"]),
                        f"{where}: required must list every property",
                    )
                for key, value in node.items():
                    walk(value, f"{where}.{key}")
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, f"{where}[{index}]")

        for path in self._all():
            walk(_load(path), path.name)

    def test_no_unique_items_anywhere(self):
        # One runtime rejects a structured-output schema carrying `uniqueItems` before any
        # model call, so a repeated id or tier is the engine's to refuse, never the schema's.
        def walk(node, where):
            if isinstance(node, dict):
                self.assertNotIn("uniqueItems", node, f"uniqueItems present at {where}")
                for key, value in node.items():
                    walk(value, f"{where}.{key}")
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, f"{where}[{index}]")

        for path in self._all():
            walk(_load(path), path.name)


class JudgeSchemaContract(unittest.TestCase):
    """Locks what plan_duel.extract_judge_fields reads."""

    def test_declares_the_fields_the_engine_reads(self):
        document = _load(JUDGE_SCHEMA)
        self.assertEqual(
            set(document["required"]),
            {"score", "differences", "missed_rejections", "preferred", "justification"},
        )
        self.assertEqual(document["properties"]["preferred"]["enum"], ["A", "B"])
        self.assertEqual(
            set(document["properties"]["differences"]["items"]["required"]),
            {"topic", "plan_a", "plan_b", "stronger", "reason"},
        )

    def test_score_range_is_constrained_not_merely_described(self):
        # convergence_exit fires on score >= 8, so an unconstrained score could end a
        # duel on a value the rubric cannot produce.
        score = _load(JUDGE_SCHEMA)["properties"]["score"]
        self.assertEqual((score["minimum"], score["maximum"]), (0, 10))

    def test_a_real_verdict_validates_and_a_malformed_one_does_not(self):
        document = _load(JUDGE_SCHEMA)
        verdict = {
            "score": 7,
            "differences": [{"topic": "Auth", "plan_a": "JWT", "plan_b": "sessions",
                             "stronger": "Equal", "reason": "both valid"}],
            "missed_rejections": [],
            "preferred": "B",
            "justification": "Because.",
        }
        self.assertEqual(_validate(verdict, document), [])
        self.assertTrue(_validate({**verdict, "score": 11}, document))
        self.assertTrue(_validate({**verdict, "preferred": "Equal"}, document))


class ReviewSchemaContract(unittest.TestCase):
    """Locks what review_runner._is_verdict and a phase gate depend on."""

    def test_declares_the_gate_fields(self):
        document = _load(REVIEW_SCHEMA)
        self.assertEqual(
            set(document["required"]), {"findings", "overall", "blocking_count"}
        )
        finding = document["properties"]["findings"]["items"]
        self.assertEqual(
            set(finding["required"]),
            {"file", "line", "severity", "summary", "failure_scenario"},
        )
        self.assertEqual(
            finding["properties"]["severity"]["enum"],
            ["blocker", "major", "minor", "nit"],
        )

    def test_severity_enum_matches_the_runners_blocking_set(self):
        # review_runner recounts blocking_count from these strings; a drift between the
        # schema's enum and the runner's known set is what makes the recount fail open.
        import sys

        engine_dir = _SKILLS / "diff-review"
        if str(engine_dir) not in sys.path:
            sys.path.insert(0, str(engine_dir))
        import review_runner

        enum = _load(REVIEW_SCHEMA)["properties"]["findings"]["items"]["properties"][
            "severity"
        ]["enum"]
        self.assertEqual(set(enum), set(review_runner.KNOWN_SEVERITIES))
        self.assertTrue(set(review_runner.BLOCKING_SEVERITIES) <= set(enum))

    def test_a_clean_verdict_validates(self):
        self.assertEqual(
            _validate(
                {"findings": [], "overall": "clean", "blocking_count": 0},
                _load(REVIEW_SCHEMA),
            ),
            [],
        )


class ReaderSchemaContract(unittest.TestCase):
    """Locks what review-panel's merge stage reads from a blind reader, and what a reader
    structurally cannot say."""

    FINDING = {
        "file": "install.py",
        "line_start": 120,
        "line_end": 131,
        "severity": "major",
        "failure": "A junction answers False to is_symlink() and True to is_dir(), so the "
                   "recursive-delete branch runs on a link.",
        "direction": "Test with the link helper before choosing a branch.",
        "consequence": "Uninstalling deletes the directory the link pointed at, so a user "
                       "loses files the installer never put there.",
        "fix_size": "small",
        "quote": "    if target.is_dir() and not target.is_symlink():\n"
                 "        shutil.rmtree(target)",
        "reproduction": None,
    }

    def test_declares_location_severity_failure_direction_and_reproduction(self):
        document = _load(READER_SCHEMA)
        self.assertEqual(set(document["required"]),
                         {"finding_count", "findings", "summary"})
        finding = document["properties"]["findings"]["items"]
        self.assertEqual(
            set(finding["required"]),
            {"file", "line_start", "line_end", "severity", "consequence", "failure",
             "direction", "fix_size", "quote", "reproduction"},
        )
        self.assertEqual(finding["properties"]["fix_size"]["enum"],
                         ["1 line", "small", "medium", "large"])
        self.assertEqual(finding["properties"]["line_start"]["minimum"], 1)
        self.assertEqual(finding["properties"]["line_end"]["minimum"], 1)

    def test_severity_reuses_diff_reviews_four_levels_verbatim(self):
        # Severity has one owner in the pack; a second vocabulary forks what a gate keys on.
        reader = _load(READER_SCHEMA)["properties"]["findings"]["items"]["properties"]
        review = _load(REVIEW_SCHEMA)["properties"]["findings"]["items"]["properties"]
        self.assertEqual(reader["severity"]["enum"], review["severity"]["enum"])
        self.assertEqual(reader["severity"]["enum"], ["blocker", "major", "minor", "nit"])

    def test_a_reader_cannot_express_established_by_or_evidence(self):
        # Those are produced by verification; a blind reader able to emit them pre-claims
        # the status the verify stage exists to assign. Structurally impossible, not
        # stripped: no property anywhere in the schema carries either name, and a finding
        # that adds one is rejected by the closed object.
        def names(node):
            if isinstance(node, dict):
                for key, value in node.get("properties", {}).items():
                    yield key
                    yield from names(value)
                for value in node.values():
                    if isinstance(value, (dict, list)) and value is not node.get("properties"):
                        yield from names(value)
            elif isinstance(node, list):
                for item in node:
                    yield from names(item)

        document = _load(READER_SCHEMA)
        self.assertFalse({"established_by", "evidence"} & set(names(document)))
        for forbidden in ("established_by", "evidence"):
            with self.subTest(field=forbidden):
                spiked = {"findings": [{**self.FINDING, forbidden: "reproduced"}], "summary": "s"}
                self.assertTrue(_validate(spiked, document))

    def test_the_source_a_reader_read_is_carried_as_quote_never_as_evidence(self):
        # A reader quotes what it actually read, so a wrong line range
        # is detectable. That is a quotation, not evidence: `evidence` stays forbidden above,
        # and the quotation gets its own name so both facts hold at once.
        finding = _load(READER_SCHEMA)["properties"]["findings"]["items"]
        self.assertIn("quote", finding["properties"])
        self.assertEqual(finding["properties"]["quote"]["type"], "string")
        self.assertNotIn("evidence", finding["properties"])

    def test_a_real_result_validates_with_and_without_a_reproduction(self):
        document = _load(READER_SCHEMA)
        self.assertEqual(
            _validate({"finding_count": 0, "findings": [], "summary": "nothing found"},
                      document), [])
        self.assertEqual(
            _validate({"finding_count": 1, "findings": [self.FINDING], "summary": "one"},
                      document), [])
        # The count is required, so a result that omits it is refused by the schema even
        # though the hand-parser tolerates the absence.
        self.assertEqual(_validate({"findings": [], "summary": "nothing found"}, document),
                         ["$: missing required 'finding_count'"])
        runnable = {**self.FINDING, "reproduction": {
            "argv": ["python3", "-m", "unittest", "tests.test_install"],
            "cwd": ".",
            "expect": "the junction test fails",
        }}
        self.assertEqual(
            _validate({"finding_count": 1, "findings": [runnable], "summary": "one"}, document), [])

    def test_an_unknown_severity_and_a_missing_field_are_rejected(self):
        document = _load(READER_SCHEMA)
        bogus = {"findings": [{**self.FINDING, "severity": "critical"}], "summary": "s"}
        self.assertTrue(_validate(bogus, document))
        for field in self.FINDING:
            with self.subTest(missing=field):
                thin = {k: v for k, v in self.FINDING.items() if k != field}
                self.assertTrue(_validate({"findings": [thin], "summary": "s"}, document))

    def test_a_reproduction_is_closed_and_fully_required(self):
        # The nullable object is still a closed object: the mechanics rule reads
        # ``type == "object"`` and would skip a ``["object", "null"]`` union silently.
        repro = _load(READER_SCHEMA)["properties"]["findings"]["items"]["properties"]["reproduction"]
        self.assertEqual(repro["type"], ["object", "null"])
        self.assertIs(repro["additionalProperties"], False)
        self.assertEqual(set(repro["required"]), set(repro["properties"]))
        self.assertEqual(set(repro["properties"]), {"argv", "cwd", "expect"})


class VerifierSchemaContract(unittest.TestCase):
    """Locks what review-panel's report stage reads from a verifier: one verdict per
    candidate in the four statuses, evidence where something ran, and a severity revision
    that cannot exist without its rationale."""

    EVIDENCE = {"argv": ["python3", "-m", "unittest", "tests.test_install"], "cwd": ".",
                "exit_status": 1, "output": "FAIL: test_junction\n", "truncated": False,
                "run_kind": "executed",
                "shows": "Shows the junction test failing on this tree."}
    VERDICT = {
        "candidate": "cand-001",
        "status": "confirmed_by_reading",
        "evidence": None,
        "rationale": "The branch on line 120 runs the recursive delete on a junction.",
        "revision": None,
        "test_first": "tests/test_install.py: point a junction at a directory outside the "
                      "install root and assert uninstall leaves it standing.",
        "unresolved_reason": None,
        "needs_files": None,
    }

    def test_declares_one_verdict_per_candidate_with_the_four_statuses(self):
        document = _load(VERIFIER_SCHEMA)
        self.assertEqual(set(document["required"]), {"verdicts", "summary"})
        verdict = document["properties"]["verdicts"]["items"]
        self.assertEqual(set(verdict["required"]),
                         {"candidate", "status", "evidence", "rationale", "revision",
                          "test_first", "unresolved_reason", "needs_files"})
        self.assertEqual(verdict["properties"]["status"]["enum"],
                         ["reproduced", "confirmed_by_reading", "refuted", "unresolved"])

    def test_evidence_is_a_closed_nullable_object_naming_argv_cwd_exit_status_and_bounded_output(self):
        evidence = _load(VERIFIER_SCHEMA)["properties"]["verdicts"]["items"]["properties"]["evidence"]
        self.assertEqual(evidence["type"], ["object", "null"])
        self.assertIs(evidence["additionalProperties"], False)
        self.assertEqual(set(evidence["required"]), set(evidence["properties"]))
        self.assertEqual(set(evidence["properties"]),
                         {"argv", "cwd", "exit_status", "output", "truncated", "run_kind",
                          "shows"})
        # A command and its output never say why they were run, so the verifier says it
        # here. The pattern matters as much as the type: `_parse_evidence` refuses
        # whitespace, and a schema that accepted it would send a compliant worker to
        # produce a verdict the engine then rejects.
        self.assertEqual(evidence["properties"]["shows"]["type"], "string")
        self.assertEqual(evidence["properties"]["shows"]["pattern"], "\\S")
        # A text search and a run of the code are both evidence and neither is the other.
        # The report counts and labels them apart, so the two words are locked here.
        self.assertEqual(evidence["properties"]["run_kind"]["enum"], ["executed", "documentary"])
        self.assertEqual(evidence["properties"]["argv"]["items"]["type"], "string")
        self.assertEqual(evidence["properties"]["exit_status"]["type"], "integer")
        self.assertEqual(evidence["properties"]["truncated"]["type"], "boolean")

    def test_a_revision_is_closed_nullable_and_carries_the_four_levels_verbatim(self):
        # Severity has one owner: the revision's enum is diff-review's, as the reader's is.
        revision = _load(VERIFIER_SCHEMA)["properties"]["verdicts"]["items"]["properties"]["revision"]
        review = _load(REVIEW_SCHEMA)["properties"]["findings"]["items"]["properties"]
        self.assertEqual(revision["type"], ["object", "null"])
        self.assertIs(revision["additionalProperties"], False)
        self.assertEqual(set(revision["properties"]), {"severity", "rationale"})
        self.assertEqual(set(revision["required"]), {"severity", "rationale"})
        self.assertEqual(revision["properties"]["severity"]["enum"], review["severity"]["enum"])

    def test_a_real_result_validates_in_every_status(self):
        document = _load(VERIFIER_SCHEMA)
        self.assertEqual(_validate({"verdicts": [], "summary": "nothing to verify"}, document), [])
        reproduced = {**self.VERDICT, "status": "reproduced", "evidence": self.EVIDENCE}
        refuted = {**self.VERDICT, "candidate": "cand-002", "status": "refuted", "evidence": self.EVIDENCE,
                   "revision": {"severity": "nit", "rationale": "The branch is unreachable."},
                   "test_first": None}
        unresolved = {**self.VERDICT, "candidate": "cand-003", "status": "unresolved",
                      "test_first": None, "unresolved_reason": "needs_a_run"}
        self.assertEqual(_validate({"verdicts": [self.VERDICT, reproduced, refuted, unresolved],
                                    "summary": "four"}, document), [])

    def test_an_unknown_status_a_missing_field_and_an_open_revision_are_rejected(self):
        document = _load(VERIFIER_SCHEMA)
        self.assertTrue(_validate({"verdicts": [{**self.VERDICT, "status": "confirmed"}], "summary": "s"}, document))
        for field in self.VERDICT:
            with self.subTest(missing=field):
                thin = {k: v for k, v in self.VERDICT.items() if k != field}
                self.assertTrue(_validate({"verdicts": [thin], "summary": "s"}, document))
        no_rationale = {**self.VERDICT, "revision": {"severity": "minor"}}
        self.assertTrue(_validate({"verdicts": [no_rationale], "summary": "s"}, document))
        for field in self.EVIDENCE:
            with self.subTest(missing=f"evidence.{field}"):
                thin = {k: v for k, v in self.EVIDENCE.items() if k != field}
                spiked = {**self.VERDICT, "status": "reproduced", "evidence": thin}
                self.assertTrue(_validate({"verdicts": [spiked], "summary": "s"}, document))

    def test_test_first_is_nullable_so_a_refuted_finding_can_carry_none(self):
        # Required on every verdict, nullable in value: a refuted finding has nothing to
        # fix and so no test to write, and the engine refuses one that carries a test anyway.
        test_first = _load(VERIFIER_SCHEMA)["properties"]["verdicts"]["items"]["properties"]["test_first"]
        self.assertEqual(test_first["type"], ["string", "null"])

    def test_a_file_outside_the_scope_is_named_as_a_path_and_not_only_in_prose(self):
        # The commonest unresolved reason there is, and the brief has always asked for the
        # file in the rationale — where nothing can add it up. The list is what lets the
        # report count how many open claims one file would settle.
        document = _load(VERIFIER_SCHEMA)
        needs = document["properties"]["verdicts"]["items"]["properties"]["needs_files"]
        self.assertEqual(needs["type"], ["array", "null"])
        self.assertEqual(needs["items"]["type"], "string")
        verdict = {**self.VERDICT, "status": "unresolved", "test_first": None,
                   "unresolved_reason": "needs_a_file_outside_the_scope",
                   "needs_files": ["src/TransactionService.java"]}
        self.assertEqual(_validate({"verdicts": [verdict], "summary": "s"}, document), [])
        # And the reason's own words send the verifier to both places.
        reason = document["properties"]["verdicts"]["items"]["properties"]["unresolved_reason"]
        self.assertIn("needs_files", reason["description"])

    def test_unresolved_reason_is_the_four_values_the_report_groups_by(self):
        # The report groups the unresolved set by what would settle each, and reports an
        # environment failure as its own thing rather than as an open question
        # about the code. Both read this enum, so its values are locked here.
        reason = _load(VERIFIER_SCHEMA)["properties"]["verdicts"]["items"]["properties"]["unresolved_reason"]
        self.assertEqual(reason["type"], ["string", "null"])
        self.assertEqual(reason["enum"],
                         ["needs_a_run", "needs_a_file_outside_the_scope",
                          "needs_a_product_decision", "blocked_by_the_environment", None])
        document = _load(VERIFIER_SCHEMA)
        for value in reason["enum"][:-1]:
            with self.subTest(reason=value):
                verdict = {**self.VERDICT, "status": "unresolved", "test_first": None,
                           "unresolved_reason": value}
                self.assertEqual(_validate({"verdicts": [verdict], "summary": "s"}, document), [])
        bogus = {**self.VERDICT, "status": "unresolved", "unresolved_reason": "needs_more_thought"}
        self.assertTrue(_validate({"verdicts": [bogus], "summary": "s"}, document))

    def test_the_verifier_cannot_name_a_finder(self):
        # There is no rebuttal round and no arbitration: the verifier answers per
        # candidate id, and nothing in its result can address a reader, a lane or a lens.
        def names(node):
            if isinstance(node, dict):
                for key, value in node.get("properties", {}).items():
                    yield key
                    yield from names(value)
                for value in node.values():
                    if isinstance(value, (dict, list)) and value is not node.get("properties"):
                        yield from names(value)
            elif isinstance(node, list):
                for item in node:
                    yield from names(item)

        self.assertFalse({"unit", "lane", "lens", "finder", "raised_by"} & set(names(_load(VERIFIER_SCHEMA))))


class ProbeSchemaContract(unittest.TestCase):
    """Locks what the capability probe returns. The report header reads these two answers to
    say what the run could execute, so `unknown` has to be expressible: a probe forced to
    answer yes or no would make a tree nobody could build look like one whose tests passed."""

    ATTEMPT = {"answer": "yes", "argv": ["python3", "-m", "compileall", "-q", "."], "cwd": ".",
               "exit_status": 0, "output": "", "truncated": False}
    NOTHING_RAN = {"answer": "unknown", "argv": [], "cwd": ".", "exit_status": None,
                   "output": "", "truncated": False}

    def test_declares_a_build_answer_a_tests_answer_and_a_summary(self):
        document = _load(PROBE_SCHEMA)
        self.assertEqual(set(document["required"]), {"build", "tests", "summary"})
        for name in ("build", "tests"):
            with self.subTest(attempt=name):
                attempt = document["properties"][name]
                self.assertEqual(set(attempt["required"]),
                                 {"answer", "argv", "cwd", "exit_status", "output", "truncated"})

    def test_every_answer_can_be_unknown(self):
        # The whole point of the probe: a tree it could not build and a tree it never tried
        # to build are different facts, and neither is `no`.
        document = _load(PROBE_SCHEMA)
        for name in ("build", "tests"):
            with self.subTest(attempt=name):
                self.assertEqual(document["properties"][name]["properties"]["answer"]["enum"],
                                 ["yes", "no", "unknown"])

    def test_an_attempt_that_ran_nothing_has_no_exit_status(self):
        document = _load(PROBE_SCHEMA)
        for name in ("build", "tests"):
            with self.subTest(attempt=name):
                status = document["properties"][name]["properties"]["exit_status"]
                self.assertEqual(status["type"], ["integer", "null"])

    def test_a_real_probe_result_validates_in_every_shape(self):
        document = _load(PROBE_SCHEMA)
        self.assertEqual(_validate({"build": self.ATTEMPT, "tests": self.ATTEMPT,
                                    "summary": "builds and tests."}, document), [])
        self.assertEqual(_validate({"build": self.NOTHING_RAN, "tests": self.NOTHING_RAN,
                                    "summary": "nothing here says how to build it."}, document), [])
        failed = {**self.ATTEMPT, "answer": "no", "exit_status": 1, "output": "error: no such module\n"}
        self.assertEqual(_validate({"build": failed, "tests": self.NOTHING_RAN,
                                    "summary": "the build fails."}, document), [])

    def test_an_unknown_answer_word_and_a_missing_field_are_rejected(self):
        document = _load(PROBE_SCHEMA)
        bogus = {"build": {**self.ATTEMPT, "answer": "maybe"}, "tests": self.ATTEMPT, "summary": "s"}
        self.assertTrue(_validate(bogus, document))
        for field in self.ATTEMPT:
            with self.subTest(missing=field):
                thin = {k: v for k, v in self.ATTEMPT.items() if k != field}
                self.assertTrue(_validate({"build": thin, "tests": self.ATTEMPT, "summary": "s"},
                                          document))
        for field in ("build", "tests", "summary"):
            with self.subTest(missing=field):
                whole = {"build": self.ATTEMPT, "tests": self.ATTEMPT, "summary": "s"}
                self.assertTrue(_validate({k: v for k, v in whole.items() if k != field}, document))

    def test_the_probe_cannot_report_a_finding(self):
        # It establishes capability and nothing else. A probe able to return findings would
        # be a reader nobody routed, raising claims no stranger ever checks.
        def names(node):
            if isinstance(node, dict):
                for key, value in node.get("properties", {}).items():
                    yield key
                    yield from names(value)
                for value in node.values():
                    if isinstance(value, (dict, list)) and value is not node.get("properties"):
                        yield from names(value)
            elif isinstance(node, list):
                for item in node:
                    yield from names(item)

        self.assertFalse({"findings", "severity", "file", "line_start", "failure", "direction"}
                         & set(names(_load(PROBE_SCHEMA))))


class ClustererSchemaContract(unittest.TestCase):
    """Locks what one clustering unit returns. The stage groups candidates and does nothing
    else, so the schema has to make the two things it must not do inexpressible: there is no
    way to say a candidate is wrong, and no way to say it should be dropped."""

    ONE = {"members": ["cand-001", "cand-002"], "consequence": "The run stops half-way.",
           "split_reason": None}
    OTHER = {"members": ["cand-003"], "consequence": "The file is left truncated.",
             "split_reason": "A different root cause at the same lines: the write, not the guard."}

    def test_declares_clusters_and_a_summary(self):
        document = _load(CLUSTERER_SCHEMA)
        self.assertEqual(set(document["required"]), {"clusters", "summary"})
        cluster = document["properties"]["clusters"]["items"]
        self.assertEqual(set(cluster["required"]), {"members", "consequence", "split_reason"})

    def test_a_split_reason_is_optional_in_value_and_required_in_shape(self):
        # Nullable rather than absent: the strictest structured-output mode requires every
        # declared property, so a cluster with nothing at its location says so with null.
        document = _load(CLUSTERER_SCHEMA)
        reason = document["properties"]["clusters"]["items"]["properties"]["split_reason"]
        self.assertEqual(reason["type"], ["string", "null"])

    def test_a_real_clustering_result_validates(self):
        document = _load(CLUSTERER_SCHEMA)
        self.assertEqual(_validate({"clusters": [self.ONE, self.OTHER],
                                    "summary": "Two defects at one site."}, document), [])
        self.assertEqual(_validate({"clusters": [], "summary": "nothing to group."}, document), [])

    def test_a_missing_field_and_an_extra_one_are_rejected(self):
        document = _load(CLUSTERER_SCHEMA)
        for field in self.ONE:
            with self.subTest(missing=field):
                thin = {k: v for k, v in self.ONE.items() if k != field}
                self.assertTrue(_validate({"clusters": [thin], "summary": "s"}, document))
        self.assertTrue(_validate({"clusters": [{**self.ONE, "verdict": "refuted"}],
                                   "summary": "s"}, document))
        self.assertTrue(_validate({"clusters": [self.ONE]}, document))

    def test_the_clusterer_cannot_judge_or_drop_a_candidate(self):
        # Grouping is the whole of this stage. A field for a verdict, a severity or a
        # dropped id would let one unit delete a defect that two others established.
        def names(node):
            if isinstance(node, dict):
                for key, value in node.get("properties", {}).items():
                    yield key
                    yield from names(value)
                for value in node.values():
                    if isinstance(value, (dict, list)) and value is not node.get("properties"):
                        yield from names(value)
            elif isinstance(node, list):
                for item in node:
                    yield from names(item)

        self.assertFalse({"status", "verdict", "severity", "dropped", "discarded", "spurious",
                          "findings", "evidence"} & set(names(_load(CLUSTERER_SCHEMA))))


class MergerSchemaContract(unittest.TestCase):
    """Locks what the merge unit returns. It groups sites and does nothing else, and no
    object in it is keyed by a site id: a closed schema cannot describe keys it does not
    know, so every per-site answer is a record naming its site, and the engine builds the
    map and refuses an unknown or repeated id."""

    PAIR = {"sites": ["S1", "S2"],
            "mechanism": "An empty input is indexed before it is checked; check it first.",
            "instances": [{"site": "S1", "instance": "core indexes an empty list."},
                          {"site": "S2", "instance": "run.py indexes an empty argv."}],
            "reason_kept_apart": None}
    SINGLE = {"sites": ["S3"], "mechanism": None, "instances": None,
              "reason_kept_apart": "A different cause at a different place."}
    COMPOUND = {"site": "S3", "second_claim": "It also leaves the file open."}

    def test_declares_groups_compound_and_a_summary(self):
        document = _load(MERGER_SCHEMA)
        self.assertEqual(set(document["required"]), {"groups", "compound", "summary"})
        group = document["properties"]["groups"]["items"]
        self.assertEqual(set(group["required"]),
                         {"sites", "mechanism", "instances", "reason_kept_apart"})

    def test_every_per_site_answer_is_a_record_and_never_a_map(self):
        document = _load(MERGER_SCHEMA)
        group = document["properties"]["groups"]["items"]["properties"]
        self.assertEqual(group["instances"]["type"], ["array", "null"])
        self.assertEqual(set(group["instances"]["items"]["required"]), {"site", "instance"})
        compound = document["properties"]["compound"]
        self.assertEqual(compound["type"], "array")
        self.assertEqual(set(compound["items"]["required"]), {"site", "second_claim"})
        self.assertTrue(_validate({"groups": [], "compound": {"S3": "a second claim"},
                                   "summary": "s"}, document))
        self.assertTrue(_validate({"groups": [{**self.PAIR, "instances": {"S1": "a", "S2": "b"}}],
                                   "compound": [], "summary": "s"}, document))

    def test_a_real_merge_result_validates(self):
        document = _load(MERGER_SCHEMA)
        self.assertEqual(_validate({"groups": [self.PAIR, self.SINGLE],
                                    "compound": [self.COMPOUND],
                                    "summary": "One group of two."}, document), [])

    def test_a_missing_field_and_an_extra_one_are_rejected(self):
        document = _load(MERGER_SCHEMA)
        for field in self.PAIR:
            with self.subTest(missing=field):
                thin = {k: v for k, v in self.PAIR.items() if k != field}
                self.assertTrue(_validate({"groups": [thin], "compound": [],
                                           "summary": "s"}, document))
        self.assertTrue(_validate({"groups": [{**self.PAIR, "verdict": "refuted"}],
                                   "compound": [], "summary": "s"}, document))
        self.assertTrue(_validate({"groups": [self.PAIR], "summary": "s"}, document))
        self.assertTrue(_validate({"groups": [self.PAIR], "compound": [
            {**self.COMPOUND, "severity": "major"}], "summary": "s"}, document))

    def test_the_merger_cannot_judge_or_drop_a_site(self):
        def names(node):
            if isinstance(node, dict):
                for key, value in node.get("properties", {}).items():
                    yield key
                    yield from names(value)
                for value in node.values():
                    if isinstance(value, (dict, list)) and value is not node.get("properties"):
                        yield from names(value)
            elif isinstance(node, list):
                for item in node:
                    yield from names(item)

        self.assertFalse({"status", "verdict", "severity", "dropped", "discarded", "spurious",
                          "findings", "evidence"} & set(names(_load(MERGER_SCHEMA))))

    def test_a_repeated_site_is_the_engines_to_refuse(self):
        # The schema cannot say "each site once" without `uniqueItems`, which one runtime
        # rejects; `parse_merger_result` refuses the repeat instead.
        repeated = {**self.PAIR, "sites": ["S1", "S1"]}
        self.assertEqual(_validate({"groups": [repeated], "compound": [], "summary": "s"},
                                   _load(MERGER_SCHEMA)), [])


class MergeCheckerSchemaContract(unittest.TestCase):
    """Locks what the merge check unit returns. It judges whether each site of a proposed
    group is that one mistake and does nothing else, and no object in it is keyed by a site
    id: every per-site answer is a record naming its site, and the engine builds the map
    and refuses an unknown or repeated id."""

    GROUP = {"group": "G1",
             "sites": [{"site": "S1", "verdict": "fits", "reason": "It is that mistake."},
                       {"site": "S2", "verdict": "does_not_fit", "reason": "Another cause."}],
             "hidden_claims": [{"site": "S1", "claim": "It also leaves the file open."}],
             "fix_touches_refuted": {"value": False, "sites": []}}

    def test_declares_groups_and_a_summary(self):
        document = _load(MERGE_CHECKER_SCHEMA)
        self.assertEqual(set(document["required"]), {"groups", "summary"})
        group = document["properties"]["groups"]["items"]
        self.assertEqual(set(group["required"]),
                         {"group", "sites", "hidden_claims", "fix_touches_refuted"})

    def test_every_per_site_answer_is_a_record_and_never_a_map(self):
        document = _load(MERGE_CHECKER_SCHEMA)
        group = document["properties"]["groups"]["items"]["properties"]
        self.assertEqual(group["sites"]["type"], "array")
        self.assertEqual(set(group["sites"]["items"]["required"]),
                         {"site", "verdict", "reason"})
        self.assertEqual(group["hidden_claims"]["type"], "array")
        self.assertEqual(set(group["hidden_claims"]["items"]["required"]), {"site", "claim"})
        self.assertTrue(_validate({"groups": [{**self.GROUP, "sites": {
            "S1": {"verdict": "fits", "reason": "x"}}}], "summary": "s"}, document))
        self.assertTrue(_validate({"groups": [{**self.GROUP, "hidden_claims": {"S1": "x"}}],
                                   "summary": "s"}, document))

    def test_a_real_merge_check_result_validates(self):
        self.assertEqual(_validate({"groups": [self.GROUP], "summary": "One upheld."},
                                   _load(MERGE_CHECKER_SCHEMA)), [])

    def test_a_verdict_is_one_of_two(self):
        bad = {**self.GROUP, "sites": [{"site": "S1", "verdict": "maybe", "reason": "x"}]}
        self.assertTrue(_validate({"groups": [bad], "summary": "s"},
                                  _load(MERGE_CHECKER_SCHEMA)))

    def test_a_missing_field_and_an_extra_one_are_rejected(self):
        document = _load(MERGE_CHECKER_SCHEMA)
        for field in self.GROUP:
            with self.subTest(missing=field):
                thin = {k: v for k, v in self.GROUP.items() if k != field}
                self.assertTrue(_validate({"groups": [thin], "summary": "s"}, document))
        self.assertTrue(_validate({"groups": [{**self.GROUP, "accepted": True}],
                                   "summary": "s"}, document))
        self.assertTrue(_validate({"groups": [self.GROUP]}, document))
        self.assertTrue(_validate({"groups": [{**self.GROUP, "fix_touches_refuted": {
            "value": False, "sites": [], "why": "x"}}], "summary": "s"}, document))

    def test_the_checker_cannot_judge_a_site_or_propose_a_group(self):
        def names(node):
            if isinstance(node, dict):
                for key, value in node.get("properties", {}).items():
                    yield key
                    yield from names(value)
                for value in node.values():
                    if isinstance(value, (dict, list)) and value is not node.get("properties"):
                        yield from names(value)
            elif isinstance(node, list):
                for item in node:
                    yield from names(item)

        self.assertFalse({"status", "severity", "dropped", "mechanism", "instances",
                          "findings", "evidence"} & set(names(_load(MERGE_CHECKER_SCHEMA))))

    def test_a_repeated_site_is_the_engines_to_refuse(self):
        # The schema cannot say "each site once" without `uniqueItems`, which one runtime
        # rejects; `parse_merge_checker_result` refuses the repeat instead.
        repeated = {**self.GROUP, "sites": [self.GROUP["sites"][0]] * 2}
        self.assertEqual(_validate({"groups": [repeated], "summary": "s"},
                                   _load(MERGE_CHECKER_SCHEMA)), [])


class SynthesizerResultContract(unittest.TestCase):
    """The fifth round's contract, one batch at a time. It carries the batch's own tiers and
    one entry per defect, and it gives the unit no way to say a defect is wrong, no way to
    re-rank it, and no way to leave it out."""

    TIERS = ["Money can be taken twice", "A run stops instead of finishing"]
    ONE = {"defect": "D1", "heading": "A retried payment is charged twice.",
           "tier": "Money can be taken twice",
           "what_goes_wrong": "A retry re-posts the charge because nothing keys the request.",
           "fix": "Key the request and reject a repeat of the same key.",
           "site_notes": [{"site": "S2", "note": "The refund path retries the same way."}],
           "cross_references": ["D4"]}
    OTHER = {"defect": "D2", "heading": "An empty input stops the run.",
             "tier": "A run stops instead of finishing",
             "what_goes_wrong": "An empty list is indexed and the call dies.",
             "fix": "Return early on an empty input.",
             "site_notes": [],
             "cross_references": []}

    def test_declares_the_tiers_and_the_defects_and_no_summary(self):
        # A batch sees only its own defects, so a paragraph about the whole run is not one
        # it can write.
        document = _load(SYNTHESIZER_SCHEMA)
        self.assertEqual(set(document["required"]), {"tiers", "defects"})
        self.assertEqual(set(document["properties"]), {"tiers", "defects"})
        entry = document["properties"]["defects"]["items"]
        self.assertEqual(set(entry["required"]),
                         {"defect", "heading", "tier", "what_goes_wrong", "fix", "site_notes",
                          "cross_references"})

    def test_a_site_note_is_a_record_and_never_a_map(self):
        # A closed schema cannot describe keys it does not know, so a note keyed by its
        # site id is refused here; the engine builds the map and refuses an unknown or
        # repeated site.
        document = _load(SYNTHESIZER_SCHEMA)
        notes = document["properties"]["defects"]["items"]["properties"]["site_notes"]
        self.assertEqual(notes["type"], "array")
        self.assertEqual(set(notes["items"]["required"]), {"site", "note"})
        self.assertTrue(_validate({"tiers": self.TIERS, "defects": [
            {**self.ONE, "site_notes": {"S2": "The refund path retries."}}]}, document))
        self.assertTrue(_validate({"tiers": self.TIERS, "defects": [
            {**self.ONE, "site_notes": [{"site": "S2", "note": "x", "why": "y"}]}]}, document))

    def test_every_text_a_defect_carries_has_a_length_limit(self):
        # The limits are the engine's to enforce, word by word; the schema states them in
        # characters as well, so a runtime that enforces the schema refuses early.
        document = _load(SYNTHESIZER_SCHEMA)
        entry = document["properties"]["defects"]["items"]["properties"]
        self.assertEqual(entry["heading"]["maxLength"], 120)
        for field in ("what_goes_wrong", "fix"):
            with self.subTest(field=field):
                self.assertIn("maxLength", entry[field])
        note = entry["site_notes"]["items"]["properties"]["note"]
        self.assertIn("maxLength", note)
        self.assertTrue(_validate({"tiers": self.TIERS,
                                   "defects": [{**self.ONE, "heading": "x" * 121}]}, document))
        self.assertEqual(_validate({"tiers": self.TIERS,
                                    "defects": [{**self.ONE, "heading": "x" * 120}]},
                                   document), [])
        self.assertTrue(_validate({"tiers": self.TIERS, "defects": [
            {**self.ONE, "what_goes_wrong": "x" * (entry["what_goes_wrong"]["maxLength"] + 1)}]},
            document))
        self.assertTrue(_validate({"tiers": self.TIERS, "defects": [
            {**self.ONE, "site_notes": [{"site": "S2", "note": "x" * (note["maxLength"] + 1)}]}]},
            document))

    def test_the_tier_list_belongs_to_the_batch_and_not_to_a_defect(self):
        # One vocabulary per reply: a tier list nested under each defect would be a schema
        # that invites every entry to name its own.
        document = _load(SYNTHESIZER_SCHEMA)
        self.assertEqual(document["properties"]["tiers"]["type"], "array")
        self.assertEqual(document["properties"]["tiers"]["items"]["type"], "string")
        entry = document["properties"]["defects"]["items"]["properties"]
        self.assertEqual(entry["tier"]["type"], "string")
        self.assertNotIn("tiers", entry)

    def test_no_entry_field_may_be_null(self):
        # The one "nothing to say" answer this round is allowed is an EMPTY cross-reference
        # list. A nullable narrative or fix would make a shrug a valid reply, and a defect
        # with no account of it is what the round exists to end; a nullable tier would take
        # the defect out of the grouping the unit was asked to produce.
        entry = _load(SYNTHESIZER_SCHEMA)["properties"]["defects"]["items"]["properties"]
        for field in ("defect", "heading", "tier", "what_goes_wrong", "fix"):
            with self.subTest(field=field):
                self.assertEqual(entry[field]["type"], "string")
        self.assertEqual(entry["cross_references"]["type"], "array")
        self.assertEqual(entry["site_notes"]["type"], "array")

    def test_a_real_synthesis_result_validates(self):
        document = _load(SYNTHESIZER_SCHEMA)
        self.assertEqual(_validate({"tiers": self.TIERS, "defects": [self.ONE, self.OTHER]},
                                   document), [])

    def test_a_missing_field_and_an_extra_one_are_rejected(self):
        document = _load(SYNTHESIZER_SCHEMA)
        for field in self.ONE:
            with self.subTest(missing=field):
                thin = {k: v for k, v in self.ONE.items() if k != field}
                self.assertTrue(_validate({"tiers": self.TIERS, "defects": [thin]}, document))
        self.assertTrue(_validate({"tiers": self.TIERS,
                                   "defects": [{**self.ONE, "severity": "blocker"}]}, document))
        # A batch writes no paragraph about the run: a reply carrying one is refused.
        self.assertTrue(_validate({"tiers": self.TIERS, "defects": [self.ONE],
                                   "summary": "s"}, document))
        self.assertTrue(_validate({"tiers": self.TIERS}, document))
        self.assertTrue(_validate({"tiers": "one", "defects": [self.ONE]}, document))

    def test_the_synthesizer_cannot_judge_rerank_or_drop_a_defect(self):
        # The stage writes an account of defects other stages settled. A field for a
        # verdict, a severity or a dropped id would let one unit undo work two rounds of
        # strangers did, and this schema is what a runtime enforces.
        def names(node):
            if isinstance(node, dict):
                for key, value in node.get("properties", {}).items():
                    yield key
                    yield from names(value)
                for value in node.values():
                    if isinstance(value, (dict, list)) and value is not node.get("properties"):
                        yield from names(value)
            elif isinstance(node, list):
                for item in node:
                    yield from names(item)

        self.assertFalse({"status", "verdict", "severity", "dropped", "discarded", "spurious",
                          "findings", "evidence", "members"} & set(names(_load(SYNTHESIZER_SCHEMA))))

    def test_the_element_type_of_every_array_is_declared(self):
        # A cross-reference is a defect id. Without `items` the schema accepts
        # `cross_references: [{}]`, and the engine then refuses the whole entry for a
        # reason the contract could have stated at the boundary. Removing either `items`
        # left the contract tests green until this one existed.
        document = _load(SYNTHESIZER_SCHEMA)
        self.assertEqual(document["properties"]["tiers"]["items"]["type"], "string")
        reference = document["properties"]["defects"]["items"]["properties"]["cross_references"]
        self.assertIn("items", reference, "an array with no declared element type")
        self.assertEqual(reference["items"]["type"], "string")
        for bad in ({}, 7, None, ["D4"]):
            with self.subTest(reference=bad):
                self.assertTrue(_validate({"tiers": self.TIERS,
                                           "defects": [{**self.ONE, "cross_references": [bad]}]},
                                           document))
        self.assertTrue(_validate({"tiers": [self.TIERS[0], 7], "defects": [self.ONE]}, document))

    def test_empty_strings_and_an_empty_tier_list_are_refused_at_the_boundary(self):
        # Constraints the parser enforces and the schema can state. An empty tier list
        # fails the whole unit; an empty narrative, fix, tier or id costs that defect its
        # assignment. Stated here they are refused before a reply is ever parsed.
        document = _load(SYNTHESIZER_SCHEMA)
        self.assertEqual(_validate({"tiers": self.TIERS, "defects": [self.ONE, self.OTHER]},
                                   document), [])
        self.assertTrue(_validate({"tiers": [], "defects": [self.ONE]},
                                  document))
        self.assertTrue(_validate({"tiers": [""], "defects": [self.ONE]},
                                  document))
        for field in ("defect", "heading", "tier", "what_goes_wrong", "fix"):
            with self.subTest(empty=field):
                self.assertTrue(_validate({"tiers": self.TIERS,
                                           "defects": [{**self.ONE, field: ""}]}, document))
        for field in ("site", "note"):
            with self.subTest(empty=field):
                self.assertTrue(_validate({"tiers": self.TIERS, "defects": [
                    {**self.ONE, "site_notes": [{**self.ONE["site_notes"][0], field: ""}]}]},
                    document))
        self.assertTrue(_validate({"tiers": self.TIERS,
                                   "defects": [{**self.ONE, "cross_references": [""]}]}, document))

    def test_uniqueness_is_the_engines_because_the_keyword_is_not_portable(self):
        """A repeated tier and a repeated cross-reference are both refused — by the
        engine, not here, and deliberately.

        `uniqueItems` is the keyword that would state it, and one of the two runtimes
        rejects a structured-output schema carrying it before any model call. A contract
        that 400s is worse than one that leaves a rule to the engine, so the rule stays
        where it already is: `_parse_tiers` fails the unit on a repeated tier, and
        `_parse_synthesis` costs a defect its assignment for a repeated reference. This
        test is what stops the keyword being added later for tidiness.
        """
        def walk(node):
            if isinstance(node, dict):
                self.assertNotIn("uniqueItems", node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        document = _load(SYNTHESIZER_SCHEMA)
        walk(document)
        self.assertEqual(_validate({"tiers": [self.TIERS[0], self.TIERS[0]],
                                    "defects": [self.ONE]}, document), [])
        self.assertEqual(_validate({"tiers": self.TIERS,
                                    "defects": [{**self.ONE, "cross_references": ["D4", "D4"]}]},
                                    document), [])

    def test_the_partition_and_the_tier_membership_are_the_engines_to_enforce(self):
        # Neither is expressible here: a schema cannot say "exactly these ids, each once"
        # or "one of the strings in a sibling array". The engine checks both, and this
        # records that a green schema is not a checked result.
        document = _load(SYNTHESIZER_SCHEMA)
        invented = {**self.ONE, "tier": "A tier nobody declared"}
        self.assertEqual(_validate({"tiers": self.TIERS, "defects": [invented]}, document), [])
        self.assertEqual(_validate({"tiers": self.TIERS, "defects": [self.ONE, self.ONE]},
                                   document), [])


class PhaseWorkerUnionContract(unittest.TestCase):
    """The DONE / BLOCKED result is a real discriminated union, not two nullable halves.

    A flat object of nullable fields accepts a DONE with no verification and a DONE
    carrying a question — i.e. a phase whose completion was never actually asserted,
    which the orchestrator would then commit.
    """

    DONE = {
        "outcome": {
            "result": "DONE",
            "summary": "Added the parser.",
            "changed_surface": "engine.py",
            "verification": "unittest: 12 passed",
            "deviations": "none",
        }
    }
    BLOCKED = {
        "outcome": {
            "result": "BLOCKED",
            "changed_surface": "none",
            "question": "Which store?",
            "options": "A or B",
            "recommendation": "A, because it is already a dependency.",
        }
    }

    def test_both_valid_shapes_validate(self):
        document = _load(WORKER_SCHEMA)
        self.assertEqual(_validate(self.DONE, document), [])
        self.assertEqual(_validate(self.BLOCKED, document), [])

    def test_union_is_nested_under_a_wrapper_key(self):
        # Load-bearing: a ROOT-level union is rejected by both runtimes with a 400
        # before any model call, so nesting it is what makes exclusivity enforceable.
        document = _load(WORKER_SCHEMA)
        self.assertEqual(set(document["required"]), {"outcome"})
        branches = document["properties"]["outcome"]["anyOf"]
        self.assertEqual(len(branches), 2)
        self.assertEqual(
            sorted(b["properties"]["result"]["enum"][0] for b in branches),
            ["BLOCKED", "DONE"],
        )

    def test_a_done_carrying_a_blocked_field_is_rejected(self):
        mixed = {"outcome": {**self.DONE["outcome"], "question": "Which store?"}}
        self.assertTrue(_validate(mixed, _load(WORKER_SCHEMA)))

    def test_a_done_missing_its_verification_evidence_is_rejected(self):
        # THE case this union exists to prevent: committing a phase whose completion
        # was never asserted.
        thin = {"outcome": {k: v for k, v in self.DONE["outcome"].items()
                            if k != "verification"}}
        self.assertTrue(_validate(thin, _load(WORKER_SCHEMA)))

    def test_a_done_with_empty_evidence_is_rejected(self):
        # The gap a presence-only schema leaves: every required field is there, so the
        # object validates, and the orchestrator commits a phase whose verification is
        # the empty string. minLength:1 closes it on the CLI path (both runtimes accept
        # the keyword). It does NOT make the field meaningful — "n/a" still passes —
        # which is why the contract keeps the orchestrator's own check on both paths.
        empty = {"outcome": {"result": "DONE", "summary": "", "changed_surface": "",
                             "verification": "", "deviations": ""}}
        self.assertTrue(_validate(empty, _load(WORKER_SCHEMA)))
        one_empty = {"outcome": {**self.DONE["outcome"], "verification": ""}}
        self.assertTrue(_validate(one_empty, _load(WORKER_SCHEMA)))

    def test_every_evidence_field_carries_a_non_empty_constraint(self):
        for branch in _load(WORKER_SCHEMA)["properties"]["outcome"]["anyOf"]:
            for name, prop in branch["properties"].items():
                if name == "result":
                    continue
                with self.subTest(branch=branch["title"], field=name):
                    self.assertEqual(prop.get("minLength"), 1)

    def test_blocked_can_report_the_paths_its_cleanup_protocol_needs(self):
        # plan-run tells the orchestrator to scope a BLOCKED cleanup to "only the paths
        # its result lists" rather than a blanket reset. That instruction is
        # unexecutable unless the BLOCKED branch has a field able to carry them.
        blocked = next(
            b for b in _load(WORKER_SCHEMA)["properties"]["outcome"]["anyOf"]
            if b["title"] == "BLOCKED"
        )
        self.assertIn("changed_surface", blocked["required"])
        with_paths = {"outcome": {**self.BLOCKED["outcome"],
                                  "changed_surface": "engine.py, tests/test_engine.py"}}
        self.assertEqual(_validate(with_paths, _load(WORKER_SCHEMA)), [])

    def test_a_blocked_without_a_question_is_rejected(self):
        thin = {"outcome": {k: v for k, v in self.BLOCKED["outcome"].items()
                            if k != "question"}}
        self.assertTrue(_validate(thin, _load(WORKER_SCHEMA)))

    def test_an_unknown_result_value_is_rejected(self):
        bogus = {"outcome": {**self.DONE["outcome"], "result": "PARTIAL"}}
        self.assertTrue(_validate(bogus, _load(WORKER_SCHEMA)))

    def test_every_json_example_in_the_contract_doc_validates(self):
        # The worker is briefed by the DOC, not the schema, so drift between them means
        # the model is asked for one shape and graded against another. Substring checks
        # cannot catch that — one example could take a flat shape the schema rejects while
        # every field name still appears somewhere. So parse each ```json block and validate
        # it, with the doc's angle-bracket placeholders filled in.
        doc = (WORKER_SCHEMA.parent / "phase-worker-contract.md").read_text(
            encoding="utf-8"
        )
        blocks = re.findall(r"```json\n(.*?)```", doc, re.DOTALL)
        self.assertGreaterEqual(len(blocks), 2, "expected a DONE and a BLOCKED example")

        document = _load(WORKER_SCHEMA)
        seen = set()
        for block in blocks:
            # `"<one or two sentences>"` is a placeholder, not a literal; substitute a
            # non-empty stand-in so the shape is what gets tested, not the prose.
            concrete = re.sub(r'"<[^"]*>"', '"placeholder"', block)
            instance = json.loads(concrete)
            self.assertEqual(
                _validate(instance, document), [],
                f"contract-doc example does not match the shipped schema:\n{block}",
            )
            seen.add(instance["outcome"]["result"])
        self.assertEqual(seen, {"DONE", "BLOCKED"}, "both branches must be documented")

    def test_the_doc_examples_would_catch_a_reverted_branch(self):
        # Guards the test above: prove it FAILS on the exact drift it exists to catch —
        # a BLOCKED example reverted to the pre-union flat shape.
        flat = {"result": "BLOCKED", "question": "q", "options": "o",
                "recommendation": "r"}
        self.assertTrue(_validate(flat, _load(WORKER_SCHEMA)))

    def test_no_shipped_doc_carries_a_stale_result_shape(self):
        # The result shape has ONE home (phase-worker-contract.md). Any other doc in the
        # skill that shows one — a flat `result: DONE` without the `outcome` wrapper, say
        # — would brief the worker for a shape the schema rejects. So scan every shipped
        # markdown doc rather than one named file: the check survives a doc being split,
        # renamed, or deleted, which is exactly how the previous version broke.
        # The validator's traversal, not a private `rglob("*.md")`. The glob folded no case,
        # so a `GUIDE.MD` carrying a stale result shape was markdown to production and
        # invisible here — a scan that "survives a doc being split, renamed or deleted"
        # stopped surviving a doc being named in capitals.
        docs = sorted(
            path
            for path, _relative, suffix in vcr.walk_tree_files(WORKER_SCHEMA.parent.parent)
            if suffix == ".md"
        )
        self.assertTrue(docs, "expected the plan-run skill to ship markdown docs")
        document = _load(WORKER_SCHEMA)
        found = 0
        for doc in docs:
            body = doc.read_text(encoding="utf-8")
            for block in re.findall(r"```json\n(.*?)```", body, re.DOTALL):
                if '"result"' not in block:
                    continue  # some other JSON, not a worker result
                instance = json.loads(re.sub(r'"<[^"]*>"', '"placeholder"', block))
                self.assertEqual(
                    _validate(instance, document), [],
                    f"{doc.name} shows a result shape the schema rejects:\n{block}",
                )
                found += 1
        # Non-vacuity: a skill that ships no example at all is a regression, not a pass.
        self.assertGreaterEqual(found, 2, "DONE and BLOCKED must be documented somewhere")



class TierNamesResultContract(unittest.TestCase):
    """The round that reconciles the batches' tier names: the final list, and a map from
    every entry the payload lists to one of those names by position. Nothing in it judges,
    re-ranks or removes a defect."""

    REPLY = {"tiers": ["Money can be taken twice", "A run stops instead of finishing"],
             "map": [{"entry": "N1", "tier": 1}, {"entry": "N2", "tier": 1},
                     {"entry": "N3", "tier": 2}]}

    def test_declares_the_final_tiers_and_the_map(self):
        document = _load(TIER_NAMES_SCHEMA)
        self.assertEqual(set(document["required"]), {"tiers", "map"})
        self.assertEqual(set(document["properties"]), {"tiers", "map"})
        record = document["properties"]["map"]["items"]
        self.assertEqual(set(record["required"]), {"entry", "tier"})
        self.assertEqual(record["properties"]["tier"]["type"], "integer")
        self.assertEqual(record["properties"]["tier"]["minimum"], 1)
        # A final name is capped, so a reply cannot outgrow the estimate it was packed under.
        names = document["properties"]["tiers"]["items"]
        self.assertEqual(names["maxLength"], 80)
        self.assertTrue(_validate({**self.REPLY, "tiers": ["x" * 81]}, document))
        self.assertEqual(_validate({**self.REPLY, "tiers": ["x" * 80, "y"]}, document), [])

    def test_a_real_reply_validates_and_a_map_keyed_by_name_does_not(self):
        # The map names a final tier by position, so it costs a few bytes per entry however
        # long the names; a map from entry to name would be refused here.
        document = _load(TIER_NAMES_SCHEMA)
        self.assertEqual(_validate(self.REPLY, document), [])
        self.assertTrue(_validate({**self.REPLY, "map": {"N1": "Money can be taken twice"}},
                                  document))
        self.assertTrue(_validate({**self.REPLY, "map": [{"entry": "N1", "tier": 0}]},
                                  document))
        self.assertTrue(_validate({**self.REPLY, "map": [{"entry": "N1",
                                                          "tier": "Money"}]}, document))
        self.assertTrue(_validate({"tiers": [], "map": []}, document))
        self.assertTrue(_validate({**self.REPLY, "summary": "s"}, document))

    def test_it_cannot_judge_rerank_or_drop_a_defect(self):
        def names(node):
            if isinstance(node, dict):
                yield from node.get("properties", {})
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        yield from names(value)
            elif isinstance(node, list):
                for item in node:
                    yield from names(item)
        self.assertFalse({"defect", "defects", "status", "severity", "verdict", "dropped"}
                         & set(names(_load(TIER_NAMES_SCHEMA))))


class OverviewResultContract(unittest.TestCase):
    """A tier summary, a summary of summaries and the overview share one contract: one
    paragraph, and nothing else. Its byte limit is stated in the payload and held by the
    engine, because it varies with the run's limits and a schema cannot carry it."""

    def test_one_summary_and_nothing_else(self):
        document = _load(OVERVIEW_SCHEMA)
        self.assertEqual(document["required"], ["summary"])
        self.assertEqual(set(document["properties"]), {"summary"})
        self.assertEqual(document["properties"]["summary"]["type"], "string")
        self.assertNotIn("maxLength", document["properties"]["summary"])
        self.assertEqual(_validate({"summary": "Two themes; the first matters most."},
                                   document), [])
        self.assertTrue(_validate({"summary": ""}, document))
        self.assertTrue(_validate({"summary": "s", "tiers": []}, document))
        self.assertTrue(_validate({}, document))


if __name__ == "__main__":
    unittest.main()
