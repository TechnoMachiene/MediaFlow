# Definition of Done (per component)

Component closes only when ALL tick. No partial credit. No "fix later".

## Automated (`make done`)
- [ ] `ruff check .` clean
- [ ] Unit tests pass (`pytest -m unit`)
- [ ] Integration tests pass on REAL Redis/Postgres (`pytest -m integration`)
- [ ] Coverage >= 85% over unit + integration
- [ ] CI green on branch

## Test content (human review)
- [ ] Happy path
- [ ] Bad input (null, empty, wrong type, huge)
- [ ] Boundary (0, 1, max, DST gap, midnight, empty queue)
- [ ] Failure path (service down, timeout, 429, crash mid-job)
- [ ] Concurrency, if shared state touched (>= 4 threads/procs, no duplicates)
- [ ] Idempotency, if retried (run twice = same result)
- [ ] Regression test for every bug found while building

## Non-functional (when relevant)
- [ ] Security: authz checked, input validated, no secrets in code/logs
- [ ] Privacy: user media/data retention rule applied
- [ ] Performance: measured once, number written down
- [ ] Logs: component emits structured log with ids, no PII

## Delivery
- [ ] Manual demo run, output pasted in PR
- [ ] Docs/README/.env.example updated
- [ ] Migration reversible (if schema changed)
- [ ] Merged to main. Tag: `component-<n>-<name>`
- [ ] Backlog updated. Next card chosen.
