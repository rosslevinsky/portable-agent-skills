# plan-duel — decisions

Three behaviors of this engine read as defects and are not. Nothing here is an instruction,
and no run reads it: a duel works from `SKILL.md`, the engine and the templates alone. Read it
before proposing a change to one of the three — each has been proposed once already, and what
makes the current shape right is not visible from the code on its own.

## An unusable judge scores zero; it does not halt the duel

**Proposed.** A judge spawn that times out while a resumed duel is re-judging its earlier
rounds should stop the run, the way `--timeout` describes a timed-out spawn elsewhere.

**Declined.** Treating an unparseable or unrunnable judge as a score of zero is what the
resume path is built on, and two tests hold it from opposite sides: one asserts the fallback
fires when the judge cannot run at all, the other asserts it stays silent when a working judge
is available. Neither passes by swallowing every case. Every round before the resume point has
to land a score, because the exit check reads all of them, and a missing entry raises instead
of degrading. Halting would discard a duel that has already produced two usable plans, on the
strength of one judgment being unavailable.

**What would change it.** A run where a zero from a timed-out re-judge changed which plan the
summary points at. That is the harm the halt would prevent, and nobody has seen it. The
`--timeout` help now separates the spawns that halt from the ones that degrade, so the promise
and the code agree; a report that they still disagree reopens this.

## Below the supported floor, the interpreter fails before any check of ours

**Proposed.** On Python 3.6 the engine should print the `Python 3.10+ required` message its
prerequisites promise, rather than dying at a syntax error the interpreter raises while
reading the file.

**Declined.** A version check cannot run until the file parses, and the syntax an older
interpreter chokes on is spread through the engine rather than sitting in one guarded place.
Printing the message first means a second entry file written entirely in old-compatible
syntax, whose only job is to reject the interpreter — and which then has to stay compatible
for as long as the engine exists. The floor is stated in the prerequisites, and every runtime
this pack targets ships an interpreter well above it.

**What would change it.** A supported runtime that actually ships an interpreter below the
floor. Then the second file buys something real instead of guarding a case nobody reaches.

## A role command with no prompt marker is accepted

**Proposed.** Validation should refuse an adapter role whose command carries no `⟪prompt⟫`
marker. As it stands the CLI starts without the prompt and the mistake only shows after the
spawn.

**Declined.** Most role configurations in the test suite carry no marker on purpose: they are
stubs that read nothing and write a fixed file. The refusal would reject the majority of
working configurations in order to catch a typo in a minority of real ones. Validation here
checks that a role has a usable shape, not that the command will do anything sensible with
what it is handed — a command can also ignore an argument it does receive, and no check
catches that either.

**What would change it.** A way to tell a deliberately prompt-free command from a mistyped
one. A role field declaring the prompt intentionally unused would make the check safe, and is
the shape any fix here should take.
