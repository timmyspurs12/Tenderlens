# GenLayer deployment evidence

`scripts/deploy_genlayer.mjs` writes the machine-readable outcome of each
deployment attempt here, and the `GenLayer deployment` workflow commits it so
the record survives workflow-log retention.

These files record what a configured GenLayer node returned. They are not an
audit of validator behaviour, not a legal opinion, and not a procurement award.
A `outcome: pending_finality` record means a transaction was broadcast but was
not observed as final; check the reported address and transaction before
treating it as deployed.
