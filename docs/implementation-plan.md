# Implementation status and deployment plan

## Owner decisions recorded

- Cloudflare Workers Paid and Enterprise Data Localization Suite are reported active. Cloudflare Access email PIN was selected.
- FastAPI + React/TypeScript was selected as the app stack.
- The owner chose the free workers.dev hostname for the interim deployment and plans to upgrade to a custom domain later.
- This interim design has no Regional Services on the workers.dev hostname. The Worker may execute globally; the API Container is constrained to APAC. D1/R2 APAC location hints are not residency guarantees. Keep the project synthetic/demo-only.
- A USD 5/month Cloudflare billing alert and 5,000 app API requests/month were accepted. The owner accepts possible overages; neither is a hard account-wide spending cap.
- Data generation and model training run locally. Dashboard, scoring API, monitoring, and approved-model registry are intended to run behind one Cloudflare hostname.
- The first administrator email was supplied in chat and is intentionally not stored in this repository.

## Implemented

- Versioned synthetic-data schema, deterministic generator, and dataset manifest.
- Time-ordered train/validation/holdout evaluation, baseline/candidate comparison, threshold and calibration analysis, release gates, model artifacts, and checksums.
- FastAPI API with synthetic-only single/batch scoring, candidate registration, independent approval/rejection, rollback, run listing, monitoring, audit events, and request quota.
- React dashboard built as Worker assets and served with the API from one hostname.
- Worker Access JWT verification and server-side role mapping; D1 registry/audit/quota handler; R2 model artifact handler; private FastAPI Container constrained to APAC.
- Wrangler is configured to enable the workers.dev route.
- Verification on 2026-11-11: all 25 Python unit/API tests pass under system Python 3.13.13. A 5,000-row synthetic dataset and candidate were generated in Docker's Python 3.12.16 runtime to match the deployed container. The React production bundle and Worker TypeScript checks pass. Wrangler deploy --dry-run built the Container image and accepted the assets/bindings. The FastAPI Container passed health and overview HTTP smoke checks.
- Deployed on 2026-11-11 to https://telecom-churn-platform.penchalajayasuryachappidi.workers.dev. D1/R2 bindings are connected and the D1 migration is applied. Access app and Worker secrets are configured. The owner completed Access email-PIN sign-in; the live dashboard displayed authenticated overview, quota, and monitoring API data. Preview/version URLs are disabled.
- Post-deployment release `876f66fa-5aa3-41ca-aca7-e90027cd909f` updated residency wording and disabled Container log collection after diagnosis. Post-deploy checks returned the expected Access redirect for `/`, `/health/live`, and `/v1/overview`; an authenticated dashboard view had previously confirmed API overview/quota/monitoring data.
- A Python 3.12.16 / scikit-learn 1.9.1 training run produced candidate `model-847014036e81da10` from 5,000 synthetic rows. Four candidates were compared; the selected logistic-regression model beat the validation no-skill PR-AUC (0.4905 vs 0.2888), and all automated release gates passed. It is registered and active in D1, with its checksummed artifact in R2. The owner approved it; registry state, active pointer, approval event, and downloaded R2 checksum were verified. A local synthetic inference check succeeded with scikit-learn 1.9.1. See `model-candidate-2026-11-11.md`.
- Fixed candidate-manifest generation to include the final checksum, holdout, and aggregate automated release-gate fields; added regression assertions and reran all 25 tests successfully.

## Proposed production request path

Browser
  -> https://telecom-churn-platform.<account-subdomain>.workers.dev
  -> Cloudflare Access email PIN
  -> Worker serves React assets, validates Access JWT, and applies request quota
  -> Worker sends API requests to a private Container (APAC)
  -> FastAPI scoring, approval, monitoring, and audit
  -> D1 registry/quota (APAC location hint, not guaranteed)
  -> R2 model artifacts (APAC location hint, best effort)

## Verification limits

The live authenticated overview, quota, and monitoring path are confirmed from the owner's dashboard screenshot. After the latest release, unauthenticated requests to the app, health, and overview paths still redirect to Access as configured. All 25 Python tests and the synthetic training run pass. The candidate is approved and active, and local synthetic inference succeeds. A signed-in production scoring request remains unverified from this session. Wrangler local development with Containers is unsupported on this Windows host, so verify the serving path through the deployed app.

## Budget and location limits

The USD 5/month Cloudflare billing alert is accepted but not configured. The 5,000 request quota applies to the app only. Cloudflare Workers Paid and Container usage can exceed included allotments; neither the alert nor quota limits the full invoice.

The free workers.dev hostname cannot use Regional Services. The Worker may execute globally, while its Container is constrained to APAC. D1's APAC hint is not guaranteed, and R2's APAC hint is best effort. This interim design does not meet strict India-only or APAC-only processing/storage requirements.

## Remaining deployment work

1. Verify a signed-in production score and model-serving path in the live app.
2. Configure the accepted USD 5 billing alert in Cloudflare. It is a notification target, not a hard spending cap.
3. Later, connect a custom domain and configure Regional Services for its hostname if desired. Review residency limitations before using real subscriber data.

## Remaining PRD work

The project is deployed, but it is not complete against the PRD. The latest audit could not perform authenticated browser checks because browser use is disabled in the desktop settings. Remaining product and operational gaps include:

- Add the dashboard batch-upload, progress, customer explorer, filtering, and export workflow. The API already accepts batch scoring requests.
- Persist training-run reports in a durable cloud store and show run history in the dashboard; current Container report files use temporary storage.
- Implement drift metrics, delayed-label performance, thresholds, and alert delivery; the current monitor reports request health and currently has no drift signals.
- Add a CI workflow and Python dependency vulnerability scanning. `uv.lock` was refreshed and its 34 installed packages pass dependency consistency checks, but a locked-environment test rerun under the available Python 3.14.6 did not finish; the 25 tests passed under Python 3.13.13. Repeat the full suite under the deployed Python 3.12 runtime before a release. The global host `pip check` also reports an unrelated preinstalled `mediapipe` incompatibility.
- Define data retention and backups, test restore procedures, and exercise rollback after a second approved model exists.
- Record source revision in model lineage and require approval reasons that describe reviewed evidence. The current approval reason is `approved`.
- Review the full PRD status matrix in `PROJECT_DOCUMENTATION.md` before claiming completion or considering real subscriber data.
