---
name: extract-hooks
description: >
  Audit TSX files, identify non-UI logic, and extract it into custom hooks.
  Use when the user invokes /extract-hooks, or says "extract hooks", "move
  logic out of components", or "separate logic from layout".
---

# Extract Hooks

Move non-UI logic out of `.tsx` files into custom `use*.ts` hooks, so that `.tsx` files contain only layout and hook calls, without changing behavior.

**Non-UI logic includes:** `useState`, `useEffect`, event handlers, API calls, data fetching, data transforms, validation, derived state, and business rules.

---

## Phase 1 — Inventory

Do not assume anything. Explore the actual codebase first.

1. Find all `.tsx` files. For each, read it and record:
   - Total lines and approximate lines of non-UI logic vs. layout/JSX
   - Categories of logic present (state, API calls, event handlers, validation, derived state, etc.)
   - Whether the same or similar logic appears in another `.tsx` file (duplication)

2. Find the existing hooks directory. Note whether it exists and where it should be created if not, based on project structure conventions.

3. List any existing `use*.ts` or `use*.tsx` files and what they do — do not duplicate them.

4. **Present the inventory to the user** as a table before proceeding. If no `.tsx` file contains extractable logic, report that and stop.

---

## Phase 2 — Recommend

Propose a list of new hook files. For each hook:

- **Name and signature** — e.g. `useRoomForm(existingRoom?: Room): { fields, handlers, submit }`
- **Encapsulates** — what logic it extracts
- **Replaces logic in** — which `.tsx` files benefit
- **Priority:**
  - **High** — duplicated logic across multiple files, or a single file with >100 lines of non-UI logic
  - **Medium** — single file with 30–100 lines of non-UI logic
  - **Low** — minor state or trivial effects; extraction is optional

Do not propose a hook that is just a thin wrapper around a single `useState` — only extract when it improves reuse, readability, or separates a real concern. Do not move pure helper functions into hooks; keep them as plain functions.

Present the list as a table.

---

## Phase 3 — Confirm

After presenting the recommendation table, ask the user exactly this:

> "Which hooks should I implement?
> - **all** — implement everything above
> - **high** / **medium** / **low** — implement by priority level
> - list names — e.g. `useVoting, useRoomForm`
> - **none** — stop here"

**Do not proceed to Phase 4 until the user responds.** If operating autonomously
(no user available), default to implementing **high** priority hooks and note
the assumption.

---

## Phase 4 — Implement

If the hooks directory does not exist, create it at the project-appropriate path before writing any hooks.

For each approved hook, in priority order:

1. **Define the hook API** before writing any code:
   - Inputs (arguments)
   - Returned state values
   - Returned actions/handlers
   - Required side effects

2. **Name hooks after the domain behavior**, not the component they came from (e.g. `useVoting`, not `useRoomDetailVoting`).

3. **Extract the logic** from the source `.tsx` file(s) into the hooks directory. Preserve exact behavior — do not refactor or rename while moving.

4. **Update the `.tsx` file(s)** to call the hook and remove the extracted code. The component should be left with only layout, JSX, and hook calls — no raw `useState`/`useEffect`/API calls.

5. **Correctness checks** during extraction:
   - Keep dependency arrays accurate
   - Preserve referential stability where callers depend on it
   - Watch for stale closure bugs introduced by the move
   - If extraction reveals unrelated responsibilities in the same block, split into multiple hooks rather than bundling them

6. **Verify** after each hook:
   - Run existing tests if available
   - Run lint/typecheck if available
   - Scan for changed render behavior

---

## Phase 5 — Review

When all hooks are implemented:

Run the `cyw` skill to review the work. If the skill is unavailable, do a
self-check: re-read each modified file, confirm no logic remains inline in
`.tsx` files, confirm all tests/lint still pass, and list any assumptions or
risks that could not be verified locally.

---

## Constraints

- Move logic as-is. Do not rewrite, optimise, or rename things while extracting — that is a separate task.
- Prefer placing hooks close to the component that uses them unless the logic is clearly shared across multiple components.
- Follow the project's existing naming conventions, import style, and file structure throughout.
- Follow the project's `CLAUDE.md` / `AGENTS.md` conventions (whichever exists).
