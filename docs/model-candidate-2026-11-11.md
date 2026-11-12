# Synthetic model candidate: 2026-11-11

This report records a locally trained candidate registered in the deployed Model registry. The owner approved it on 2026-11-11; D1 records it as the active model. A signed-in production scoring request has not yet been verified from this session.

## Reproduction and artifact

- Dataset: 5,000 generated synthetic rows, seed 42, dataset version `ds-28365cd8429c48f9`.
- Runtime: Python 3.12.16, scikit-learn 1.9.1, pandas 3.1.1 (matches the deployed API container's Python/scikit-learn runtime).
- Training run: `run-ba00b708db4a40b9`.
- Candidate: `model-847014036e81da10` (`logistic_regression`), compared against three other model candidates and a no-skill prior.
- Artifact: `artifacts/models/model-847014036e81da10/model.joblib`.
- Manifest: `artifacts/models/model-847014036e81da10/manifest.json`.
- SHA-256: `847014036e81da1029cafbe5ded44340e90926215d985ca927fcce8cd0c544ea`.
- Threshold selected on validation: `0.727656879` (positive when probability is greater than threshold).
- Automated release gates: all pass, including schema, leakage-safe feature set, chronological partitions, candidate comparison, validation baseline, one-time holdout evaluation, and checksum.

## Synthetic evaluation

| Partition | Rows | PR-AUC | ROC-AUC | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| Validation | 1,004 | 0.4905 | 0.7254 | 0.5556 | 0.1897 | 0.2828 |
| Holdout | 1,027 | 0.4425 | 0.7435 | 0.4783 | 0.1686 | 0.2493 |
| Validation no-skill prior | 1,004 | 0.2888 | 0.5000 | 0.0000 | 0.0000 | 0.0000 |

The chronological split contains 2,969 training rows, 1,004 validation rows, and 1,027 holdout rows. Selection and threshold choice used validation; the holdout was evaluated once after candidate selection. All figures describe generated synthetic data only. They are not estimates of real telecom performance, business lift, or churn reduction.

## Candidate activation and verification

The D1 approval event records the owner as approver, with the reason `approved`; the platform active-model pointer references this version. A local synthetic scoring smoke test loaded the artifact with Python 3.13.13 and scikit-learn 1.9.1, verified its checksum, and returned probability `0.634634` for a synthetic example snapshot. This is a local inference check and does not verify a signed-in request through the deployed Worker and Container.

To check production serving, open the deployed app, select **Score a customer**, enter a synthetic `SYN-` customer snapshot, and run prediction. Do not enter real subscriber data under the current location design.

Do not upload real subscriber data or treat this synthetic candidate as business-validated.
