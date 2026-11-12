# Telecommunications Churn ML Platform

A deployed, governed machine-learning demonstration for telecom churn workflows. The project includes deterministic synthetic data generation, time-aware model evaluation, approval-controlled model promotion, scoring APIs, and a Cloudflare-hosted dashboard. Synthetic records and metrics demonstrate engineering behavior only; they are not actual subscribers or evidence of retention impact.

## Live application

[Open Churn Intelligence](https://telecom-churn-platform.penchalajayasuryachappidi.workers.dev) (Cloudflare Access email-PIN protected).

The live registry currently has approved and active candidate `model-847014036e81da10`. Registry state and artifact checksum were verified, and local synthetic inference succeeded. A fresh signed-in production scoring request was not completed in the latest audit, so end-to-end live scoring remains to be confirmed.

## Current status

The Cloudflare dashboard, Worker, private FastAPI Container, D1 registry, and R2 artifact storage are deployed. All 25 Python tests pass under Python 3.13; the frontend/Worker production build and `npm audit --audit-level=high` pass. `uv.lock` now matches `pyproject.toml`, and the 34 locked packages pass dependency consistency checks. A test rerun under the available Python 3.14.6 did not finish, so repeat the suite under the deployed Python 3.12 runtime before a new release. The full project is not yet complete against its PRD: live scoring still needs a user-side smoke test, the agreed $5 billing alert has not been configured, and drift monitoring, alert delivery, durable training history, batch analyst UI, backups, CI, and a production rollback exercise remain outstanding.

Keep the deployment synthetic-only. The free `workers.dev` route does not provide Regional Services; the Worker may execute globally, D1's APAC location hint is not a residency guarantee, and R2 APAC placement is best effort. The current deployment does not meet strict India or app-wide APAC residency. The $5 alert is not a hard billing cap, and the 5,000 monthly API quota only limits this application.

## Documentation

- [Full project documentation](docs/PROJECT_DOCUMENTATION.md) — architecture, data contract, model lifecycle, API, dashboard, deployment, verification, PRD coverage, and remaining work.
- [Full project documentation (Word)](docs/Telecommunications_Churn_ML_Project_Documentation.docx).
- [Interview guide](docs/INTERVIEW_GUIDE.md) — concise presentation, architecture walkthrough, metrics, likely questions, and accurate limitations.
- [Interview guide (Word)](docs/Telecommunications_Churn_ML_Project_Interview_Guide.docx).
- [Cloudflare deployment guide](docs/cloudflare-setup.md) — current configuration, operations, regional limitations, and domain upgrade notes.
- [Implementation status](docs/implementation-plan.md) — decisions, delivered work, and remaining deployment steps.
- [Candidate evaluation record](docs/model-candidate-2026-11-11.md) — metrics and artifact verification for the active synthetic candidate.
- [Architecture decision record](docs/adr/0001-stack-and-deployment.md) — deployment choice and consequences.
- [OpenAPI schema](contracts/openapi.yaml) — API contract.

## Run locally

Requires Python 3.12 or later and Node.js 22 or later.

```powershell
python -m pip install -e ".[app,cloudflare]"
npm ci
npm run build
churn-platform generate --rows 5000 --seed 42 --output data/generated
churn-platform train --dataset data/generated/dataset.csv --output-dir artifacts
```

Start the API and dashboard in separate terminals:

```powershell
uvicorn churn_platform.api:app --reload
npm run dev
```

Training runs locally. The cloud training endpoint intentionally returns `409`; candidate artifacts are registered separately and require a human approval decision before activation.

## Verify and deploy

```powershell
python -m unittest discover -s tests -v
npm run build
npx wrangler deploy --dry-run
```

To deploy a configured Cloudflare environment, use `npx wrangler deploy`. Follow [the Cloudflare deployment guide](docs/cloudflare-setup.md) and review its Access, data-location, and billing sections first. Do not upload real subscriber data to the current interim deployment.

The `infra/aws-india` directory is a retained prototype and is not used by the live Cloudflare deployment.
