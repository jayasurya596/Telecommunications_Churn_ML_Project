# ADR 0001: Cloudflare application and deployment

**Status:** Worker deployed on workers.dev; authenticated overview API previously confirmed; synthetic candidate approved and active; signed-in scoring smoke test remains outstanding  
**Date:** 2026-11-09

## Context

The owner requested a single protected app link. They initially preferred APAC processing and India storage, then chose a free workers.dev address for the interim deployment and a custom-domain upgrade later. Cloudflare Regional Services cannot regionalize the workers.dev hostname; D1/R2 APAC location hints also do not guarantee residency.

## Decision

- Use Python/scikit-learn for data and training, FastAPI for the API, and React/TypeScript for the dashboard.
- Serve dashboard assets and API from the same Worker hostname.
- Enable the free workers.dev route for the Worker.
- Use Cloudflare Access email PIN. Verify the Access JWT in the Worker and map verified email addresses to roles using a server-side secret.
- Constrain the private API Container to APAC. The Worker reaches FastAPI through the Cloudflare Container binding.
- Store model registry, approval/audit events, and atomic quota count in D1 using an APAC location hint. Store model files in R2 using an APAC location hint.
- Generate data and train models locally. Keep the deployment synthetic-only unless data-location requirements are reviewed.
- Enforce the app's 5,000 API requests/month quota. Configure the separately accepted USD 5/month billing alert, while treating it as a notification only.

## Consequences

- The app is deployed at one free workers.dev URL, redirects unauthenticated traffic to Access, and previously served authenticated overview/quota/monitoring API data to the owner. Candidate `model-847014036e81da10` is now approved and active after a human decision. A signed-in production scoring request remains unverified in the latest audit.
- Regional Services applies only to a custom domain, so the interim Worker may execute globally. The Container is APAC-constrained, not India-pinned.
- D1's APAC hint is not guaranteed, R2's APAC hint is best effort, and Worker outbound subrequests are outside Regional Services. This is not a strict India/APAC residency design.
- Cloudflare usage can exceed the accepted USD 5 monthly alert target; the alert does not cap the account bill.
- The owner plans to upgrade to a custom domain later. Worker, D1 registry, R2 model bucket, Access protection, and Worker secrets are configured. The accepted USD 5/month alert is not configured.
- Model promotion requires automated release gates and separate human approval.

## References

- [workers.dev hostname](https://developers.cloudflare.com/workers/configuration/routing/workers-dev/)
- [Cloudflare Access for Workers](https://developers.cloudflare.com/workers/configuration/cloudflare-access/)
- [Workers Regional Services](https://developers.cloudflare.com/data-localization/how-to/workers/)
- [D1 data location](https://developers.cloudflare.com/d1/configuration/data-location/)
- [R2 data location](https://developers.cloudflare.com/r2/reference/data-location/)
- [Container placement](https://developers.cloudflare.com/containers/concepts/placement/)
