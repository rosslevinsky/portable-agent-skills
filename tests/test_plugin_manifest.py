#!/usr/bin/env python3
"""The Agent Plugins manifest, checked against the parts of the spec a client enforces.

`plugin.json` makes this pack installable by any Agent Plugins 1.0.0 client rather than
only by `install.py`. The format is vendor-neutral, which is the whole reason it is here:
adopting a single vendor's plugin format would have picked a side between the two runtimes
the pack exists to serve equally.

The layout the spec wants is the layout the pack already had — `skills/<name>/SKILL.md`,
one directory per skill — so nothing moved to adopt it.

Three of these assertions are regression guards that pass on arrival. The fourth,
`test_the_manifest_version_matches_the_changelog`, is the one with teeth: it stops a second
answer to a question the changelog already answers.
"""
import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "plugin.json"

sys.path.insert(0, str(REPO_ROOT))
import install as installer  # noqa: E402

# Section 5.3: for Agent Plugins 1.0.0 this value MUST be the canonical identifier, and a
# manifest carrying anything else is invalid — the client rejects the plugin outright.
CANONICAL_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"

# Section 5: 1-64 characters, lowercase alphanumerics / hyphens / periods, first and last
# characters alphanumeric, and no `--` or `..` run.
NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")


class TheManifestSatisfiesTheSpec(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_the_schema_identifier_is_the_canonical_one(self):
        """Not decorative: a wrong or missing value makes the manifest invalid outright."""
        self.assertEqual(self.manifest.get("$schema"), CANONICAL_SCHEMA)

    def test_the_name_satisfies_the_specs_character_rules(self):
        name = self.manifest.get("name", "")
        self.assertTrue(1 <= len(name) <= 64, f"name must be 1-64 characters: {name!r}")
        self.assertTrue(NAME_RE.fullmatch(name),
                        f"name must be lowercase alphanumerics, hyphens and periods, "
                        f"starting and ending alphanumeric: {name!r}")
        self.assertNotIn("--", name, "no consecutive hyphens")
        self.assertNotIn("..", name, "no consecutive periods")

    def test_the_manifest_version_matches_the_changelog(self):
        """The anti-drift assertion, and the reason this file exists.

        `install.py` reads the pack's version from `CHANGELOG.md` rather than storing it,
        precisely so a release has one number to bump instead of two — its own docstring
        says a second copy drifts "the first time someone bumps only the enforced one".
        Writing `version` here creates exactly that second copy, so it is enforced too and
        the drift cannot happen quietly.

        Declaring no version at all would also have been spec-legal. It is worse: a
        consumer resolving an update has nothing to compare.
        """
        self.assertEqual(
            self.manifest.get("version"), installer.pack_version(),
            "plugin.json's version and the CHANGELOG's latest release heading disagree — "
            "bump both in the same change, or the pack advertises a version it is not")

    def test_the_description_hardcodes_no_skill_count(self):
        """A count here would be a third place to bump, after the README's and the budget file.

        The README already carries a skill-count check for this exact reason; the answer
        there was a rule, and the answer here is not to write the number down at all.
        """
        digits = [c for c in self.manifest.get("description", "") if c.isdigit()]
        self.assertEqual(
            digits, [],
            "the description carries a figure that will go stale when a skill is added; "
            "describe the pack without counting it")


class TheLayoutTheSpecRequiresIsTheLayoutThePackHas(unittest.TestCase):
    """Spec: skills are "each immediate child directory of `skills/` containing a path named
    exactly `SKILL.md` that resolves to a regular file".

    Asserted against `install.py`'s own discovery so the two cannot disagree about what a
    skill is — the installer and any Agent Plugins client must find the same set, or an
    install and a plugin load ship different packs.
    """

    def test_the_manifest_sits_at_the_plugin_root_beside_skills(self):
        self.assertTrue(MANIFEST.is_file(), "plugin.json must be at the plugin root")
        self.assertTrue((REPO_ROOT / "skills").is_dir(),
                        "a client discovers skills under skills/ at the same root")

    def test_spec_discovery_and_installer_discovery_find_the_same_skills(self):
        by_spec = sorted(
            child.name for child in (REPO_ROOT / "skills").iterdir()
            if child.is_dir() and (child / "SKILL.md").is_file()
            and not (child / "SKILL.md").is_symlink()
        )
        by_installer = installer.discover_skills(REPO_ROOT / "skills")
        self.assertEqual(
            by_spec, by_installer,
            "an Agent Plugins client and install.py would ship different skill sets")
        self.assertGreater(len(by_spec), 0, "no skills discovered — the layout is broken")


if __name__ == "__main__":
    unittest.main()
