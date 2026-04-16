---
name: security-review-codebase
description: "This skill should be used when the user asks to 'security review the codebase', 'audit the codebase for vulnerabilities', 'run a full security audit', 'check the whole project for security issues', or similar requests for a security review scoped to the entire checked-in codebase rather than uncommitted changes."
---

# Security Review: Full Codebase

_Classification: Degraded — parallel execution is optional; sequential fallback preserves all analysis coverage._

Perform a security-focused audit of the entire checked-in codebase to identify HIGH-CONFIDENCE security vulnerabilities with real exploitation potential. This is not a general code review — focus ONLY on concrete security vulnerabilities.

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

## Execution Steps

Run this analysis as parallel work units if supported, otherwise sequentially:

1. **Vulnerability Discovery** — Search the codebase to map the attack surface and produce a candidate list of vulnerabilities with file and line references. Apply the Analysis Methodology above.

2. **False Positive Filter (per finding)** — For each candidate vulnerability, re-read the relevant code and apply the False Positive Filtering rules above. Return a confidence score (0.0–1.0) and a pass/fail verdict. Run these in parallel if supported, otherwise process each finding sequentially.

3. **Synthesis** — Collect all findings that passed filtering with confidence ≥ 0.8. Write the final report.

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
- **LOW**: Defense-in-depth issues or lower-impact vulnerabilities (only include if confidence ≥ 0.8)

If no findings meet the confidence threshold, output:

```
# Security Review: Full Codebase

No high-confidence vulnerabilities found.
```
