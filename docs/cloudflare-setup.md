# Cloudflare deployment guide

## Current choice and status

The owner chose a free workers.dev address for now and plans to add a custom domain later. The Worker is deployed at https://telecom-churn-platform.penchalajayasuryachappidi.workers.dev. The connected account has D1 database `churn-registry`, R2 bucket `telecom-churn-models`, and the D1 schema migration applied. The owner completed email-PIN sign-in and confirmed the live authenticated dashboard loaded overview, quota, and monitoring data through the API.

The owner created the Access application in the dashboard and shared its team domain and audience tag; these were stored as Worker secrets. Unauthenticated requests to the dashboard, health, and API routes redirect to the Access sign-in page. The OAuth login can deploy the Worker and manage D1, but it lacks Access application/policy write permissions. The owner created the R2 bucket in the dashboard. The authenticated API overview, quota, and monitoring calls are confirmed from the live dashboard.

Workers Paid is reported active. Cloudflare's Workers Paid plan has a USD 5/month minimum, with included usage and possible usage-based charges beyond the allotments. Containers also have usage-based charges. The owner previously accepted a USD 5/month billing alert and a 5,000 API requests/month project quota. The alert is not configured yet and is not a hard bill cap.

## Cloudflare architecture and location limits

One workers.dev hostname will serve the dashboard and API. The Worker serves built React assets, verifies Cloudflare Access JWTs, applies the monthly API quota, and routes API calls to a private FastAPI Container. The Container is constrained to APAC and reaches D1/R2 through outbound handlers.

The free workers.dev route cannot be configured for Regional Services. The Worker may therefore execute globally; the API Container is constrained to APAC. D1 and R2 are configured with APAC location hints, but this is not a strict location boundary:

- D1's APAC location hint does not guarantee the database location. Its documented jurisdiction restrictions are EU, US, and FedRAMP; India is not a supported D1 jurisdiction.
- R2's APAC location hint is best effort.
- Regional Services is available only on a configured custom domain and does not restrict Worker outbound subrequests.
- Worker code and secrets are distributed globally even when execution is regionalized.
- Cloudflare Access and operational metadata may have separate location handling.

The owner selected workers.dev after these constraints were explained, with a custom-domain upgrade planned for later. Keep the app synthetic-only; do not upload real subscriber records under this interim location design.

References: [workers.dev hostname](https://developers.cloudflare.com/workers/configuration/routing/workers-dev/), [Cloudflare Access for Workers](https://developers.cloudflare.com/workers/configuration/cloudflare-access/), [Workers Regional Services](https://developers.cloudflare.com/data-localization/how-to/workers/), [D1 data location](https://developers.cloudflare.com/d1/configuration/data-location/), [R2 data location](https://developers.cloudflare.com/r2/reference/data-location/), [Container placement](https://developers.cloudflare.com/containers/concepts/placement/), [Container outbound bindings](https://developers.cloudflare.com/containers/configuration/workers-connections/), [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/), and [Container pricing](https://developers.cloudflare.com/containers/platform/pricing/).

## Provision and deploy

1. Confirm that the `telecom-churn-platform` Access app allows the intended administrator email with One-time PIN. Do not enable account-wide protection unless you intend it for all Workers.
2. The team domain, audience tag, and server-side role map have been stored as Worker secrets. Keep these values out of the repository.
3. D1 `churn-registry` and R2 `telecom-churn-models` already exist. The D1 ID is configured in wrangler.jsonc and migration `0001_registry.sql` is applied. Verify their state before continuing.

       npx wrangler d1 create churn-registry --location=apac

   Apply the migration after the binding is updated:

       npx wrangler d1 migrations apply churn-registry --remote

4. For a new environment, create the model-artifact bucket with an APAC location hint:

       npx wrangler r2 bucket create telecom-churn-models --location=apac

   R2 placement is best effort. The name must match the Wrangler binding.
5. Build and validate the release:

       npm ci
       npm run build
       npx wrangler deploy --dry-run

6. Configure the previously accepted USD 5/month billing alert. This is a notification target, not a hard billing cap. The app enforces 5,000 API requests per month in D1.
7. Redeploy after Access protection and secrets are ready:

       npx wrangler deploy

8. Confirm the exact workers.dev URL in Wrangler's deployment output. Verify email-PIN sign-in, role boundaries, health, quota, candidate approval/rejection/rollback, scoring, and monitoring before sharing it.

## Future custom-domain upgrade

After registering or connecting a domain, attach it to the Worker as a custom domain. Configure Regional Services for that custom hostname if the Cloudflare account entitlement allows it. Regional Services can regionalize Worker execution on the hostname, but it does not constrain outbound subrequests, D1/R2 location hints, or global distribution of Worker code and secrets. Reassess the data-location requirements before using real subscriber data.

## Application behavior

- Synthetic data generation, chronological model evaluation, release gates, candidate registration, human approval, rollback, monitoring, audit events, and scoring are implemented in the Python platform and dashboard.
- Only approved artifacts can score records. Inference accepts synthetic SYN- identifiers and rejects training-target fields.
- D1 holds the model registry, approvals, audit events, and atomic request quota counter. R2 holds model artifacts.
- Worker assets and the API share one hostname. The API Container has no direct internet access and uses binding-backed outbound routes.
- Human approval is required before a trained candidate can become active.

## Verification and remaining work

- Candidate `model-847014036e81da10` is approved and active. The signed-in dashboard previously loaded overview, quota, and monitoring data, but a fresh signed-in score and governance action were not verified in the latest audit.
- Configure the accepted USD 5/month billing alert. The 5,000-call quota does not cap the Cloudflare invoice.
- Do not use real subscriber data with the current location design. Strict India/APAC residency is not established.
- See `PROJECT_DOCUMENTATION.md` for the complete PRD coverage matrix and other unfinished work, including durable training history, drift and alert delivery, backups, CI, batch analyst UI, and rollback verification.
