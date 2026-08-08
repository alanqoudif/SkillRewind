# ADR 0005: Sandbox and no-network-by-default replay

## Status
Accepted (v0.2.0), with a documented gap

## Context
Counterfactual replay must never require a paid/hosted model or network access for the default demo and test suite, and must not let an API caller execute arbitrary code.

## Decision
- The default and only-tested-by-CI runner is `DeterministicFixtureRunner`: fully in-process, no I/O, keyed by a registered recipe name (`skillrewind.replay.deterministic.register_fixture`).
- Runners are resolved through an approved-name registry (`skillrewind.replay.base.get_runner`); an unknown name raises `UnapprovedRunnerError` rather than importing an arbitrary module path.
- `SandboxedSubprocessRunner` exists for allowlisted recipes only (`ALLOWLISTED_RECIPES`, populated explicitly by the caller, never from untrusted input), runs with a sanitized environment, POSIX CPU/memory/process rlimits, and a timeout with process-tree kill on expiry.
- An `OpenAICompatibleRunner` is deliberately **not implemented** in this release — the spec allows this as an optional adapter, never required for tests or the primary demo.

## Consequences
`SandboxedSubprocessRunner`'s isolation is honestly weaker than a real container: there is no network namespace, so a subprocess could still make outbound network calls if the host OS permits it. This is documented in the module docstring and in `SECURITY.md`, not silently glossed over. Real network isolation (a Docker-backed runner) is deferred to a future release alongside service-mode deployment.
