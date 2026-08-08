# RFC 0001: Hidden-Lineage Revocation for Self-Evolving Agent Skills

- **Status:** Draft
- **Version:** 0.1
- **Author:** Faisal Ali Said Al-Anqoudi, Nuqta Technologies
- **Date:** 9 August 2026

## 1. Abstract

This RFC defines the proposed semantics of SkillRewind, a revocation layer for persistent artifacts produced by self-evolving AI agents. The system records immutable derivations when possible, reconstructs candidate influence relationships when provenance is incomplete, tests selected relationships through counterfactual re-derivation, quarantines confirmed descendants, rebuilds them from retained support, and emits a bounded attestation.

The RFC deliberately separates deterministic facts from probabilistic or interventional evidence. It does not define perfect semantic unlearning, parameter erasure, or a universal proof that an unwanted behavior can never recur.

## 2. Motivation

An agent may promote a transient trajectory, memory, document, or tool output into a reusable skill. Later skills can inherit the behavior while changing the surface form. Examples include:

- paraphrasing the original instruction;
- translating or refactoring implementation code;
- preserving a procedural ordering or trust assumption;
- consolidating multiple memories into a new skill;
- revising a skill with another model; or
- producing a harmful effect only when multiple ancestors are combined.

Deleting the original file removes the source artifact, not necessarily the descendant behavior. A recorded dependency graph can expose descendants only when its edges are complete. SkillRewind therefore treats incomplete influence provenance as a first-class operating condition.

## 3. Terminology

### 3.1 Artifact

An immutable, content-addressed object that may influence or be produced by an agent. Examples include:

- trajectory or execution trace;
- memory or summary;
- Agent Skills directory;
- prompt patch;
- generated source code or configuration;
- template or workflow;
- validation report; and
- environment or model snapshot.

### 3.2 Derivation

A build-like event that creates an artifact from a declared recipe, environment, model configuration, and support set.

### 3.3 Recorded edge

A relationship captured directly by instrumentation, such as `used-as-input`, `retrieved-during-derivation`, or `distilled-from`.

### 3.4 Latent influence edge

A behaviorally relevant relationship that existed during derivation but is absent from the recorded graph.

### 3.5 Revoked root

An artifact that is invalidated because it is poisoned, obsolete, legally restricted, untrusted, or otherwise no longer permitted to support active descendants.

### 3.6 Replayable artifact

An artifact whose derivation can be reconstructed within a declared fidelity boundary using a retained task snapshot, candidate context set, recipe, model identifier, environment digest, and validation suite.

### 3.7 Attestation

A signed or hash-addressed report that states what was recorded, inferred, replay-confirmed, rebuilt, verified, and left unresolved.

## 4. Goals

SkillRewind SHOULD:

1. withdraw the recorded descendant closure before probabilistic analysis;
2. preserve exact evidence for captured derivations;
3. recover plausible hidden influence with complementary trace families;
4. distinguish correlation from intervention-confirmed effects;
5. allocate a finite replay budget to the most decision-relevant candidates;
6. rebuild descendants only from retained, non-revoked support;
7. verify both target-behavior removal and retained task utility; and
8. expose coverage and uncertainty in a machine-readable attestation.

## 5. Non-goals

SkillRewind does NOT promise:

- exact removal from foundation-model parameters;
- reliable causal claims when the derivation cannot be replayed;
- future safety outside the declared verifier suite;
- identical outputs from nondeterministic or drifting hosted models;
- automatic repair of every compositional interaction; or
- a guarantee that semantic similarity implies inheritance.

## 6. Threat model

An adversary may influence bounded trajectories, documents, memories, tool outputs, or imported skills that enter an evolution pipeline. The adversary may attempt to conceal propagation through paraphrase, code mutation, multi-hop derivation, or cross-model revision.

The adversary is assumed unable to modify the SkillRewind event log, benchmark oracle, signing keys, or isolated experiment harness. A stronger threat model covering log compromise is deferred to a future RFC.

## 7. Artifact identity and immutability

Each persisted artifact MUST have a digest over a canonical serialization. The manifest MUST distinguish the content digest from logical names such as `deploy-fastapi/latest`.

Minimum identity fields:

```json
{
  "artifact_id": "skill://deploy-fastapi@sha256:91ab...",
  "digest": "sha256:91ab...",
  "kind": "agent-skill",
  "created_at": "2026-08-09T00:00:00Z"
}
```

Mutating an artifact MUST produce a new version and digest. Aliases MAY point to versions but MUST NOT replace the immutable record.

## 8. Derivation manifest

A replay-oriented derivation manifest SHOULD record:

- builder recipe and version;
- model provider, model identifier, and decoding configuration;
- environment or container digest;
- task and workspace snapshot digests;
- exact recorded inputs;
- candidate context pool, even when individual influence is unknown;
- retrieved memories and active skills;
- tool schemas and normalized tool results;
- random seeds where meaningful;
- validation suite and outcomes; and
- replayability limitations.

The candidate context pool is important: it permits later interventions even when the original system failed to record which member actually influenced the result.

## 9. Evidence types

An influence edge has exactly one current evidence class and MAY be promoted when stronger evidence is obtained.

### 9.1 Recorded

Directly captured by the derivation instrumentor. Recorded does not automatically mean behaviorally causal; it means the artifact was an explicit input or exposure.

### 9.2 Inferred

Supported by static or observational evidence. Candidate features MAY include:

- lexical fingerprints and semantic representations;
- normalized ASTs and API-call signatures;
- data-flow or configuration summaries;
- operational graphs of triggers, steps, tools, and artifacts;
- deterministic behavioral probe vectors;
- shared tasks and context pools;
- version adjacency and temporal proximity; and
- graph paths through already confirmed relations.

An inferred edge MUST include a calibrated confidence score and evidence references.

### 9.3 Replay-confirmed

Supported by a paired intervention that reconstructs the derivation with a candidate ancestor present and withheld or replaced by a clean control. A replay-confirmed edge MUST state:

- intervention definition;
- replay fidelity information;
- repetitions and seeds;
- outcome distance or target-behavior difference;
- confidence interval or deterministic result; and
- validation artifacts.

### 9.4 Rejected

Tested and not supported under the declared intervention and verifier suite. Rejection is bounded and MUST NOT be interpreted as universal proof of no influence.

### 9.5 Unresolved

Outside the replayability boundary, inconclusive, or not tested within budget.

## 10. Revocation algorithm

### 10.1 Barrier-first hard closure

On receipt of a valid revocation event, the runtime MUST immediately make the revoked roots and recorded descendant closure non-servable. This occurs before hidden-lineage analysis.

### 10.2 Candidate recovery

The runtime constructs candidate hidden edges within a bounded temporal, task, and context neighborhood. The candidate stage SHOULD optimize recall but MUST include strict negatives representing independently developed same-function skills.

### 10.3 Active replay selection

Counterfactual tests are selected under a budget. A candidate's priority SHOULD reflect:

- probability of an edge;
- expected change to the affected closure;
- bridge or cut importance in the uncertain graph;
- severity of the target behavior;
- cost and fidelity of replay; and
- expected information gain.

Exhaustive pairwise replay is a benchmark baseline, not a production requirement.

### 10.4 Interaction testing

When the target behavior may require several ancestors, the runtime MAY run pairwise, grouped, or Monte-Carlo Shapley-style interventions. Because interaction discovery can be exponential, the attestation MUST report the tested order and remaining interaction boundary.

### 10.5 Quarantine

Replay-confirmed descendants MUST remain non-servable until repaired or explicitly waived by an authorized human. Inferred high-risk descendants MAY be conservatively quarantined according to policy.

### 10.6 Clean-room rebuild

A descendant can be rebuilt only from retained support that excludes revoked and confirmed contaminated ancestors. Its original recipe MAY be reused if the recipe itself is not revoked.

A rebuilt successor MUST pass:

1. task-specific deterministic verification;
2. target-behavior safety probes;
3. paired utility tests against an appropriate reference;
4. predecessor-closure checks; and
5. integrity checks over the rebuilt manifest.

Failure leaves the artifact quarantined.

## 11. Revocation policies

A conforming implementation SHOULD expose at least three policies:

- **strict:** quarantine all recorded and high-confidence inferred descendants;
- **balanced:** hard closure plus active replay for uncertain candidates;
- **forensic:** produce a blast-radius report without modifying serving state.

Policy parameters and thresholds MUST be included in the attestation.

## 12. Bounded attestation

A revocation attestation MUST contain:

- revoked roots and reason class;
- start and completion timestamps;
- tool and schema versions;
- declared replayability boundary;
- recorded closure;
- inferred candidates and confidence;
- replay-confirmed, rejected, and unresolved edges;
- quarantined artifacts;
- rebuilt and republished artifacts;
- verifier suite and outcomes;
- residual target-behavior observations;
- replay, token, and wall-clock costs; and
- bounded claims.

A permissible claim is:

> No active artifact remains in the recorded and replay-confirmed closure, and the declared target behavior was not observed under the listed probe suite.

An impermissible claim is:

> The agent has perfectly forgotten the revoked information in every possible future context.

## 13. Agent Skills compatibility

SkillRewind SHOULD remain compatible with the Agent Skills directory format. A skill MAY include:

```text
my-skill/
├── SKILL.md
├── DERIVATION.json
├── tests/
├── scripts/
└── references/
```

Optional metadata can point to the immutable digest and derivation manifest:

```yaml
metadata:
  skillrewind.digest: "sha256:91ab..."
  skillrewind.derivation: "DERIVATION.json"
```

A library-level lock file MAY pin exact skill digests and trusted roots.

## 14. Provisional command surface

```text
skillrewind capture -- <agent command>
skillrewind closure --root <artifact>
skillrewind candidates --root <artifact>
skillrewind replay --edge <source> <target>
skillrewind revoke --root <artifact> --policy balanced
skillrewind rebuild --event <revocation-id>
skillrewind verify --event <revocation-id>
skillrewind attest --event <revocation-id>
```

Version 0.1 of the codebase implements only the recorded closure and a recorded-evidence attestation.

## 15. Failure handling

- If an artifact is not replayable, it MUST NOT be labeled replay-confirmed.
- If model or environment drift exceeds a declared threshold, replay evidence MUST be downgraded or marked invalid.
- If verification is incomplete, the artifact remains quarantined unless a human waiver is recorded.
- If the log is incomplete, the attestation MUST report the known coverage gap.
- If budget exhaustion leaves high-severity candidates unresolved, the default SHOULD be fail-closed.

## 16. Open research questions

1. Which combination of trace families best separates inheritance from independent convergence?
2. How should replay fidelity be quantified across hosted and local models?
3. Which active-selection objective best trades cost against hidden-descendant recall?
4. How can higher-order compositional influence be tested without combinatorial explosion?
5. How should attestations be signed, merged, and audited across organizations?
6. How should externalized skill revocation interact with parametric unlearning?

## 17. Security and ethics

Research payloads MUST use inert canaries and mocked side effects. Live credentials, production systems, destructive commands, and real exfiltration are out of scope. Vulnerabilities found in public systems should follow coordinated disclosure before exploit details are released.
