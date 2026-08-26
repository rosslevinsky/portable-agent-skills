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

    if isinstance(instance, list) and "items" in schema:
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


class PortabilityInvariants(unittest.TestCase):
    """Rules every shipped schema must follow to be accepted by BOTH runtimes."""

    def _all(self):
        return [JUDGE_SCHEMA, REVIEW_SCHEMA, WORKER_SCHEMA]

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
        # plan-run tells the orchestrator to scope a BLOCKED cleanup to "the paths the
        # worker reported changing" rather than a blanket reset. That was unexecutable
        # while the BLOCKED branch had no field able to carry them.
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
        # cannot catch that — one example could revert to the old flat shape while every
        # field name still appears somewhere. So parse each ```json block and validate
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


if __name__ == "__main__":
    unittest.main()
