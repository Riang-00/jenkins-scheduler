# Jenkins Scheduler

- This is the public, independently installed Windows desktop edition.
- Work directly without an SDD workflow. Do not add approval checkpoints for routine implementation.
- Tests must use temporary data, synthetic accounts, and loopback Jenkins servers. Never trigger real deployments for verification.
- Never publish runtime data, credentials, logs, browser profiles, or company-specific defaults.
- Preserve pending and consumed reservation states across upgrades. Never replay a failed or uncertain deployment.
- Lock order: deployment, planning, short reservation lock. Keep network requests outside short locks.
- Validate Python tests plus frontend lint, typecheck, build, and the Windows native build. Test the real native backend registration path when changing registration.
- Keep the original private workspace and its user installation separate. Release artifacts belong under ignored artifacts/ or .staging/.
