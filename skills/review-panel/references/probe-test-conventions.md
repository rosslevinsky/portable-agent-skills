## Writing the test conventions

This run also asks for the tree's test conventions, as your payload's `## Test conventions`
section says, so they are a third thing you answer beside the two questions. Later agents
each write one failing test for a defect, and your notes are how they write it the way this
tree does, without each relearning it.

Write **one note per test suite** — a tree with two suites, say a Python one and a
JavaScript one, gets two notes — and none where the tree holds no tests. In each:

- `suite`: the suite's directory, `.` for the root.
- `runner`: the test runner, by name.
- `run_one`: the argv that runs one test, with `{test}` where its name or path goes. Try it
  on one existing test if you can.
- `location` and `naming`: where the tests for a given source file live, and how test files
  and tests are named.
- `helpers`: the shared fixtures and helper files a new test would use.
- `example`: one existing test file that is a good model to copy.
- `rules`: what the repository's own instruction and contributing documents say about
  writing tests, quoted, with the document's path.

Every path is relative to the copy's root and **must exist** in the copy as you were given
it; one that does not is dropped. Use null for what you could not find, never a guess.
