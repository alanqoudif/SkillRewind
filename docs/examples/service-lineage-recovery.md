# Example: ingest a derivation, then start candidate recovery (Service mode)

This walks through the Phase C2.1 API surface end to end: ingest two
artifacts, record a derivation linking them, then submit and observe a
candidate-recovery run. It assumes a running Service-mode API (`skillrewind
serve --database-url ...`) with auth disabled for local testing, or an
`ingest`+`read`-scoped API key.

## curl

```bash
BASE=http://127.0.0.1:8000/api/v1

# 1. Ingest a parent artifact.
PARENT_ID=$(curl -s -X POST "$BASE/artifacts?kind=agent-skill&logical_name=base-skill" \
  --data-binary "base skill body" | python3 -c "import sys,json; print(json.load(sys.stdin)['artifact_id'])")

# 2. Ingest the derived (child) artifact.
CHILD_ID=$(curl -s -X POST "$BASE/artifacts?kind=agent-skill&logical_name=derived-skill" \
  --data-binary "derived skill body" | python3 -c "import sys,json; print(json.load(sys.stdin)['artifact_id'])")

# 3. Create a derivation.
DERIVATION_ID=$(curl -s -X POST "$BASE/derivations" \
  -H "Content-Type: application/json" \
  -d '{"recipe": "skill-authoring-agent", "recipe_version": "1.0.0"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['derivation_id'])")

# 4. Record the parent as a direct input.
curl -s -X POST "$BASE/derivations/$DERIVATION_ID/inputs" \
  -H "Content-Type: application/json" \
  -d "{\"inputs\": [{\"parent_artifact_id\": \"$PARENT_ID\", \"relation\": \"direct-input\"}]}"

# 5. Set the derivation's output artifact -- this materializes a `recorded`
#    evidence edge PARENT_ID -> CHILD_ID.
curl -s -X POST "$BASE/derivations/$DERIVATION_ID/output" \
  -H "Content-Type: application/json" \
  -d "{\"artifact_id\": \"$CHILD_ID\"}"

# 6. Confirm recorded closure sees it.
curl -s "$BASE/lineage/$PARENT_ID/descendants"

# 7. Submit candidate recovery for the parent as a suspicious root.
curl -s -X POST "$BASE/lineage/recovery-runs" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: example-run-1" \
  -d "{\"root_artifact_id\": \"$PARENT_ID\"}"
# -> {"job_id": "...", "run_id": "...", "status": "accepted", "status_url": "...", "events_url": "..."}

# 8. Run a worker to process the job (in a real deployment this runs
#    continuously as `skillrewind worker-run`):
#    skillrewind worker-run --database-url ...

# 9. Poll the run and list candidates once it completes.
curl -s "$BASE/lineage/recovery-runs/<run_id>"
curl -s "$BASE/lineage/recovery-runs/<run_id>/candidates"
```

## Python

```python
import httpx

client = httpx.Client(base_url="http://127.0.0.1:8000/api/v1")

parent = client.post(
    "/artifacts", params={"kind": "agent-skill", "logical_name": "base-skill"}, content=b"base skill body"
).json()
child = client.post(
    "/artifacts", params={"kind": "agent-skill", "logical_name": "derived-skill"}, content=b"derived skill body"
).json()

derivation = client.post(
    "/derivations", json={"recipe": "skill-authoring-agent", "recipe_version": "1.0.0"}
).json()

client.post(
    f"/derivations/{derivation['derivation_id']}/inputs",
    json={"inputs": [{"parent_artifact_id": parent["artifact_id"], "relation": "direct-input"}]},
)
client.post(
    f"/derivations/{derivation['derivation_id']}/output", json={"artifact_id": child["artifact_id"]}
)

descendants = client.get(f"/lineage/{parent['artifact_id']}/descendants").json()
assert child["artifact_id"] in descendants["items"]

recovery = client.post(
    "/lineage/recovery-runs",
    headers={"Idempotency-Key": "example-run-1"},
    json={"root_artifact_id": parent["artifact_id"]},
).json()
print(recovery)  # {"job_id": ..., "run_id": ..., "status": "accepted", ...}

# Run a worker (e.g. `skillrewind worker-once --database-url ...`) or, in a
# test/embedded context, `Worker(JobQueue(engine)).run_once()`, then:
run = client.get(f"/lineage/recovery-runs/{recovery['run_id']}").json()
candidates = client.get(f"/lineage/recovery-runs/{recovery['run_id']}/candidates").json()
print(run["status"], candidates["items"])
```

Every candidate returned here has `"evidence_class": "inferred"` and
`"calibrated_probability": null` — a candidate score is a decision-support
signal for replay prioritization, never a recorded lineage fact or a
calibrated probability, until a real replay confirms it (not implemented in
this milestone; see `STATUS.md`).
