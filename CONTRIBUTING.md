# Contributing to Kavanah Analytics

Thanks for your interest in contributing. Please read this whole document
before opening a pull request — it'll save you a round trip.

## How this repository works

This repository is a **published, one-way snapshot** of our private,
canonical development repository. Periodically, a maintainer exports the
current state of the private `main` branch, strips anything that shouldn't
be public, and pushes it here as a single squashed commit (or a tagged
release). A few things follow from that:

- **There is no commit history or blame here beyond a given snapshot.** The
  history you see is the export history, not full development history.
- **We do not force-push over community contributions.** Pull requests
  opened against this repo are never silently discarded by a snapshot push;
  a maintainer merges what's mergeable first, or carries it forward by hand.
- **Each snapshot replaces the public tree wholesale.** The publish tool
  overwrites the working tree of public `main` with the exported private
  tree, so anything merged directly into public `main` is superseded by the
  next snapshot unless it has also been ported into the private repository.
  That port is the maintainer's job (see the next point), not yours.
- **Pull requests are welcome, but they are re-applied by hand.** A
  maintainer reviews your PR, then re-applies the change to the private
  repository (as a new commit there, typically squashed and possibly
  adapted to fit surrounding code that isn't visible here). Once that lands
  and is included in the next published snapshot, your PR will show as
  merged or closed here. This means there can be a delay between "looks
  good, approved" and the change actually appearing in a subsequent
  snapshot — that's expected, not a sign your contribution was dropped.
- **Expect your PR to be squashed**, and possibly split or combined with
  other pending work, when it's re-applied privately.

If any of this is a blocker for a particular contribution (e.g. you need
commit-level attribution preserved), open an issue first and we'll figure
out the best path.

## Before you start

For anything beyond a small fix — a new feature, a behavior change, a
refactor — please open an issue first to discuss the approach. This avoids
spending time on a PR that doesn't fit the project's direction.

## Development setup

```bash
git clone https://github.com/Kavanah/kavanah-analytics.git
cd kavanah-analytics

pip install --require-hashes -r bin/python/requirements.txt
pip install -r requirements-test.txt

# Most of the codebase expects the Python package root on PYTHONPATH:
export PYTHONPATH=bin/python

# Run the test suite, excluding tests that need a live Elasticsearch/browser:
pytest -m "not integration and not e2e"
```

Pre-commit hooks (Black, Flake8, and a secrets scan) are available and
recommended:

```bash
pip install pre-commit
pre-commit install
```

For local deployment / running the full stack (Elasticsearch, Kibana, the
web app) to test against a real environment, see
[deployment/README.md](./deployment/README.md).

## Branch and PR flow (for outside contributors)

1. **Fork** the repository.
2. Create a branch off `main` named for what it does
   (`fix/dashboard-export-timeout`, `feat/add-csv-import`).
3. Make your change. Keep **one concern per PR** — a bug fix and an
   unrelated refactor should be two PRs, not one. Smaller PRs review and
   re-apply faster.
4. Use **[Conventional Commits](https://www.conventionalcommits.org/)** for
   your commit messages (`fix:`, `feat:`, `docs:`, `test:`, `chore:`, etc.).
   This isn't just style — it's what lets a maintainer quickly understand
   and re-apply your change by hand.
5. Open the PR against `main` with a clear description of the problem and
   the fix. Reference an existing issue if there is one.

## Changing Python dependencies

`bin/python/requirements.txt` is a **lockfile**, not the file you edit. It is
compiled from `bin/python/requirements.in` and carries an exact version plus
sha256 hashes for every package, so the Docker image and CI install the same
tree and `pip install --require-hashes` refuses anything that drifted. To add,
remove or re-floor a dependency:

```bash
pip install uv                      # once; any recent uv works
cd bin/python
$EDITOR requirements.in             # edit the direct dependencies / floors
uv pip compile requirements.in --output-file requirements.txt \
    --generate-hashes --python-version 3.13 --python-platform linux
```

Commit `requirements.in` and `requirements.txt` together. The `--python-version`
must match the `FROM python:3.13-slim` line in `bin/python/Dockerfile` (a test
pins the two to each other); the lock is compiled for Linux only because that
is the only place it is installed for real. Never hand-edit the `.txt` — a
missing or wrong hash fails the image build and CI rather than installing.
Dependabot regenerates the lock the same way for its weekly bumps.

## Test expectations

Every fix or feature in this project ships with a test exercising it —
that's the standing rule for all changes, including ones from outside
contributors. The pull request template asks for two things in the PR body:

- **`Tested-by: <description or test reference>`** — point at the
  automated test(s) that cover your change. If you added a new test, just
  describe what it exercises; you don't need to know our internal test-ID
  scheme to do this.
- **`Skip-test: <reason>`** — the escape hatch for changes that genuinely
  can't be exercised by an automated test (a subjective UX tweak, something
  that only makes sense to verify manually). Use this sparingly and explain
  why in one line.

If you're not sure how to test something, say so in the PR and a maintainer
will help figure out a test with you rather than blocking on it.

## Identifier discipline

This project has real customers and internal teams behind it, and none of
that should ever end up in the public codebase. When writing code, tests,
fixtures, or example configuration:

- **Never use a real customer name, internal team/mission codename, partner
  or agency name, internal hostname, IP address, or e-mail address** —
  including ones you've seen elsewhere in this codebase or in an issue
  someone filed. If you need an example value, invent one of the same shape
  (e.g. `acme-team`, `example.org`, `admin@example.com`).
- This applies to test fixtures and sample data just as much as production
  code — a "fake" value modeled too closely on a real one (a real domain
  with the TLD swapped, a real name with one letter changed) isn't actually
  safe.
- CI on this repository runs a secret scan (Gitleaks) on every push and
  pull request. Separately, the maintainers run a private identifier scan
  against the whole tree before each snapshot is published; a PR that
  introduces a flagged value will need it replaced before it can be
  re-applied.

## Code style

- **Python**: formatted with [Black](https://github.com/psf/black),
  line length 127. Linted with Flake8 per the rules in `setup.cfg`.
- **JavaScript**: linted with ESLint (see `.eslintrc.json`).
- Pre-commit hooks (see above) run Black and Flake8 automatically on staged
  Python files — running `pre-commit install` once will save you review
  round trips on formatting.

## Contribution terms

Kavanah Analytics is copyright © Kavanah Media and licensed under the
[GNU Affero General Public License v3.0](./LICENSE) (AGPL-3.0-or-later).
Contributions are accepted under the same license.

We use the **Developer Certificate of Origin** (DCO), not a Contributor
License Agreement. By adding a `Signed-off-by:` line to each commit you
certify the statements at <https://developercertificate.org> — in short,
that you wrote the change or otherwise have the right to submit it under
this project's license, and that you understand it is public.

Sign off with the `-s` flag:

```bash
git commit -s -m "fix(import): handle empty page list"
```

which appends a trailer of the form:

```
Signed-off-by: Your Name <you@example.com>
```

The name and address must be real (no pseudonyms) and should match the
commit author. The `DCO` check on every pull request fails when any commit
in the PR lacks the trailer; fix a missed one with `git commit -s --amend`
(or `git rebase --signoff` for several) and force-push your branch.

There is nothing to sign and no copyright assignment: you keep the
copyright to your contribution.

## Documentation contributions

Contributions to the Sphinx user/admin documentation under
`docs/documentation/` follow their own, more detailed guide — see
[docs/documentation/CONTRIBUTING.md](./docs/documentation/CONTRIBUTING.md)
for build instructions, style conventions, and where new pages go.

## Community

- **Discord**: [https://discord.gg/6TE6GbcDax](https://discord.gg/6TE6GbcDax)
  — the best place to ask questions before opening an issue, or to talk
  through a larger change before you start.
- **Security issues**: see [SECURITY.md](./SECURITY.md) — please don't file
  these as public issues or PRs.


Thanks again for contributing!
