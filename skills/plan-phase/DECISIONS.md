# plan-phase — decisions

Why this skill's contract is shaped the way it is. Nothing here is an instruction, and
nothing on the runtime path reads it: `plan-phase` works from `SKILL.md` and
`references/v2-templates.md` alone. This is for whoever proposes changing one of those
rules, so the reasoning does not have to be rediscovered — or re-litigated — every time
someone looks at the tracker contract with fresh eyes.

## The tracker contract needs no Markdown model

Rules a line-based reader can apply with **no Markdown model at all** — which is the point.
The checker bans fences and HTML comments outright rather than parsing past them, and
gives inline code no meaning, so there is no Markdown parser whose bugs could turn into
*silently skipped* validation.

## The threat model is a typo, not an adversary

The threat model is a **typo**, not an adversary: `plan-phase` writes this file, `plan-run`
reads it, and a human reviews the diff. Rules that cost nothing — an ASCII character class,
a length bound, splitting on CommonMark line endings — are worth having. Rules that would
cost another scan over the text are not: a second pass over Markdown is precisely what this
shape exists to avoid.
