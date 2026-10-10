# Security Policy

## Supported Versions

This repository is a periodically published snapshot of our private
development codebase (see [CONTRIBUTING.md](./CONTRIBUTING.md) for how that
relationship works). Because of that, we support security fixes on:

| Version | Supported |
|---|---|
| `main` (latest published snapshot) | ✅ |
| Tagged releases (`vX.Y.Z`) | ✅ most recent tag only |
| Older tags | ❌ please upgrade to the latest snapshot or tag |

If you are running a fork or a modified deployment, the same policy applies
to your merge base with `main` — we can't evaluate reports against changes
we can't see.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**
Public issues are indexed and searchable immediately, which gives potential
attackers a head start before a fix ships.

Instead, report privately to:

**privacy@kavanahanalytics.com**

Please include as much of the following as you can:

- A description of the vulnerability and its potential impact
- Steps to reproduce (a minimal proof of concept is ideal)
- The version/commit or tag you tested against
- Any suggested remediation, if you have one

### What to expect

- We aim to **acknowledge your report within 3 business days**.
- We aim to **triage within 10 business days** — we'll let you know whether
  the report is confirmed, needs more information, or is out of scope, and
  give a rough sense of timeline for a fix. We're a small maintainer team,
  so please bear with us if a report lands during a busy stretch.
- We'll credit reporters (if desired) once a fix has shipped, unless you ask
  us not to.
- Please give us a reasonable window to ship a fix before any public
  disclosure. We're a small maintainer team, and coordinated disclosure lets
  us protect self-hosted deployments other than our own.

### Scope

In scope for security reports:

- The web application (the Flask-based management console and its API
  routes, authentication, and authorization logic)
- Data importers (the platform integration/import scripts and their handling
  of credentials and third-party API responses)
- Deployment scripts under `deployment/` (privilege handling, generated
  configuration, secret handling)

Out of scope:

- Vulnerabilities that require access already equivalent to an authenticated
  admin of a deployment
- Issues in third-party services this project integrates with (report those
  to the third party) — unless our integration itself introduces the
  vulnerability (e.g. improper token handling, missing validation)
- Denial-of-service reports that rely purely on resource exhaustion from
  unauthenticated, unthrottled bulk requests against a self-hosted instance
  you fully control (these are configuration/hardening concerns — still
  welcome as an issue, just not a security-disclosure-track report)

### Safe harbour

We consider good-faith security research conducted in line with this policy
to be authorized. That means: only test against instances you own or have
explicit permission to test, avoid privacy violations and service
disruption, and give us the opportunity to fix an issue before disclosing it
publicly. We will not pursue legal action against researchers who follow
these guidelines.

### A note on scope of testing

The maintainers run their own periodic security scans (static analysis,
dependency/CVE scanning, dynamic scanning, secret scanning) against the
private development branch this project is published from, and some
hardening work is tracked privately before it lands here. If you find
something already known to us, we'll say so during triage rather than treat
your report as lower priority — duplicate reports are still useful
confirmation.
