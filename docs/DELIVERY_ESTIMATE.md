# Delivery estimate

Planning baseline: 24 October 2026. These are engineering estimates, not promised
release dates. They assume one engineer contributing about 30 focused hours per
week, stable MVP scope, and timely provider credentials, infrastructure and review.

| Deliverable | Remaining effort | Estimated window from baseline |
| --- | --- | --- |
| Reviewable off-chain MVP with dashboard, explanations, history, simulations and live updates | 110–180 hours | 4–6 weeks: 21 November–5 December 2026 |
| Testnet demo including evidence commitments, signed reports, contracts, consumer and runbook | 200–330 hours total | 8–12 weeks: 19 December 2026–16 January 2027 |

Off-chain work includes benchmark follow-through and real service checks (20–30
hours), historical/provider/dependency-evidence work (30–50), dashboard and simulation
flows (35–50), live round delivery and reconnect/resume (10–20), and integration,
browser verification and packaging (15–30). These tasks overlap; effort and calendar
ranges are approximate. Testnet adds approximately 90–150 hours for canonical
evidence/proofs, signing/key policy, contract implementation/tests, deployment and
the end-to-end demo. Paid audit and production operations are outside these ranges.

Backend observations, six adapters, ingestion/replay, statistical diagnostics,
dependency registry, source scoring, quarantine/weighting, aggregation, quality gates
and full-pipeline synthetic benchmarks are implemented. Dashboard/live delivery and
testnet publication remain unimplemented. The synthetic correlated-majority attack
target fails; honest unavailable behavior remains essential.

Usable live publication also depends on verified independence/deployment evidence
and sufficient fresh providers. Historical calibration is incomplete. These are
release gates: an evidence delay can extend the calendar even if the interface and
demo are complete. Provider access is not assumed to prove source independence.

The broader vision—independently operated validators, governance, staking,
generalized data domains, ZK/TEE/AVS work—is outside MVP and requires separately
scoped research, staffing and estimates. A defensible final date for that entire
vision is not available from the current roadmap.

Next sequence: review measured benchmark limitations; close service-runtime and
evidence/calibration gaps; implement dashboard/live updates; build evidence and
testnet integration. Refresh this estimate when those external gates are resolved.
week, stable MVP scope, and timely provider credentials, infrastructure and review.
