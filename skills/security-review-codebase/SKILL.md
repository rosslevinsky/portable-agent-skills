---
name: security-review-codebase
description: "This skill should be used when the user asks to 'security review the codebase', 'audit the codebase for vulnerabilities', 'run a full security audit', 'run a deep, thorough, or hierarchical security review', or 'check the whole project for security issues' — any security review scoped to the entire checked-in codebase (not uncommitted changes, not general code quality). Runs a single-pass audit by default and scales to an optional hierarchical deep mode (per-component sub-reviews plus a cross-component data-flow pass) for large or complex codebases — per-component reviews run as sub-agents where available, sequentially otherwise."
---

# Security Review: Full Codebase

_Classification: Degraded — the default single-pass review runs in any runtime (parallelism is optional; the sequential fallback preserves full coverage). The optional deep mode (`references/hierarchical-mode.md`) uses fresh sub-agents for per-component reviews where available; without them it runs the same decomposition and cross-component pass sequentially in one context. That keeps the method intact, but on a **very large** codebase a single accumulating context can thin the thoroughness of later components' reviews — a coverage risk, not just a speed/hygiene loss, which is why this is Degraded rather than Full. (It differs from `plan-run`, which stays Full because its per-phase execution units are individually bounded and need not all fit in one context.)_

_Progress: bounded — each per-component sub-agent returns its findings on completion; deep mode adds parallel fan-out, not a live progress channel, so no progress file is used._

Perform a security-focused audit of the entire checked-in codebase to identify HIGH-CONFIDENCE security vulnerabilities with real exploitation potential. This is not a general code review — focus ONLY on concrete security vulnerabilities.

## Objective

Identify HIGH-CONFIDENCE security vulnerabilities across the full codebase. Do not comment on code quality, style, or theoretical issues. Only flag issues where exploitation potential scores **≥ 0.8** on the confidence scale below.

## Critical Instructions

1. **MINIMIZE FALSE POSITIVES**: Only flag issues you score **≥ 0.8** for actual exploitability
2. **AVOID NOISE**: Skip theoretical issues, style concerns, or low-impact findings
3. **FOCUS ON IMPACT**: Prioritize vulnerabilities leading to unauthorized access, data breaches, or system compromise
4. **EXCLUSIONS** — Do NOT report denial of service, rate limiting or resource
   exhaustion, or a secret held in a file whose **purpose** is to hold it. *Hard
   Exclusions* under False Positive Filtering is the authoritative list, and it states
   what the secrets carve-out does **not** cover — read it there, not this summary
5. **THIS AUDIT ADDS NO FILES TO THE AUDITED TREE — in either mode.** It reads someone
   else's repository and that is all it does. Single-pass writes nothing to disk at all;
   its report is the output below. Deep mode has a persistent artifact set, and that set
   goes to a run directory under the OS temporary path — checked to be absolute *and*
   outside the tree being audited, with its absolute path printed. Never create a run
   directory inside the project, and never edit the project's `.gitignore` to hide one: a
   review whose whole promise is that it only reads has no business modifying tracked
   config, and since nothing is written inside the project there is nothing to ignore.

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

## Analysis Methodology

### Phase 1 — Codebase Reconnaissance

Use file search and read tools to map the attack surface:

1. Identify the tech stack, frameworks, and languages in use
2. Locate entry points: HTTP handlers, CLI argument parsers, message consumers, file processors
3. Locate trust boundaries: authentication middleware, authorization checks, input validation layers
4. Identify high-risk patterns: subprocess calls, eval/exec, deserialization, file I/O with user-controlled paths, raw SQL construction, template rendering
5. Note existing security frameworks and sanitization patterns already in use

### Phase 2 — Vulnerability Assessment

For each high-risk area identified in Phase 1:

1. Trace data flow from untrusted input sources to sensitive operations
2. Look for privilege boundaries being crossed unsafely
3. Identify injection points and unsafe deserialization
4. Compare patterns against established secure practices in the same codebase — flag deviations

### Phase 3 — False Positive Filtering

For each candidate finding, apply the hard exclusions and precedents below before including it in the report.

## False Positive Filtering

**Hard Exclusions — automatically exclude:**
1. Denial of Service (DOS) vulnerabilities or resource exhaustion attacks
2. A secret held in a file whose **purpose** is to hold it, access-controlled and outside
   version control — a deployment `.env`, a mounted secret. Nothing else is covered: a
   credential committed to the repository or written into source is the hardcoded-secret
   category above, and a secret written to a log is Precedent 1 — both reportable
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
16. Insecure *guidance* in documentation — an example in a README that models a bad
    practice is not itself a vulnerability. This does **not** extend to real secrets: a
    live key, token, or password committed in a markdown file is a finding at its own
    impact, and the extension of the file it sits in does not change that.
17. A lack of audit logs is not a vulnerability

**Precedents:**
1. Logging high-value secrets in plaintext is a vulnerability. Logging URLs is assumed safe.
2. UUIDs can be assumed to be unguessable and do not need to be validated.
3. Environment variables and CLI flags are trusted **when they come from the operator
   invoking the program** — attacks that assume control of them on that path are invalid.
   They are not trusted where the program *builds* them from untrusted input: a command
   line assembled out of a request parameter is argument injection, and the sink that
   assembles it is the finding.
4. Resource management issues (memory leaks, file descriptor leaks) are not valid.
5. Subtle low-impact web vulnerabilities (tabnabbing, XS-Leaks, prototype pollution, open redirects) should not be reported unless extremely high confidence.
6. React and Angular are generally secure against XSS. Do not report XSS in `.tsx`/`.jsx` files unless `dangerouslySetInnerHTML`, `bypassSecurityTrustHtml`, or similar unsafe methods are used.
7. Most vulnerabilities in GitHub Action workflows are not exploitable in practice — require a very specific concrete attack path.
8. A lack of permission checking or authentication in client-side JS/TS code is not a vulnerability.
9. Only include MEDIUM findings if they are obvious and concrete.
10. Most vulnerabilities in Jupyter notebooks are not exploitable in practice — require a concrete attack path with untrusted input.
11. Logging non-PII data is not a vulnerability even if sensitive. Only report logging vulnerabilities for secrets, passwords, or PII.
12. Command injection in shell scripts is generally not exploitable — only report with a concrete untrusted-input attack path.

**Signal Quality Criteria — for remaining findings, assess:**
1. Is there a concrete, exploitable vulnerability with a clear attack path?
2. Does this represent a real security risk vs theoretical best practice?
3. Are there specific code locations and reproduction steps?
4. Would this finding be actionable for a security team?

**Confidence Scoring:**
- 0.9–1.0: Certain exploit path identified, tested if possible
- 0.8–0.9: Clear vulnerability pattern with known exploitation methods
- 0.7–0.8: Suspicious pattern requiring specific conditions to exploit
- Below 0.7: Do not report (too speculative)

Only include findings with confidence ≥ 0.8.

## Final Reminder

Focus on HIGH and MEDIUM findings only. Better to miss some theoretical issues than flood the report with false positives. Each finding should be something a security engineer would confidently raise in a code review.

A fix for a reported vulnerability is new code and gets the same review — re-run this audit
scoped to the changed files. Remediation is where a review's own defects concentrate.

## Execution Steps

Run this analysis as parallel work units if supported, otherwise sequentially:

1. **Vulnerability Discovery** — Search the codebase to map the attack surface and produce a candidate list of vulnerabilities with file and line references. Apply the Analysis Methodology above.

2. **False Positive Filter (per finding)** — For each candidate vulnerability, re-read the relevant code and apply the False Positive Filtering rules above. Return a confidence score (0.0–1.0) and a pass/fail verdict. Run these in parallel if supported, otherwise process each finding sequentially.

   **Where a fresh work unit is available, run a finding's filter in one that did not
   produce it** — the context that found a vulnerability is a poor judge of it. The burden
   is a concrete attack path: the untrusted input, its route, the line it reaches. A finding
   that cannot be given one fails, however plausible. Expect a large share to fail.

3. **Synthesis** — Collect all findings that passed filtering with confidence ≥ 0.8. Write the final report.

## Deep mode (optional) — hierarchical multi-component review

For a **large or complex codebase**, the single-pass review above can lose coverage and miss
cross-boundary issues. When the codebase is big enough to warrant
it, escalate to **deep mode**. Sub-agents accelerate deep mode; they are **not** a
precondition for it. Deep mode: understand the architecture, decompose the codebase
into 4–8 security components, review each component independently (a fresh sub-agent per
component where supported), then run a dedicated **cross-component data-flow analysis** that
catches vulnerabilities visible only when two components are considered together — multi-hop
taint, cross-boundary privilege escalation, IDOR across components. Deep mode also writes a
persistent artifact set (architecture doc, per-component reports, final report).

Deep mode uses the **same** Security Categories, False Positive Filtering, confidence
threshold (≥ 0.8), finding format, and severity guidelines defined here — it changes only the
orchestration. Where sub-agents are unavailable, run the same decomposition and
cross-component pass **sequentially in this context**, persisting each component's findings
to its report file as you go and treating those files, not working memory, as the source of
truth. Prefer the sub-agent path when the codebase is too large to review in one context
with care; `references/hierarchical-mode.md` carries that caveat in full.

The full deep-mode procedure is in `references/hierarchical-mode.md` (see References). Read
it only when running deep mode.

## Required Output Format

Output findings in markdown. Each finding must include: file path, line number, severity, category, description, exploit scenario, and fix recommendation.

### Example Finding

```
# Vuln 1: SQL Injection: `src/db/users.py:87`

* Severity: High
* Category: sql_injection
* Description: The `username` parameter from the HTTP request is directly interpolated into a SQL query string without parameterization.
* Exploit Scenario: Attacker sends `username=admin'--` to bypass authentication and access any account.
* Recommendation: Use parameterized queries or an ORM. Replace string interpolation with `cursor.execute("SELECT * FROM users WHERE username = %s", (username,))`.
```

## Severity Guidelines

- **HIGH**: Directly exploitable — RCE, data breach, authentication bypass
- **MEDIUM**: Requires specific conditions but with significant impact
- **LOW**: defense-in-depth and lower-impact issues — **not reported**. They are what
  "AVOID NOISE" excludes, and a report carrying them buries the findings that matter.

Severity is assigned from **impact**, and it is assigned before this rule applies. A finding
that clears Precedent 5's "extremely high confidence" bar with a concrete attack path has an
impact that makes it MEDIUM or HIGH — so it is reported, at that severity. LOW is the band
for what stays speculative or defensive, not a place to file something real.

If no findings meet the confidence threshold, output the template below. The scope lines are
required: a clean report that never says what it covered reads the same as one nobody ran.

```
# Security Review: Full Codebase

No high-confidence vulnerabilities found.

Reviewed: <components, or the file scope walked>
Not reviewed: <paths, and why>
```

## References

- `references/hierarchical-mode.md` — the full deep-mode procedure: run-directory setup,
  architecture discovery, attack-surface mapping, component decomposition, per-component
  sub-reviews, cross-component data-flow analysis, and synthesis. Read only when running
  deep mode.
