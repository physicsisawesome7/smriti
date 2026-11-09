# Security policy

Smriti holds an organisation's documents and decides who may read them, so we treat security reports as our highest priority.

## Reporting a vulnerability

**Do not open a public GitHub issue for a security problem.**

Use either:

- GitHub's [private vulnerability reporting](https://github.com/physicsisawesome7/smriti/security/advisories/new), or
- email **[lalit@safearly.com](mailto:lalit@safearly.com)** with "SECURITY" in the subject.

Please include:

- the affected version or commit;
- your deployment mode (Docker, pip, demo or token auth);
- steps to reproduce;
- what an attacker gains;
- any proof of concept.

Use synthetic data only. Never send us real personal or official documents.

## What to expect

| Step | Target |
|---|---|
| Acknowledgement | within 3 working days |
| First assessment and severity | within 7 days |
| Fix for **Critical** findings | within 72 hours of confirmation |
| Fix for **High** findings | within 14 days |
| Medium and Low | in the next planned release |

We will tell you when the fix ships and credit you in the advisory and [CHANGELOG.md](CHANGELOG.md), unless you prefer to stay anonymous. Please give us a reasonable time to release a fix before you publish details.

## Supported versions

| Version | Supported |
|---|---|
| 0.2.x | Yes |
| 0.1.x | No: upgrade to 0.2 (see the changelog for behaviour changes) |

## What counts

**In scope:**
- Anything that lets a person read a document, passage, title, count or answer they are not allowed to see. This includes leaks through caches, shared links, channels, agents, tools and the API.
- Identity and authorisation bypass.
- Prompt injection that makes an agent take an action or send data out without the required approval.
- SSRF through connectors or tools.
- Sandbox escape.
- Audit-log tampering that verification does not detect.
- Unmasked personal data where masking is configured.
- Injection or XSS.
- Supply-chain issues in what we ship.

**Out of scope:**
- **Demo mode used on a network.** It is refused off loopback by design and shows a banner.
- **Findings that need administrator or curator rights**, unless they cross a tenant boundary.
- **Volume-only denial of service** against a single-box install.
- **Model quality issues that verification catches.** A model may write a wrong sentence; the answer verifier is there to catch it before the answer is shown. If an unsupported answer *is* shown, that is in scope.

## How Smriti is hardened

The [roadmap](docs/roadmap.md) lists the threat model and the security bar for a government pilot. [CHANGELOG.md](CHANGELOG.md) lists every fix since the last release.

Key controls:
- Two access-enforcement points in retrieval, with a leakage suite in CI.
- An egress guard on every outbound request.
- Sealed credentials.
- Approval gates for agent actions, forced after the agent has read untrusted content.
- An isolated code sandbox.
- A hash-chained audit log.
- Dependencies pinned by hash, and images by digest.
