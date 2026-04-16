---
name: security-review-codebase-hierarchical
description: "Use when the user asks for a deep, hierarchical, or thorough security review of the codebase. Breaks the codebase into architectural components and reviews each with focused sub-agents, then synthesizes findings. Better than security-review-codebase for large or complex codebases."
---

# Security Review: Hierarchical Multi-Pass

_Classification: Runtime-limited — requires multi-agent orchestration capabilities. In a single-agent runtime, use the `security-review-codebase` skill instead — it covers the same vulnerability categories but does not produce the hierarchical artifact set (architecture doc, component decomposition, per-component reports, or explicit cross-component analysis pass)._

Perform a deep security audit of the entire codebase using a hierarchical, multi-agent approach. Review proceeds from architecture understanding → attack surface mapping → parallel component sub-reviews → cross-component analysis → synthesis.

## Objective

Identify HIGH-CONFIDENCE security vulnerabilities across the full codebase. Do not comment on code quality, style, or theoretical issues. Only flag issues where exploitation potential is >80% confident.

## Critical Instructions

1. **MINIMIZE FALSE POSITIVES**: Only flag issues where you're >80% confident of actual exploitability
2. **AVOID NOISE**: Skip theoretical issues, style concerns, or low-impact findings
3. **FOCUS ON IMPACT**: Prioritize vulnerabilities leading to unauthorized access, data breaches, or system compromise
4. **EXCLUSIONS** — Do NOT report:
   - Denial of Service (DOS) vulnerabilities, even if they allow service disruption
   - Secrets or sensitive data stored on disk (handled by other processes)
   - Rate limiting or resource exhaustion issues

## Security Categories to Examine

**Input Validation Vulnerabilities:**
- SQL injection via unsanitized user input
- Command injection in system calls or subprocesses
- XXE injection in XML parsing
- Template injection in templating engines
- NoSQL injection in database queries
- Path traversal in file operations

**Authentication & Authorization Issues:**
- Authentication bypass logic
- Privilege escalation paths
- Session management flaws
- JWT token vulnerabilities
- Authorization logic bypasses

**Crypto & Secrets Management:**
- Hardcoded API keys, passwords, or tokens
- Weak cryptographic algorithms or implementations
- Improper key storage or management
- Cryptographic randomness issues
- Certificate validation bypasses

**Injection & Code Execution:**
- Remote code execution via deserialization
- Pickle injection in Python
- YAML deserialization vulnerabilities
- Eval injection in dynamic code execution
- XSS vulnerabilities in web applications (reflected, stored, DOM-based)

**Data Exposure:**
- Sensitive data logging or storage
- PII handling violations
- API endpoint data leakage
- Debug information exposure

Additional notes:
- Even if something is only exploitable from the local network, it can still be a HIGH severity issue

## False Positive Filtering

**Hard Exclusions — automatically exclude:**
1. Denial of Service (DOS) vulnerabilities or resource exhaustion attacks
2. Secrets or credentials stored on disk if they are otherwise secured
3. Rate limiting concerns or service overload scenarios
4. Memory consumption or CPU exhaustion issues
5. Lack of input validation on non-security-critical fields without proven security impact
6. Input sanitization concerns for GitHub Action workflows unless clearly triggerable via untrusted input
7. A lack of hardening measures — only flag concrete vulnerabilities
8. Race conditions or timing attacks that are theoretical rather than practical
9. Vulnerabilities related to outdated third-party libraries
10. Memory safety issues in memory-safe languages (Rust, Go, etc.)
11. Files that are only unit tests or only used as part of running tests
12. Log spoofing concerns — outputting un-sanitized user input to logs is not a vulnerability
13. SSRF vulnerabilities that only control the path — SSRF is only a concern if it can control the host or protocol
14. Including user-controlled content in AI system prompts is not a vulnerability
15. Regex injection or Regex DOS concerns
16. Insecure documentation — do not report findings in markdown or documentation files
17. A lack of audit logs is not a vulnerability

**Precedents:**
1. Logging high-value secrets in plaintext is a vulnerability. Logging URLs is assumed safe.
2. UUIDs can be assumed to be unguessable and do not need to be validated.
3. Environment variables and CLI flags are trusted values. Attacks relying on controlling env vars are invalid.
4. Resource management issues (memory leaks, file descriptor leaks) are not valid.
5. Subtle low-impact web vulnerabilities (tabnabbing, XS-Leaks, prototype pollution, open redirects) should not be reported unless extremely high confidence.
6. React and Angular are generally secure against XSS. Do not report XSS in `.tsx`/`.jsx` files unless `dangerouslySetInnerHTML`, `bypassSecurityTrustHtml`, or similar unsafe methods are used.
7. Most vulnerabilities in GitHub Action workflows are not exploitable in practice — require a very specific concrete attack path.
8. A lack of permission checking or authentication in client-side JS/TS code is not a vulnerability.
9. Only include MEDIUM findings if they are obvious and concrete.
10. Most vulnerabilities in Jupyter notebooks are not exploitable in practice — require a concrete attack path with untrusted input.
11. Logging non-PII data is not a vulnerability even if sensitive. Only report logging vulnerabilities for secrets, passwords, or PII.
12. Command injection in shell scripts is generally not exploitable — only report with a concrete untrusted-input attack path.

**Confidence Scoring:**
- 0.9–1.0: Certain exploit path identified, tested if possible
- 0.8–0.9: Clear vulnerability pattern with known exploitation methods
- 0.7–0.8: Suspicious pattern requiring specific conditions to exploit
- Below 0.7: Do not report (too speculative)

Only include findings with confidence ≥ 0.8.

## Output Directory Setup

Before any analysis, set up the output directory:

1. Determine the project root (the directory containing `.git/`).
2. Create a timestamped run directory: `<project-root>/security-review/YYYY-MM-DDTHH-MM/`
3. Create or update a `latest` symlink pointing to the new run directory. From the project root: `ln -sfn <absolute-run-dir-path> <project-root>/security-review/latest` — the `-f` flag overwrites any existing symlink. Use absolute paths to avoid working-directory ambiguity.
4. Ensure `security-review/` is in the project's `.gitignore`. Check if `.gitignore` exists: if it does not, create it. If it exists but does not contain `security-review/`, append the line. Do NOT modify `.gitignore` if the entry already exists.
5. Print the run directory path to the user so they can follow along.

All intermediate files and the final report are written inside the run directory.

## Phase 0 — Documentation & Architecture Discovery

Read every architectural guidance file you can find. At minimum:
- `CLAUDE.md` (root and any subdirectory variants)
- `README.md` / `README.rst`
- `agents.md`, `AGENTS.md`, or any file named `architecture.*`, `ARCHITECTURE.*`
- Any files under `docs/`

Goal: understand the intended security model — what is trusted, what is untrusted, where are the trust boundaries, and what sensitive operations exist. Record this understanding; it will anchor all subsequent phases.

Write a brief summary to `<run-dir>/00-architecture.md`.

## Phase 1 — Attack Surface Mapping

Using file search and read tools, map the attack surface:

1. Identify the tech stack, frameworks, and languages in use.
2. Locate entry points: HTTP handlers, CLI argument parsers, message consumers, file processors, webhook handlers.
3. Locate trust boundaries: authentication middleware, authorization checks, input validation layers.
4. Identify high-risk patterns: subprocess calls, eval/exec, deserialization, file I/O with user-controlled paths, raw SQL construction, template rendering.
5. Note existing security frameworks and sanitization patterns already in use.

Write the results to `<run-dir>/01-attack-surface.md`. Include:
- Entry points table (location, input source, trust level)
- Trust boundary map
- High-risk pattern inventory with file paths

## Phase 2 — Component Decomposition

Based on Phase 0–1 findings, divide the codebase into **4–8 logical components** that represent natural security boundaries (e.g. "API layer", "auth system", "file processing", "database access", "background jobs", "admin interface").

For each component, define:
- A short name (slug)
- A one-sentence description
- The file globs that belong to it

Write the component plan to `<run-dir>/02-plan.md` as a checklist:

```
## Components

- [ ] api-layer — HTTP request handlers and routing (src/api/**)
- [ ] auth — Authentication and session management (src/auth/**)
- [ ] db — Database models and query construction (src/models/**)
- [ ] file-processing — User-uploaded file handling (src/files/**)
...
```

## Phase 3 — Parallel Component Sub-Reviews

Before launching component reviews, read `<run-dir>/01-attack-surface.md` into your context — you will embed its contents in each component review's prompt.

Launch all component reviews in parallel if the runtime supports spawning
multiple concurrent work units. Otherwise, run each component review
sequentially. The goal is to review each component independently before
cross-component analysis.

> **Claude adapter:** Launch all component sub-agents in a single message (one
> Agent tool call per component, all in the same response). Run them as
> foreground agents so you receive all results before proceeding to Phase 4.
> Do not use `run_in_background: true`.

Each component review prompt must include:
- The component name and file globs to examine
- The full text of the attack surface document
- The full Security Categories list and False Positive Filtering rules from this skill
- The instruction to return findings in the standard finding format (see Output Format below)
- The instruction NOT to write files — return findings as text in the response only

Once all component reviews have returned, for each component write its findings to `<run-dir>/03-<component-slug>.md` and update its checkbox from `[ ]` to `[x]` in `<run-dir>/02-plan.md`.

Then proceed to Phase 4.

## Phase 4 — Cross-Component Data Flow Analysis

This phase is performed by the orchestrating agent (not the component reviewers). Component reviews are scoped to one component and cannot see cross-boundary flows.

Review all component findings from Phase 3. Then perform targeted reads of the code at component interfaces — the points where data crosses from one component into another — and ask:

1. Does untrusted input from an entry point flow through multiple components before reaching a sensitive sink?
2. Are there privilege escalation paths that span components (e.g. low-privilege data enters the auth component and is used to make an authorization decision)?
3. Are there IDOR or access control gaps visible only when two components are considered together?
4. Does data from an external source get sanitized in one component but then passed unsanitized to a second component that re-uses it in a dangerous context?

Write any new findings to `<run-dir>/04-cross-component.md`. Apply the same false positive filtering and confidence threshold (≥ 0.8).

## Phase 5 — Synthesis & Final Report

1. Collect all findings from Phase 3 component files and Phase 4 cross-component file.
2. Deduplicate: if multiple sub-agents flagged the same issue, merge into one finding.
3. Apply final false positive filtering pass.
4. Sort by severity (HIGH → MEDIUM → LOW).
5. Write `<run-dir>/REPORT.md` — the final deliverable.
6. Print the full report to the user.

## Required Output Format

Each finding must include:

```
# Vuln N: <Category>: `<file>:<line>`

* Severity: High | Medium | Low
* Category: <category_slug>
* Component: <component-slug>
* Confidence: <0.8–1.0>
* Description: <what is wrong and why it is exploitable>
* Exploit Scenario: <concrete attacker action and outcome>
* Recommendation: <specific fix with example code if applicable>
```

## Severity Guidelines

- **HIGH**: Directly exploitable — RCE, data breach, authentication bypass
- **MEDIUM**: Requires specific conditions but with significant impact
- **LOW**: Defense-in-depth issues or lower-impact vulnerabilities (only include if confidence ≥ 0.8)

## Final Report Structure

```markdown
# Security Review: <Project Name>
Date: <YYYY-MM-DD>
Run directory: security-review/<timestamp>/

## Summary
<total finding counts by severity>

## Findings

<findings sorted HIGH → MEDIUM → LOW>

## Reviewed Components
<list of components reviewed with file scope>
```

If no findings meet the confidence threshold:

```markdown
# Security Review: <Project Name>

No high-confidence vulnerabilities found.
```
