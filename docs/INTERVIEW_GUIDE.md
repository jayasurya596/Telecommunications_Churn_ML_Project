# Telecommunications Churn Platform Interview Guide

## Start with an accurate summary

“I built and deployed a synthetic-data telecom churn platform that demonstrates the ML lifecycle from data generation through model evaluation, approval, and scoring. Training runs locally with Python and scikit-learn. A React dashboard and FastAPI service run behind Cloudflare Access, with D1 for registry and audit metadata and R2 for model artifacts. The current approved model is active, and its artifact checksum is verified. The model results are synthetic, so I present this as an engineering demonstration rather than evidence of real retention impact.”

## One-minute project explanation

“The project’s purpose was to make the churn workflow reviewable and governed. I created a deterministic telecom-like data generator with versioned schema and quality metadata. The training pipeline uses chronological train, validation, and holdout partitions and fits preprocessing inside each model pipeline to reduce leakage. It compares a no-skill prior with logistic regression, random forest, and histogram gradient boosting. Logistic regression won on validation PR-AUC and beat the baseline. The selected candidate was registered with a checksum and release-gate evidence, then approved by a human before becoming active.

“The deployed app uses a Cloudflare Worker for the dashboard and routing, Access email PIN for authentication, a private Python Container for API and inference, D1 for the registry and quota, and R2 for model files. I also documented the limits: the data are synthetic, the current monitoring does not implement drift or alert delivery, and Cloudflare’s workers.dev and storage hints do not guarantee India or APAC residency.”

## Three-minute walkthrough

1. Open the protected app and identify the synthetic demo label and production environment.
2. Show the active model version and the current request quota.
3. Open Model registry and explain how a candidate carries its dataset version, run ID, threshold, evaluation metrics, release gates, dependency metadata, and artifact checksum.
4. Explain that approval is a separate human action. The current approval appears in D1 with actor, timestamp, decision, and reason. The recorded reason for this candidate is brief, so a future approval should explain the evidence reviewed.
5. Open Score a customer and use only the supplied synthetic `SYN-` snapshot. If the production scoring path is working during the interview, show the probability, risk band, threshold, model version, and factors. If it is not, do not imply production scoring was verified; show the local synthetic inference check and state that the signed-in production score still needs a live smoke test.
6. Open Monitoring and explain what is measured now: request count, 5xx rate, active model version, and schema signal limitations. Point out that drift signals and delayed-label performance are future work.

## Architecture explanation

“The Worker sits at the edge. Cloudflare Access authenticates the person, and the Worker verifies the JWT, applies the project request quota, strips client-supplied role headers, and forwards trusted identity to a private Container. The Container cannot reach the public internet; it accesses D1 and R2 through Worker outbound bindings. D1 stores the model lifecycle and operational counters. R2 stores the serialized scikit-learn pipeline. Training stays local, so candidate artifacts cross the boundary only when an operator registers them.”

“I chose this split so the ML workflow could use Python while the dashboard and API share one Cloudflare URL. The tradeoff is platform coupling in the Worker bindings and a Cloudflare-specific data-location story. The API contract is separately described in OpenAPI, but the current cloud implementation is not region-pinned to India.”

## Model and evaluation explanation

“I chose PR-AUC as the primary selection metric because the positive class is less common than the negative class. The same chronological partitions and preprocessing pipeline are used for all candidates. The split is 2,969 train, 1,004 validation, and 1,027 holdout rows. I select the estimator and review threshold on validation data, then evaluate the selected pipeline once on holdout.”

| Result | Value | How to explain it |
|---|---:|---|
| Validation PR-AUC, logistic regression | 0.4905 | Candidate-selection metric on synthetic validation data. |
| Validation PR-AUC, no-skill prior | 0.2888 | The selected candidate beat this baseline. |
| Holdout PR-AUC | 0.4425 | One-time final evaluation on a later synthetic period. |
| Holdout ROC-AUC | 0.7435 | Synthetic discrimination measure; not a business KPI. |
| Threshold | 0.7277 | Chosen on validation for about 10% review capacity. |

“These figures depend on the generator’s synthetic relationships. They are not expected production performance. I would need representative labeled history, a time-aware evaluation design, calibration review, and segment validation before making a business claim.”

## Governance and explanations

“Each trained artifact gets a model version derived from its checksum. The manifest stores the dataset and run IDs, selected model, candidate comparisons, threshold, dependency versions, holdout metrics, and automated release gates. The API checks the checksum before loading an artifact. Promotion requires a separate human decision; retraining itself cannot promote a model.”

“For a score explanation, the current code substitutes a feature with a reference value and measures the score change. It is useful for describing model behavior, but it does not prove that the feature caused churn. Global permutation importance is saved in the training report. It is not yet shown as a dashboard visualization.”

## Security and deployment explanation

“Access is enforced at the Worker and identity roles are mapped server-side. The Container is private and receives trusted identity headers only after JWT verification. Prediction validation accepts synthetic `SYN-` IDs and rejects the training label. The Worker enforces 5,000 project API requests per month. That is a project quota, not a Cloudflare bill cap. The agreed $5 alert is not configured.”

“The live address is a `workers.dev` hostname. The Container is constrained to APAC, but the Worker may execute globally, D1 APAC placement is a hint, and R2 placement is best effort. This deployment does not satisfy strict India or app-wide APAC residency. I keep it synthetic-only.”

## Questions an interviewer may ask

### Why use PR-AUC instead of accuracy?

Accuracy can look high when churn is the less common class. PR-AUC focuses on precision and recall for the positive class. I also report ROC-AUC, threshold-specific precision and recall, calibration, and segment results. The current values are from synthetic data only.

### How did you avoid leakage?

I excluded the label, customer key, and snapshot date from model features. The split is chronological and keeps dates disjoint. Each candidate uses a scikit-learn pipeline so preprocessing is fit only on the training partition. I select using validation and reserve the holdout for one final evaluation.

### Why did logistic regression win?

It achieved the strongest validation PR-AUC among the compared predictive candidates and exceeded the no-skill prior. It is also straightforward to inspect. The result does not prove that logistic regression will win on real telecom data; it is the choice for this generated dataset and run.

### What does the threshold mean?

The threshold is an operating point selected from validation scores for an approximately 10% review capacity. It defines the high-risk band in this demo. It must be reconsidered against the operational cost of outreach, false positives, and representative validation data.

### How do you know the artifact in production is the one you evaluated?

The model version is derived from the artifact SHA-256. The manifest stores the same checksum. The R2 object was downloaded and its checksum matched. D1 points to the approved version. I have not yet completed a signed-in production scoring smoke test in this audit, so I distinguish registry activation from verified end-to-end scoring.

### What is actually monitored?

Request count, server-error rate, active model version, schema signal limitations, and quota. Drift signals are currently empty; delayed-label performance is unavailable. There is no alert delivery yet. Those are known implementation gaps rather than implied capabilities.

### Why not train inside Cloudflare?

The project keeps training local and uploads a versioned candidate artifact to the registry. That avoids making the serving container a training worker and keeps training reproducible on the local Python toolchain. The cloud training endpoint intentionally returns `409` and explains the local CLI workflow.

### Is this ready for real subscriber records?

No. The records and metrics are synthetic, residency is not guaranteed, production scoring has not been freshly verified, backups and retention are incomplete, and drift/alerting are not implemented. It is a deployed engineering demo, not a real-data production service.

## Honest project limits to state

- No live signed-in scoring request was performed during the latest audit because browser use is disabled in the desktop settings.
- The dashboard supports one-record scoring; batch scoring exists only at the API level and there is no export workflow.
- The training reports are not durably stored in Cloudflare; the Container looks for them under a temporary directory.
- Monitoring does not calculate drift or delayed-label performance and does not send alerts.
- There is no CI workflow, Python dependency vulnerability scan, backup/retention plan, or exercised production rollback.
- The approval event for the active candidate contains the short reason `approved`; it does not describe evidence reviewed.
- Workers.dev and APAC location hints do not guarantee India or APAC residency. Keep data synthetic.

## Closing statement

“The strongest part of this project is the governed path from reproducible synthetic data to a versioned model and a human-controlled promotion. The next work is to verify production scoring live, complete the batch analyst workflow, add durable run history and useful drift/alerting, and decide whether the data-location guarantees are adequate before any real data is considered.”
