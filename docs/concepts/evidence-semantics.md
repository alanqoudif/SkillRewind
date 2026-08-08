# Evidence semantics

SkillRewind distinguishes five evidence classes for every influence edge (`skillrewind.domain.enums.EvidenceClass`):

| Class | Meaning | Who sets it |
|---|---|---|
| `recorded` | Directly captured as an input or context exposure at derivation time. | Capture SDK, JSONL import, manual `edge-add`. |
| `inferred` | Supported by static multi-trace scoring (expression/implementation/operational/behavioral/graph/temporal features). Not a causal claim. | `skillrewind.lineage.candidates.recover_candidates` |
| `replay-confirmed` | Supported by a paired intervention (present vs. withheld) within a declared, fully-reconstructed replay boundary. | `skillrewind.replay.service.run_paired_replay` |
| `rejected` | Tested but not supported under the declared intervention and probes. Not universal proof of no influence outside that boundary. | `run_paired_replay` |
| `unresolved` | Not replayable, inconclusive, outside budget, or outside coverage. A runner failure always lands here, never in `rejected`. | `run_paired_replay`, revocation policy decisions |

## Rules this codebase enforces

1. **A recorded edge proves exposure, not behavioral causation.** Nothing downstream (barrier, quarantine decision, attestation bounded claim) treats a `recorded` edge as proof the source *caused* the target's behavior — only that it was available as input.
2. **A similarity score is never replay confirmation.** The static scorer (`skillrewind.inference.scoring`) can only ever produce `inferred` edges. Only `run_paired_replay` can write `replay-confirmed` or `rejected`.
3. **A rejected edge is not universal proof of no influence.** Every attestation's bounded claims and every rejected-edge record carries the declared replay boundary (recipe, task snapshot, environment, fidelity) it was tested under.
4. **Recorded closure is exact and non-probabilistic.** `skillrewind.lineage.closure.build_graph` filters strictly to `evidence_class == "recorded"`, so a barrier or serving-resolution decision that depends on "recorded closure" can never be silently widened by an unconfirmed inferred or replay-confirmed-but-still-uncertain edge.
5. **Promotion preserves history.** When a candidate is replay-tested, the edge's `evidence` list is appended to (see `skillrewind.replay.service._persist_edge`), not overwritten — the static evidence that raised the candidate in the first place remains inspectable after replay confirms or rejects it.
