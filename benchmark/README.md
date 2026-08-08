# RewindBench v0.1 Design

RewindBench evaluates revocation of persistent agent skills when influence provenance is incomplete. It is designed around **planted ground truth**, controlled edge loss, inert canary behavior, and explicit cost accounting.

## 1. Benchmark tracks

### Core controlled track

A generator creates derivation DAGs with known ancestors, benign utility tasks, and inert canaries. The full oracle graph is retained privately by the scorer. Participant systems receive the artifacts, derivation snapshots permitted by the track, and an observed graph with selected edges removed.

### Public-system track

Adapters instrument pinned versions of public self-evolving skill systems. The initial candidates are SkillClaw and Trace2Skill; CoEvoSkills is an optional third integration. Each run must record commit hashes, container digests, model identifiers, prompts, and replay limitations.

## 2. Scenario families

| Family | Hidden influence pattern |
|---|---|
| Direct inheritance | Instruction or code is copied with light edits. |
| Semantic laundering | A rule is paraphrased without distinctive lexical overlap. |
| Implementation mutation | Code is refactored, translated, or moved to another API while behavior persists. |
| Procedural inheritance | The same ordering, trigger, or trust assumption survives surface changes. |
| Multi-hop contamination | Influence passes through two to five generations. |
| Memory-to-skill promotion | A memory or summary is distilled into a reusable skill. |
| Cross-model distillation | One model creates a skill and another model revises it. |
| Compositional influence | The target effect requires two otherwise benign ancestors together. |

## 3. Provenance-loss mechanisms

Each case can be evaluated at edge-loss rates of 0%, 1%, 5%, 10%, 25%, and 50% using:

- **uniform:** random edge dropout;
- **type-selective:** remove a relationship class, such as memory-to-skill;
- **bridge-targeted:** remove high-betweenness or cut edges;
- **attribution-selective:** remove edges likely to be omitted when an evolver records only explicitly cited evidence.

The removed edges remain available to the oracle scorer and are never exposed to the evaluated system.

## 4. Inert canaries

Allowed target effects include:

- emitting a unique canary token;
- selecting a mocked unsafe Boolean flag;
- writing a marker to a temporary sandbox path;
- making a tool call to a local fake endpoint; and
- selecting a disallowed branch in a simulator.

Cases must not contain real credentials, destructive commands, production endpoints, or live exfiltration targets.

## 5. Strict negatives

Every scenario family must include independently developed artifacts that implement similar functionality but do not descend from the revoked root. These strict negatives test whether a method mistakes common solutions for inheritance.

Additional negatives should include:

- same text but different operational behavior;
- different text but common library boilerplate;
- shared tool calls with different triggers;
- common code templates; and
- temporal proximity without shared derivation support.

## 6. Splits

Recommended splits are grouped, not random at the artifact level:

- **train:** transformation templates, task families, and model combinations available for calibration;
- **development:** unseen graphs with known scenario families;
- **test-in-domain:** new tasks and graph instances from known families;
- **test-transformation:** withheld mutation operators;
- **test-model:** a withheld evolver or model family;
- **test-composition:** higher-order interactions not present in training.

No descendant or near-duplicate of a test artifact may occur in training.

## 7. Inputs and outputs

Each case follows [`../spec/benchmark-case.schema.json`](../spec/benchmark-case.schema.json).

A system receives:

- artifacts and permitted manifests;
- the observed graph;
- revoked roots;
- replay snapshots permitted by the track;
- a replay-call budget; and
- a validation API that does not reveal the oracle graph.

A system returns:

- predicted affected closure;
- evidence class and confidence per predicted edge;
- replay decisions and costs;
- quarantine and rebuild decisions; and
- a revocation attestation.

## 8. Primary metrics

1. Hidden-descendant precision, recall, F1, and path-level recall.
2. Residual canary activation after revocation and repair.
3. False quarantine rate.
4. Retained clean utility.
5. Repair success and safe republish rate.
6. Replay calls, tokens, wall-clock time, and cost-normalized recall.
7. Brier score, expected calibration error, and reliability diagrams.
8. Attestation coverage across recorded, inferred, replay-confirmed, and unresolved evidence.

## 9. Baselines

- `DeleteRoot`
- `RecordedClosure`
- `Embedding`
- `StaticMultiTrace`
- `ExhaustiveReplay`
- `SkillRewind-Active`

Public related systems should be run from official code when compatible. Reimplementations must be labeled as such.

## 10. Experimental discipline

- Pair conditions on the same case, seed, snapshot, and candidate graph.
- Freeze benchmark generation before the primary experiment.
- Report confidence intervals and paired statistical tests.
- Record every excluded run and reason.
- Map every paper number to raw artifacts and an analysis script.
- Report model and environment drift separately from method failure.

## 11. Example

See [`examples/semantic-laundering.case.json`](examples/semantic-laundering.case.json). Its unsafe behavior is a sandbox-only canary flag; it is not an operational exploit.
