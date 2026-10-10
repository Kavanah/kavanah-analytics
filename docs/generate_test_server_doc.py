"""
Generate the Kavanah Analytics Test Server Checks tutorial as a Word document.
Run with: python docs/generate_test_server_doc.py
"""

from docx import Document
from docx.shared import Pt, RGBColor, Inches, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import copy

# ── Colour palette ──────────────────────────────────────────────────────────
DARK_NAVY   = RGBColor(0x1a, 0x23, 0x3a)   # headings / header rows
TEAL        = RGBColor(0x00, 0x86, 0x96)   # accent / sub-headings
AMBER       = RGBColor(0xf5, 0xa6, 0x23)   # "every commit" badge
GREEN       = RGBColor(0x27, 0xae, 0x60)   # "daily cron" badge
PURPLE      = RGBColor(0x6c, 0x3a, 0x8f)   # "pre-PR gate" badge
LIGHT_GREY  = RGBColor(0xf5, 0xf6, 0xfa)   # table row shading
WHITE       = RGBColor(0xff, 0xff, 0xff)
BLACK       = RGBColor(0x00, 0x00, 0x00)
DARK_TEXT   = RGBColor(0x2c, 0x2c, 0x2c)
MID_GREY    = RGBColor(0x6b, 0x72, 0x80)


def set_cell_bg(cell, hex_str):
    """Set table cell background colour via XML (python-docx has no direct API)."""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), hex_str)
    tcPr.append(shd)


def hex_from_rgb(rgb: RGBColor) -> str:
    return f"{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def add_heading(doc, text, level=1, color=DARK_NAVY, space_before=18, space_after=6):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(space_before)
    p.paragraph_format.space_after = Pt(space_after)
    run = p.add_run(text)
    run.bold = True
    run.font.color.rgb = color
    run.font.size = Pt({1: 20, 2: 15, 3: 12.5}.get(level, 11))
    return p


def add_body(doc, text, italic=False, color=DARK_TEXT, size=10.5, space_after=4):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.space_before = Pt(0)
    run = p.add_run(text)
    run.font.size = Pt(size)
    run.font.color.rgb = color
    run.italic = italic
    return p


def add_badge_paragraph(doc, badge_text, badge_rgb, body_text):
    """One paragraph: coloured [BADGE] followed by body text."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(3)
    badge_run = p.add_run(f"  {badge_text}  ")
    badge_run.bold = True
    badge_run.font.size = Pt(9)
    badge_run.font.color.rgb = WHITE
    # Background highlight isn't natively supported inline; we'll use font colour instead
    # and show the badge in the badge colour as a workaround
    badge_run.font.color.rgb = badge_rgb
    badge_run.bold = True
    spacer = p.add_run("  ")
    spacer.font.size = Pt(10.5)
    body_run = p.add_run(body_text)
    body_run.font.size = Pt(10.5)
    body_run.font.color.rgb = DARK_TEXT
    return p


def build_summary_table(doc, rows_data, col_widths, headers, header_bg="1A233A"):
    """
    rows_data: list of lists of strings.
    col_widths: list of Inches values.
    """
    table = doc.add_table(rows=1 + len(rows_data), cols=len(headers))
    table.style = 'Table Grid'
    table.alignment = WD_TABLE_ALIGNMENT.LEFT

    # Header row
    hdr_row = table.rows[0]
    for i, (cell, hdr) in enumerate(zip(hdr_row.cells, headers)):
        set_cell_bg(cell, header_bg)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        run = p.add_run(hdr)
        run.bold = True
        run.font.color.rgb = WHITE
        run.font.size = Pt(9.5)

    # Data rows
    for r_idx, row_data in enumerate(rows_data):
        row = table.rows[r_idx + 1]
        bg = "F5F6FA" if r_idx % 2 == 0 else "FFFFFF"
        for c_idx, (cell, value) in enumerate(zip(row.cells, row_data)):
            set_cell_bg(cell, bg)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            run = p.add_run(str(value))
            run.font.size = Pt(9)
            run.font.color.rgb = DARK_TEXT

    # Column widths
    for row in table.rows:
        for i, cell in enumerate(row.cells):
            cell.width = col_widths[i]

    return table


def add_spacer(doc, lines=1):
    for _ in range(lines):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        run = p.add_run("")
        run.font.size = Pt(4)


# ─────────────────────────────────────────────────────────────────────────────
# CHECK DATA
# ─────────────────────────────────────────────────────────────────────────────

checks = {
    # ── Code Quality ──────────────────────────────────────────────────────────
    "flake8": {
        "category": "Code Quality",
        "full_name": "flake8 — Python Style Linter",
        "triggers": ["Every commit (pre-commit hook / CI)"],
        "frequency": "Every commit",
        "mechanism": "GitHub Actions (pre-commit hook + CI step)",
        "runtime": "< 30 seconds",
        "what": (
            "flake8 is a Python linting tool that checks source code against PEP 8 — "
            "Python's official style guide. It catches formatting issues such as lines "
            "that are too long, incorrect indentation, unused imports, missing whitespace "
            "around operators, and hundreds of other style rules. It does NOT execute the "
            "code — it only reads it."
        ),
        "how": (
            "flake8 is installed as a development dependency (requirements-dev.txt or "
            "pyproject.toml). In the CI pipeline it runs as a GitHub Actions step:\n"
            "    flake8 . --max-line-length=120 --exclude=.venv,migrations\n"
            "If any file violates a rule, flake8 exits with a non-zero code, which "
            "causes the GitHub Actions job to fail and blocks the commit from merging."
        ),
        "why": (
            "Consistent code style reduces cognitive load during code review. A shared "
            "style means engineers focus on logic, not formatting debates."
        ),
        "blocks": "Yes — commit / PR fails immediately if violations are found.",
    },
    "black": {
        "category": "Code Quality",
        "full_name": "Black — Python Auto-Formatter",
        "triggers": ["Every commit (pre-commit hook / CI)"],
        "frequency": "Every commit",
        "mechanism": "GitHub Actions (pre-commit hook + CI step)",
        "runtime": "< 20 seconds",
        "what": (
            "Black is an opinionated Python code formatter. Unlike flake8 (which reports "
            "violations), Black actually rewrites files to a canonical format — consistent "
            "quotes, line length, trailing commas, and spacing. In CI it is run in "
            "--check mode, meaning it reports differences without modifying files."
        ),
        "how": (
            "Installed as a dev dependency. CI runs:\n"
            "    black --check --diff .\n"
            "If any file would be changed by Black, the job fails. Developers are "
            "expected to run Black locally before pushing (often via a pre-commit hook "
            "or IDE integration), so CI is a safety net rather than the primary enforcement."
        ),
        "why": (
            "Black eliminates all style debates by being non-configurable. Teams stop "
            "arguing about quote style or line length and focus on business logic."
        ),
        "blocks": "Yes — commit / PR fails if any file is not Black-compliant.",
    },
    "eslint": {
        "category": "Code Quality",
        "full_name": "ESLint — JavaScript Linter",
        "triggers": ["Every commit (CI)"],
        "frequency": "Every commit",
        "mechanism": "GitHub Actions CI step",
        "runtime": "< 1 minute",
        "what": (
            "ESLint analyses JavaScript (and TypeScript) source files for syntax errors, "
            "likely bugs, anti-patterns, and style violations. It is highly configurable "
            "via an .eslintrc file and can enforce team-specific rules beyond the defaults."
        ),
        "how": (
            "ESLint is installed as an npm dev dependency. The CI step runs:\n"
            "    npx eslint . --ext .js,.jsx,.ts,.tsx\n"
            "Any error-level rule violation causes a non-zero exit and fails the job. "
            "Warning-level violations are reported but do not block."
        ),
        "why": (
            "JavaScript has many footguns — implicit globals, type coercion bugs, "
            "undefined variable references — that ESLint catches statically before "
            "the code runs in a browser."
        ),
        "blocks": "Yes — commit / PR fails on error-level violations.",
    },
    # ── Security ──────────────────────────────────────────────────────────────
    "gitleaks": {
        "category": "Security",
        "full_name": "Gitleaks — Secret Scanner",
        "triggers": ["Every commit (CI)"],
        "frequency": "Every commit",
        "mechanism": "GitHub Actions CI step",
        "runtime": "< 1 minute",
        "what": (
            "Gitleaks scans the git history and staged files for accidentally committed "
            "secrets: API keys, OAuth tokens, AWS credentials, private keys, passwords, "
            "and other sensitive strings. It uses a library of regex patterns tuned for "
            "hundreds of common secret formats."
        ),
        "how": (
            "Gitleaks runs as a GitHub Actions step using the official action:\n"
            "    uses: gitleaks/gitleaks-action@v2\n"
            "It scans the full commit diff on every push. If a secret pattern is detected "
            "in any new or modified file, the job fails immediately and the workflow "
            "output shows exactly which file and line triggered the alert."
        ),
        "why": (
            "Accidentally committing secrets is one of the most common and costly security "
            "incidents. Gitleaks acts as a last line of defence before bad commits reach "
            "the shared repository."
        ),
        "blocks": "Yes — commit / PR fails if any secret is detected.",
    },
    "trivy_daily": {
        "category": "Security",
        "full_name": "Trivy — Container CVE Scanner (Daily)",
        "triggers": ["Daily cron job"],
        "frequency": "Daily (scheduled)",
        "mechanism": "GitHub Actions scheduled workflow (cron)",
        "runtime": "5–15 minutes",
        "what": (
            "Trivy scans Docker container images for known CVEs (Common Vulnerabilities "
            "and Exposures) in OS packages, language runtime libraries, and application "
            "dependencies. It queries the NVD (National Vulnerability Database) and "
            "vendor-specific advisories."
        ),
        "how": (
            "A GitHub Actions workflow is triggered on a cron schedule (e.g. 0 3 * * * — "
            "3 AM UTC daily). It:\n"
            "1. Builds (or pulls) the current Docker images\n"
            "2. Runs: trivy image --severity HIGH,CRITICAL <image-name>\n"
            "3. Outputs a report; failures are flagged (alert sent via SNS/email)\n\n"
            "The daily run catches newly published CVEs in dependencies that have not "
            "changed in the codebase — i.e., yesterday's image may be vulnerable today."
        ),
        "why": (
            "New vulnerabilities are published continuously. A one-time scan on build is "
            "not enough — the daily scan ensures the team is alerted when the threat "
            "landscape changes, even with no new code."
        ),
        "blocks": "No direct block — alerts the team; action is manual.",
    },
    "trivy_prepr": {
        "category": "Security",
        "full_name": "Trivy — Container CVE Scanner (Pre-PR)",
        "triggers": ["Pre-PR gate (main branch)"],
        "frequency": "On PR open/update targeting main",
        "mechanism": "GitHub Actions CI workflow (event-triggered)",
        "runtime": "5–15 minutes",
        "what": (
            "Same Trivy scan as the daily job, but run specifically on the image built "
            "from the PR branch. This ensures no PR introduces a new high/critical CVE "
            "before it is merged to main."
        ),
        "how": (
            "Runs inside the pre-PR gate GitHub Actions workflow, triggered by "
            "pull_request events targeting main. The step fails the workflow if any "
            "HIGH or CRITICAL CVE is found. Developers must resolve or suppress "
            "(with justification) the finding before the PR can be merged."
        ),
        "why": "Gate to prevent shipping a known-vulnerable image to staging/production.",
        "blocks": "Yes — PR merge blocked until findings are resolved.",
    },
    "semgrep": {
        "category": "Security",
        "full_name": "Semgrep — Static Application Security Testing (SAST)",
        "triggers": ["Pre-PR gate (main branch)"],
        "frequency": "On PR open/update targeting main",
        "mechanism": "GitHub Actions CI workflow",
        "runtime": "2–10 minutes",
        "what": (
            "Semgrep performs SAST — it reads source code and looks for insecure coding "
            "patterns without executing the code. It can detect SQL injection, XSS "
            "vulnerabilities, insecure deserialization, hardcoded credentials in logic, "
            "use of deprecated/unsafe functions, and more. It uses a large library of "
            "community and Semgrep-maintained rules as well as custom rules."
        ),
        "how": (
            "Runs via the official Semgrep GitHub Action:\n"
            "    uses: semgrep/semgrep-action@v1\n"
            "      with:\n"
            "        config: p/python p/flask p/owasp-top-ten\n"
            "Semgrep analyses the diff or full codebase and reports rule matches with "
            "severity levels. High-severity findings fail the workflow."
        ),
        "why": (
            "Unit and integration tests validate behaviour but don't systematically "
            "check for security anti-patterns. Semgrep provides a scalable, automated "
            "security review that runs before every merge."
        ),
        "blocks": "Yes — PR merge blocked on high-severity findings.",
    },
    "owasp_zap": {
        "category": "Security",
        "full_name": "OWASP ZAP — Web Application Vulnerability Scanner",
        "triggers": ["Pre-PR gate (main branch)"],
        "frequency": "On PR open/update targeting main",
        "mechanism": "GitHub Actions CI workflow",
        "runtime": "10–20 minutes",
        "what": (
            "OWASP ZAP (Zed Attack Proxy) is a dynamic application security testing "
            "(DAST) tool. Unlike Semgrep (which reads code), ZAP actually makes HTTP "
            "requests to a running instance of the application and probes for "
            "vulnerabilities: XSS, SQL injection, insecure headers, open redirects, "
            "CSRF weaknesses, exposed admin endpoints, and more. It simulates what an "
            "attacker would do against a live system."
        ),
        "how": (
            "The pre-PR workflow:\n"
            "1. Builds and starts the application in a Docker container\n"
            "2. Runs the ZAP baseline scan via:\n"
            "       uses: zaproxy/action-baseline@v0.10.0\n"
            "         with:\n"
            "           target: 'http://localhost:8080'\n"
            "3. ZAP crawls the app and fires a battery of passive + active checks\n"
            "4. Produces an HTML/JSON report; high/critical alerts fail the workflow."
        ),
        "why": (
            "Static analysis (Semgrep) finds code-level vulnerabilities. ZAP finds "
            "runtime vulnerabilities that only appear when the app is actually running "
            "and responding to requests — a fundamentally different and complementary "
            "attack surface."
        ),
        "blocks": "Yes — PR merge blocked on high/critical alert findings.",
    },
    # ── Testing ───────────────────────────────────────────────────────────────
    "pytest_daily": {
        "category": "Testing",
        "full_name": "pytest-flask + ES Containers — Integration Tests (Daily)",
        "triggers": ["Daily cron job"],
        "frequency": "Daily (scheduled)",
        "mechanism": "GitHub Actions scheduled workflow (cron)",
        "runtime": "5–20 minutes",
        "what": (
            "pytest-flask is a pytest plugin that provides fixtures for testing Flask "
            "applications (test client, request context, etc.). The integration tests "
            "spin up real Elasticsearch containers alongside the Flask app and run tests "
            "that cover full request-response cycles, database reads/writes, and import "
            "pipeline logic against a real ES instance — not mocks."
        ),
        "how": (
            "The scheduled GitHub Actions workflow:\n"
            "1. Starts an Elasticsearch Docker container via docker-compose or "
            "   services: in the workflow YAML\n"
            "2. Runs: pytest tests/ --tb=short -q\n"
            "3. Failures are reported; the team is alerted\n\n"
            "Running daily catches environment drift — e.g., a new ES version, expired "
            "tokens, or a changed external API that breaks the tests even though no code "
            "changed."
        ),
        "why": (
            "CI on commit catches regressions from code changes. Daily CI catches "
            "regressions from environmental changes (dependency updates, infrastructure "
            "changes, time-dependent logic like token expiry)."
        ),
        "blocks": "No direct block — alerts team; action is manual.",
    },
    "pytest_prepr": {
        "category": "Testing",
        "full_name": "pytest-flask + ES Containers — Integration Tests (Pre-PR)",
        "triggers": ["Pre-PR gate (main branch)"],
        "frequency": "On PR open/update targeting main",
        "mechanism": "GitHub Actions CI workflow",
        "runtime": "5–20 minutes",
        "what": "Same integration test suite as the daily run (see above).",
        "how": (
            "Runs inside the pre-PR gate workflow. The Elasticsearch test container is "
            "declared in the workflow's services: block so it is automatically started "
            "before tests run and torn down after. pytest must exit 0 for the workflow "
            "to pass."
        ),
        "why": "Hard gate — no code reaches main without passing the full integration suite.",
        "blocks": "Yes — PR merge blocked until all tests pass.",
    },
    "docker_daily": {
        "category": "Build & Infrastructure",
        "full_name": "Docker Image Build (Daily)",
        "triggers": ["Daily cron job"],
        "frequency": "Daily (scheduled)",
        "mechanism": "GitHub Actions scheduled workflow (cron)",
        "runtime": "3–10 minutes",
        "what": (
            "Builds all Docker images (Flask app, pipeline containers) from the current "
            "Dockerfiles and verifies the build completes without errors. Does not push "
            "to a registry — it is a build validation only."
        ),
        "how": (
            "The scheduled workflow runs:\n"
            "    docker build -t deam-app:test .\n"
            "for each service. A failure indicates a broken Dockerfile, a missing "
            "dependency, or a package that is no longer available in the package "
            "registry — all of which would block a production deploy."
        ),
        "why": (
            "Docker build failures caused by upstream package changes (e.g., a Python "
            "package yanked from PyPI) need to be caught before a deploy is attempted, "
            "not during one."
        ),
        "blocks": "No direct block — alerts team.",
    },
    "docker_prepr": {
        "category": "Build & Infrastructure",
        "full_name": "Docker Image Build (Pre-PR)",
        "triggers": ["Pre-PR gate (main branch)"],
        "frequency": "On PR open/update targeting main",
        "mechanism": "GitHub Actions CI workflow",
        "runtime": "3–10 minutes",
        "what": "Same build validation as the daily job (see above).",
        "how": (
            "First step in the pre-PR gate workflow. Subsequent steps (pytest, ZAP, etc.) "
            "only run if the build succeeds — there is no point running tests against an "
            "image that cannot be built."
        ),
        "why": "Verifies the PR branch's changes don't break the build pipeline.",
        "blocks": "Yes — all subsequent pre-PR steps are skipped if build fails.",
    },
    # ── E2E & Data Integrity ──────────────────────────────────────────────────
    "playwright": {
        "category": "End-to-End Testing",
        "full_name": "Playwright — End-to-End UI Tests",
        "triggers": ["Pre-PR gate (main branch)"],
        "frequency": "On PR open/update targeting main",
        "mechanism": "GitHub Actions CI workflow",
        "runtime": "10–30 minutes",
        "what": (
            "Playwright is a browser automation framework that controls real browsers "
            "(Chromium, Firefox, WebKit) to simulate user interactions with the web "
            "application. E2E tests written in Playwright click buttons, fill forms, "
            "navigate between pages, and assert on what the user sees — verifying the "
            "full stack works together end-to-end."
        ),
        "how": (
            "The pre-PR workflow:\n"
            "1. Starts the full application stack (Flask, Elasticsearch, Nginx) via "
            "   docker-compose\n"
            "2. Installs Playwright browsers: npx playwright install --with-deps\n"
            "3. Runs: npx playwright test\n"
            "4. Uploads test results and screenshots as workflow artefacts\n\n"
            "Tests cover login, asset CRUD, dashboard builder flows, import triggers, "
            "and other critical UI paths."
        ),
        "why": (
            "Integration tests verify server-side logic; Playwright verifies the browser "
            "experience. A server can return HTTP 200 while the UI is completely broken "
            "due to a JavaScript error or a missing template variable."
        ),
        "blocks": "Yes — PR merge blocked if any Playwright test fails.",
    },
    "schema": {
        "category": "End-to-End Testing",
        "full_name": "Schema Comparison — VM vs. Production Index Parity",
        "triggers": ["Pre-PR gate (main branch)"],
        "frequency": "On PR open/update targeting main",
        "mechanism": "GitHub Actions CI workflow (custom script)",
        "runtime": "2–5 minutes",
        "what": (
            "A custom script compares the Elasticsearch index mappings on the test VM "
            "against those on the production server. If a migration was applied in "
            "production but not in the test environment (or vice versa), the schema "
            "comparison will detect the divergence."
        ),
        "how": (
            "The script calls the Elasticsearch _mapping API on both environments:\n"
            "    GET https://test-server:9200/<index>/_mapping\n"
            "    GET https://prod-server:9200/<index>/_mapping\n"
            "It diffs the JSON structures and fails the workflow if any field types, "
            "index names, or mapping settings differ. Credentials are stored in "
            "GitHub Actions secrets."
        ),
        "why": (
            "Tests passing on the test server with a different schema than production "
            "gives a false sense of safety. A field that is a keyword in production but "
            "text in the test environment will cause query failures that tests never catch."
        ),
        "blocks": "Yes — PR merge blocked on schema divergence.",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# BUILD DOCUMENT
# ─────────────────────────────────────────────────────────────────────────────

def build_doc():
    doc = Document()

    # Page margins
    for section in doc.sections:
        section.top_margin    = Cm(2.2)
        section.bottom_margin = Cm(2.2)
        section.left_margin   = Cm(2.5)
        section.right_margin  = Cm(2.5)

    # Default paragraph font
    style = doc.styles['Normal']
    style.font.name = 'Calibri'
    style.font.size = Pt(10.5)

    # ── Title ─────────────────────────────────────────────────────────────────
    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_p.paragraph_format.space_before = Pt(0)
    title_p.paragraph_format.space_after  = Pt(6)
    title_run = title_p.add_run("Kavanah Analytics — Test Server Checks")
    title_run.bold = True
    title_run.font.size = Pt(24)
    title_run.font.color.rgb = DARK_NAVY

    sub_p = doc.add_paragraph()
    sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub_p.paragraph_format.space_before = Pt(0)
    sub_p.paragraph_format.space_after  = Pt(20)
    sub_run = sub_p.add_run("Developer Tutorial — What Runs, When, and Why")
    sub_run.font.size = Pt(12)
    sub_run.font.color.rgb = MID_GREY
    sub_run.italic = True

    # ── Introduction ──────────────────────────────────────────────────────────
    add_heading(doc, "Overview", level=1, space_before=0)
    add_body(doc, (
        "The test server sits between a developer's local machine and the staging "
        "environment. Its job is to catch problems early — before they reach a server "
        "that other people are using. Checks run at three distinct points in the "
        "workflow, each with a different purpose:"
    ))
    add_spacer(doc)

    overview_data = [
        ["Every Commit", "Fast feedback (< 2 min)", "Blocks the commit/push if any check fails",
         "GitHub Actions (event-triggered)"],
        ["Daily (Cron)", "Background health monitoring (~20 min)", "Alerts the team; no hard block",
         "GitHub Actions (scheduled cron)"],
        ["Pre-PR Gate\n(main only)", "Full quality gate (~20–30 min)", "PR cannot be merged until all checks pass",
         "GitHub Actions (event-triggered)"],
    ]
    build_summary_table(
        doc,
        overview_data,
        col_widths=[Inches(1.4), Inches(2.1), Inches(2.1), Inches(2.1)],
        headers=["Trigger", "Purpose", "Effect", "Mechanism"],
    )
    add_spacer(doc)
    add_body(doc, (
        "All checks run inside GitHub Actions workflows — either triggered by a git push/PR event "
        "or by a scheduled cron expression. There is no separate cron daemon on the server; "
        "GitHub's infrastructure handles the scheduling."
    ), italic=True, color=MID_GREY)

    # ── Master Summary Table ───────────────────────────────────────────────────
    add_heading(doc, "Master Summary Table", level=1)
    add_body(doc, "Every check at a glance — sorted by category, then trigger frequency.")
    add_spacer(doc)

    master_rows = []
    for key, c in checks.items():
        trigger_short = (
            "Every commit" if "Every commit" in c["frequency"]
            else "Daily (cron)" if "Daily" in c["frequency"]
            else "Pre-PR (main)"
        )
        master_rows.append([
            c["category"],
            c["full_name"].split(" — ")[0],
            trigger_short,
            c["mechanism"].split("(")[0].strip(),
            c["runtime"],
            c["blocks"],
        ])

    # Sort by category then trigger
    order = {"Code Quality": 0, "Security": 1, "Testing": 2,
             "Build & Infrastructure": 3, "End-to-End Testing": 4}
    trig_order = {"Every commit": 0, "Daily (cron)": 1, "Pre-PR (main)": 2}
    master_rows.sort(key=lambda r: (order.get(r[0], 9), trig_order.get(r[2], 9)))

    build_summary_table(
        doc,
        master_rows,
        col_widths=[Inches(1.3), Inches(1.9), Inches(1.1), Inches(1.5), Inches(0.9), Inches(0.95)],
        headers=["Category", "Tool", "When", "Mechanism", "Runtime", "Blocks?"],
    )

    # ── Cron vs Event section ──────────────────────────────────────────────────
    add_heading(doc, "Cron Jobs vs. Event-Triggered Checks", level=1)
    add_body(doc, (
        "A common source of confusion is the difference between checks that run because "
        "something happened (a push, a PR) and checks that run on a schedule regardless "
        "of activity. Both use GitHub Actions — the difference is in how the workflow is "
        "triggered."
    ))
    add_spacer(doc)

    add_heading(doc, "Event-Triggered (on: push / pull_request)", level=2, color=TEAL, space_before=8)
    add_body(doc, (
        "These workflows start the moment a developer pushes code or opens/updates a PR. "
        "GitHub detects the git event, queues the workflow, and a runner executes it "
        "within seconds. The developer sees the result before they can do anything else. "
        "If the check fails, the commit is blocked (pre-commit hook) or the PR is "
        "marked as failing."
    ))
    add_body(doc, "Example workflow trigger:", italic=True, color=MID_GREY)
    add_body(doc, "    on:\n      push:\n        branches: [dev, main]\n      pull_request:\n        branches: [main]",
             color=RGBColor(0x2d, 0x6a, 0x4f))

    add_spacer(doc)
    add_heading(doc, "Scheduled / Cron (on: schedule)", level=2, color=TEAL, space_before=8)
    add_body(doc, (
        "These workflows run at a fixed time regardless of whether any code changed. "
        "GitHub Actions uses standard cron syntax. For example, '0 3 * * *' means "
        "'run at 03:00 UTC every day'. The workflow wakes up, runs the checks, and "
        "sends alerts if anything fails. No developer action is required to trigger it."
    ))
    add_body(doc, "Example workflow trigger:", italic=True, color=MID_GREY)
    add_body(doc, "    on:\n      schedule:\n        - cron: '0 3 * * *'   # 3 AM UTC daily",
             color=RGBColor(0x2d, 0x6a, 0x4f))

    add_spacer(doc)
    cron_vs_event = [
        ["flake8", "Event (push/PR)", "Immediate feedback on every push"],
        ["Black", "Event (push/PR)", "Immediate feedback on every push"],
        ["ESLint", "Event (push/PR)", "Immediate feedback on every push"],
        ["Gitleaks", "Event (push/PR)", "Catches secrets before they spread"],
        ["Docker Build", "Both", "Event on PR; also daily to catch upstream breakage"],
        ["pytest + ES", "Both", "Event on PR; also daily to catch environmental drift"],
        ["Trivy", "Both", "Event on PR; also daily for newly published CVEs"],
        ["Semgrep", "Event (PR → main)", "Pre-PR gate only"],
        ["OWASP ZAP", "Event (PR → main)", "Pre-PR gate only — requires running app"],
        ["Playwright", "Event (PR → main)", "Pre-PR gate only — requires full stack"],
        ["Schema Check", "Event (PR → main)", "Pre-PR gate only — compares live environments"],
    ]
    build_summary_table(
        doc,
        cron_vs_event,
        col_widths=[Inches(1.5), Inches(1.8), Inches(4.35)],
        headers=["Tool", "Trigger Type", "Rationale"],
    )

    # ── Categories ────────────────────────────────────────────────────────────
    categories = {
        "Code Quality": {
            "color": TEAL,
            "intro": (
                "Code quality checks enforce consistent, readable code. They run on every "
                "commit so that style issues are caught immediately — before code review, "
                "before testing, and before merging. They are the fastest checks in the "
                "pipeline (typically < 1 minute combined)."
            ),
            "keys": ["flake8", "black", "eslint"],
        },
        "Security": {
            "color": RGBColor(0xc0, 0x39, 0x2b),
            "intro": (
                "Security checks look for vulnerabilities at three levels: secrets in the "
                "git history (Gitleaks), known CVEs in dependencies (Trivy), insecure code "
                "patterns (Semgrep), and live runtime vulnerabilities (OWASP ZAP). They "
                "span all three trigger points — commit, daily, and pre-PR — because "
                "different attack surfaces require different scanning approaches."
            ),
            "keys": ["gitleaks", "trivy_daily", "trivy_prepr", "semgrep", "owasp_zap"],
        },
        "Testing": {
            "color": GREEN,
            "intro": (
                "Integration tests verify that the application logic works correctly against "
                "real infrastructure (a real Elasticsearch instance, not mocks). pytest-flask "
                "provides Flask-specific test fixtures. Tests run both on a schedule and as a "
                "hard gate before merging to main."
            ),
            "keys": ["pytest_daily", "pytest_prepr"],
        },
        "Build & Infrastructure": {
            "color": RGBColor(0xe6, 0x7e, 0x22),
            "intro": (
                "Docker image builds confirm the application packages cleanly into its "
                "deployment artefact. Build failures caused by upstream changes (deleted "
                "packages, changed base images) are caught daily, not at deploy time."
            ),
            "keys": ["docker_daily", "docker_prepr"],
        },
        "End-to-End Testing": {
            "color": PURPLE,
            "intro": (
                "E2E checks test the system as a whole — from browser interactions down to "
                "the database. Playwright tests the UI as a real user would. The schema "
                "comparison checks that the test environment actually mirrors production."
            ),
            "keys": ["playwright", "schema"],
        },
    }

    for cat_name, cat_info in categories.items():
        add_heading(doc, cat_name, level=1, color=cat_info["color"])
        add_body(doc, cat_info["intro"])
        add_spacer(doc)

        for key in cat_info["keys"]:
            c = checks[key]
            # Sub-heading: tool name
            add_heading(doc, c["full_name"], level=2, color=DARK_NAVY, space_before=14, space_after=3)

            # Frequency / mechanism row
            meta_p = doc.add_paragraph()
            meta_p.paragraph_format.space_before = Pt(0)
            meta_p.paragraph_format.space_after  = Pt(6)

            def meta_kv(p, key_text, val_text, key_color, spacer=True):
                kr = p.add_run(key_text + ": ")
                kr.bold = True
                kr.font.size = Pt(9.5)
                kr.font.color.rgb = key_color
                vr = p.add_run(val_text)
                vr.font.size = Pt(9.5)
                vr.font.color.rgb = DARK_TEXT
                if spacer:
                    sr = p.add_run("    ")
                    sr.font.size = Pt(9.5)

            meta_kv(meta_p, "Runs", c["frequency"], TEAL)
            meta_kv(meta_p, "Via", c["mechanism"], TEAL)
            meta_kv(meta_p, "Runtime", c["runtime"], TEAL)
            meta_kv(meta_p, "Blocks merge?", c["blocks"], RGBColor(0xc0, 0x39, 0x2b), spacer=False)

            # What / How / Why
            for label, body_key in [("What it does", "what"), ("How it is implemented", "how"),
                                     ("Why it exists", "why")]:
                lp = doc.add_paragraph()
                lp.paragraph_format.space_before = Pt(5)
                lp.paragraph_format.space_after  = Pt(2)
                lr = lp.add_run(label + ": ")
                lr.bold = True
                lr.font.size = Pt(10.5)
                lr.font.color.rgb = DARK_NAVY
                br = lp.add_run(c[body_key])
                br.font.size = Pt(10.5)
                br.font.color.rgb = DARK_TEXT

            add_spacer(doc)

    # ── Footer note ───────────────────────────────────────────────────────────
    add_heading(doc, "Further Reading", level=1, space_before=20)
    refs = [
        ("flake8 documentation", "https://flake8.pycqa.org"),
        ("Black documentation", "https://black.readthedocs.io"),
        ("ESLint documentation", "https://eslint.org/docs"),
        ("Gitleaks GitHub", "https://github.com/gitleaks/gitleaks"),
        ("Trivy documentation", "https://trivy.dev"),
        ("Semgrep documentation", "https://semgrep.dev/docs"),
        ("OWASP ZAP documentation", "https://www.zaproxy.org/docs"),
        ("Playwright documentation", "https://playwright.dev/docs/intro"),
        ("GitHub Actions — scheduled workflows",
         "https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows#schedule"),
    ]
    for name, url in refs:
        p = doc.add_paragraph(style='List Bullet')
        p.paragraph_format.space_before = Pt(1)
        p.paragraph_format.space_after  = Pt(1)
        nr = p.add_run(name + " — ")
        nr.font.size = Pt(10)
        nr.font.color.rgb = DARK_TEXT
        ur = p.add_run(url)
        ur.font.size = Pt(10)
        ur.font.color.rgb = TEAL

    out_path = "docs/Kavanah_Analytics_Test_Server_Checks.docx"
    doc.save(out_path)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    build_doc()
