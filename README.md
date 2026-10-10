# Kavanah Analytics

A data aggregation and management system for digital marketing campaigns. Integrates data from Facebook Ads, Google Ads, YouTube, Instagram, Google Analytics, Echo messaging, Chatwoot, and Disciple Tools into Elasticsearch for visualization through custom dashboards.

Kavanah Analytics is for teams who run digital marketing/engagement campaigns across multiple advertising and messaging platforms and want one self-hosted place to aggregate that data, build custom dashboards on top of it, and query it with natural language — without handing the underlying data to a third-party SaaS analytics vendor. It's built around Elasticsearch and Kibana, and ships as a Dockerized stack you deploy on your own infrastructure.

## Credits

**Kavanah Analytics** was designed and built by **John Mann**, **Joshua Mann**, **Allen Messimer**, and **Jon Ralls**.

Special thanks to **Glenn Ansley** for creating the original Digital Engagement Dashboard project, upon which Kavanah Analytics is based.


## Tech Stack

| Category | Technologies |
|----------|-------------|
| Runtime | Python 3.13 |
| Database | Elasticsearch 9.3 |
| Visualization | Kibana 9.3, ECharts, Leaflet |
| Web | Flask, Gunicorn, OpenResty (nginx) |
| Containerization | Docker, Docker Compose |
| APIs | facebook-business SDK, google-ads SDK, Anthropic |
| CI/CD | GitHub Actions (hosted runners) |
| Security | OWASP ZAP, Trivy, Semgrep, Gitleaks |

## Architecture

```
                    ┌─────────────────────────────────────┐
                    │         nginx reverse proxy          │
                    │       (HTTPS, SSL termination)       │
                    └──────┬──────────┬──────────┬────────┘
                           │          │          │
              ┌────────────▼──┐  ┌────▼────┐  ┌──▼───────────────┐
              │  Flask DEAM   │  │ Kibana  │  │  FB OAuth Server │
              │  (port 8084)  │  │ (5601)  │  │    (port 8083)   │
              └───────┬───────┘  └────┬────┘  └──────────────────┘
                      │               │
                      └───────┬───────┘
                              │
                    ┌─────────▼─────────┐
                    │   Elasticsearch   │
                    │    (port 9200)    │
                    └───────────────────┘
```

**Docker Containers:**

| Container | Purpose |
|-----------|---------|
| `es01` | Elasticsearch database |
| `kib01` | Kibana web interface |
| `imports_container` | Python runtime: asset management (8084), OAuth server (8083), password reset (8085) |
| `nginx-reverse-proxy` | HTTPS reverse proxy |

## Quick Start

### Prerequisites

- Ubuntu 24.04 LTS with SSH access and passwordless sudo
- Git
- Domain name with DNS A record (for production/staging)

### Installation

```bash
git clone https://github.com/Kavanah/kavanah-analytics.git
cd kavanah-analytics

# Step 1-2: Base system (Docker, Python 3, Git)
bash deployment/step1-and-2-base-setup.sh

# Step 3: Application stack (Elasticsearch, Kibana, Flask apps)
bash deployment/step3-app-stack.sh

# Step 4: Seed test data (optional)
bash deployment/step4-seed-test-data.sh

# Step 5: Testing tools (optional — pytest, Playwright, Locust, ZAP, Trivy)
bash deployment/step5-testing-deps.sh

# Step 6: nginx reverse proxy (requires domain + SSL certs)
bash deployment/step6-nginx-setup.sh
```

All scripts are idempotent and skip already-completed steps.

### Configuration

Step 3 generates two configuration files:

**`.env.test`** — Environment variables (gitignored):
```
ENC_KEY=<random-base64>
FLASK_SECRET_KEY=<random-hex>
ES01_PASSWORD=<generated>
```

**`config/master-config.yaml`** — Credentials and server settings:
```yaml
server:
  ip_address: "<your-server-ip>"
  admin_email: "admin@example.com"
reporting:
  hostname: "your-domain.com"   # Set before step 6
```

### Post-Install

1. Import Kibana saved objects: `bash deployment/kibana-import.sh`
2. Create data views: `bash deployment/create-data-views.sh`
3. Access the management console at `https://your-domain.com/assets/`

For the full set of deployment scripts (day-2 operations, rollback, health
checks) and more detail on each step above, see
[deployment/README.md](./deployment/README.md).

## Project Structure

```
kavanah-analytics/
├── bin/python/                     # Python application code
│   ├── asset_management/           # Flask web app (DEAM)
│   │   └── web/
│   │       ├── app.py              # Main Flask application
│   │       ├── blueprints/         # Dashboard builder, data explorer
│   │       ├── services/           # Business logic services
│   │       ├── templates/          # Jinja2 HTML templates
│   │       └── static/             # JS, CSS, GeoJSON assets
│   ├── import_scripts/             # Data import modules
│   │   ├── facebook/               # Facebook Ads insights
│   │   ├── google_ads/             # Google Ads performance
│   │   ├── echo/                   # Echo messaging
│   │   └── chatwoot/               # Chatwoot conversations
│   ├── lib/                        # Shared utilities (ES helpers, encryption)
│   ├── fb_oauth_server.py          # Facebook/Google OAuth server
│   ├── Dockerfile                  # Container image definition
│   └── start_web_apps.sh           # Container entrypoint
├── deployment/                     # Installation and setup scripts
├── config/                         # YAML configuration (gitignored)
├── tests/                          # pytest suite (over 9,000 tests)
│   ├── flask/                      # Web app unit tests
│   ├── services/                   # Service-layer unit tests
│   ├── lib/                        # Shared-library unit tests
│   ├── pipelines/                  # Importer pipeline tests
│   ├── migration/                  # ES data-migration tests
│   ├── deployment/                 # Deployment script tests
│   ├── security/                   # Security regression tests
│   ├── release_plans/              # Release-plan acceptance tests
│   ├── load/                       # Locust load tests
│   └── e2e/                        # Playwright end-to-end tests
├── .github/workflows/              # CI/CD pipelines
└── docs/                           # Documentation
```

## Management Console (DEAM)

The web interface at `/assets/` provides:

- **Asset Explorer** — browse and manage companies, teams, accounts, and platform integrations
- **Dashboard Builder** — create custom ECharts dashboards (bar, line, pie, metric, trend line, map, table)
- **Dashboard Viewer** — view dashboards with org/team filtering and time ranges
- **Import Data** — configure and trigger data imports from advertising platforms
- **Data Explorer** — natural language queries against Elasticsearch (powered by Claude)
- **Testing** — regression tests, visual regression, dashboard comparison, load testing, security dashboard
- **User Management** — permission-based access control with `mc_*` flags assigned to roles (`superuser` and `asset_admin` are the built-in admins; the legacy `org_admin` role has been retired in favour of granular permissions on custom roles)

## Development

### Branch Strategy

This repository is a periodically published snapshot of a private
development repository; `main` is its only long-lived branch.

- **Contributors**: fork the repository, create a branch off `main`, and open
  a PR against `main`. See [CONTRIBUTING.md](./CONTRIBUTING.md) for the full
  flow and how PRs are re-applied.
- **Maintainers**: publish new snapshots of `main` from the private
  repository; PRs merged here are ported privately before the next snapshot.
- PRs require passing CI checks (unit tests and secret scan) and review.

### Running Tests

```bash
# Python unit tests
pytest tests/ -v

# Security regression tests only
pytest tests/flask/test_security.py -v

# Playwright e2e tests (requires running server)
npm run test:e2e

# Visual regression tests (structural + screenshot)
npm run test:visual

# Update visual baselines
npm run test:visual:update
```

### CI Pipeline

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| Unit tests (pytest) | Push/PR to main | Unit tests (`-m "not integration and not e2e"`) |
| Secret scan (gitleaks) | Push/PR to main | Secret scanning |

## Documentation

- [Deployment](./deployment/README.md) — installation, configuration, and day-2 operations
- [Sphinx Docs](./docs/documentation/) — admin guide, API reference, user guide

## Community

- **Discord**: [https://discord.gg/6TE6GbcDax](https://discord.gg/6TE6GbcDax) — ask questions, discuss ideas, or get help
- **Contributing**: see [CONTRIBUTING.md](./CONTRIBUTING.md) for dev setup, the PR flow, and test expectations
- **Security issues**: please report privately per [SECURITY.md](./SECURITY.md) rather than opening a public issue


## License

Copyright © 2025–2026 Kavanah Media.

Kavanah Analytics is licensed under the [GNU Affero General Public License v3.0](./LICENSE) (AGPL-3.0-or-later).

This means you are free to use, modify, and distribute this software, but if you run a modified version as a network service, you must make the modified source code available to its users under the same license.

Contributions are accepted under the [Developer Certificate of Origin](https://developercertificate.org); see [CONTRIBUTING.md](./CONTRIBUTING.md#contribution-terms). There is no CLA.
