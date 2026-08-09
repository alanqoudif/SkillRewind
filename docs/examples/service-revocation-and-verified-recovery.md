# Example: candidate recovery -> replay -> revocation -> rebuild -> attestation (Service mode)

This walks through the Phase C2.3 API surface end to end: recover a hidden
descendant, replay-confirm it, preview a balanced revocation, submit it,
monitor the job, and fetch the resulting signed attestation. Assumes a
running Service-mode API (`skillrewind serve --database-url ...`) with an
API key carrying `ingest`, `read`, `replay`, and `revoke` scopes, and a
configured `attestation_signing_key_path` if signing is requested.

## curl

```bash
BASE=http://127.0.0.1:8000/api/v1
AUTH="Authorization: Bearer $API_KEY"

# 1. Ingest a root skill and a hidden descendant (the descendant's derivation
#    will omit the root from its recorded inputs -- that's what makes the
#    influence "hidden").
ROOT_ID=$(curl -s -X POST "$BASE/artifacts?kind=agent-skill&logical_name=fast-http" \
  -H "$AUTH" --data-binary "root skill body" | python3 -c "import sys,json;print(json.load(sys.stdin)['artifact_id'])")
DESC_ID=$(curl -s -X POST "$BASE/artifacts?kind=agent-skill&logical_name=deploy-service" \
  -H "$AUTH" --data-binary "descendant skill body" | python3 -c "import sys,json;print(json.load(sys.stdin)['artifact_id'])")

DERIV_ID=$(curl -s -X POST "$BASE/derivations" -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"recipe\": \"poisoned-recipe\", \"recipe_version\": \"0.1\", \"payload\": {\"task_snapshot\": {\"root_marker\": \"$ROOT_ID\"}, \"seed\": 1}}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['derivation_id'])")
curl -s -X POST "$BASE/derivations/$DERIV_ID/output" -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"artifact_id\": \"$DESC_ID\"}"

# 2. Confirm recorded closure misses the hidden descendant.
curl -s "$BASE/lineage/$ROOT_ID/descendants" -H "$AUTH"   # does not include DESC_ID

# 3. Candidate recovery.
RUN_ID=$(curl -s -X POST "$BASE/lineage/recovery-runs" -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"root_artifact_id\": \"$ROOT_ID\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['run_id'])")
# ... run a worker (skillrewind worker-once), then:
CANDIDATE_ID=$(curl -s "$BASE/lineage/recovery-runs/$RUN_ID/candidates" -H "$AUTH" \
  | python3 -c "import sys,json;print([c['candidate_id'] for c in json.load(sys.stdin)['items'] if c['candidate_artifact_id']=='$DESC_ID'][0])")

# 4. Replay.
REPLAY_RUN_ID=$(curl -s -X POST "$BASE/replay/runs" -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"candidate_id\": \"$CANDIDATE_ID\", \"repetitions\": 1}" | python3 -c "import sys,json;print(json.load(sys.stdin)['replay_run_id'])")
# ... run a worker, then confirm:
curl -s "$BASE/replay/runs/$REPLAY_RUN_ID" -H "$AUTH"   # verdict: "confirmed"

# 5. Side-effect-free preview.
curl -s -X POST "$BASE/revocations/preview" -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"roots\": [\"$ROOT_ID\"], \"policy\": \"balanced\"}"
# proposed_targets includes ROOT_ID (revoke) and DESC_ID (quarantine, evidence_class=replay-confirmed).

# 6. Submit the revocation with rebuild + verification + signed attestation.
RESP=$(curl -s -X POST "$BASE/revocations" -H "$AUTH" -H "Content-Type: application/json" \
  -H "Idempotency-Key: revoke-fast-http-1" \
  -d "{\"roots\": [\"$ROOT_ID\"], \"reason\": \"unsafe canary rule\", \"severity\": \"high\", \"policy\": \"balanced\", \
       \"rebuild_enabled\": true, \
       \"verification_suite\": {\"suite_id\": \"s\", \"version\": \"0.1.0\", \"canary_keys\": [\"mock_disable_verification\"], \"utility_retention_threshold\": 0.9}, \
       \"attestation_requested\": true, \"sign_requested\": true}")
REVOCATION_ID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['revocation_id'])")
JOB_ID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")

# 7. Monitor: SSE stream, or poll.
curl -s -N "$BASE/events/stream?job_id=$JOB_ID" -H "$AUTH"
# ... run a worker, then:
curl -s "$BASE/revocations/$REVOCATION_ID" -H "$AUTH"   # state: "completed", quarantined/rebuilt populated

# 8. Quarantine and successor resolution.
curl -s "$BASE/quarantine" -H "$AUTH"
curl -s "$BASE/artifacts/$DESC_ID/resolve" -H "$AUTH"    # resolution: "superseded", successor_artifact_id set
SUCCESSOR_ID=$(curl -s "$BASE/artifacts/$DESC_ID/resolve" -H "$AUTH" | python3 -c "import sys,json;print(json.load(sys.stdin)['successor_artifact_id'])")
curl -s "$BASE/artifacts/$SUCCESSOR_ID/resolve" -H "$AUTH"   # resolution: "active"

# 9. Attestation: canonical JSON, Markdown, HTML, and signature verification.
ATTESTATION_ID=$(curl -s "$BASE/revocations/$REVOCATION_ID" -H "$AUTH" | python3 -c "import sys,json;print(json.load(sys.stdin).get('attestation_id'))")
curl -s "$BASE/attestations/$ATTESTATION_ID/canonical" -H "$AUTH"
curl -s "$BASE/attestations/$ATTESTATION_ID/render?format=markdown" -H "$AUTH"
curl -s -X POST "$BASE/attestations/$ATTESTATION_ID/verify" -H "$AUTH"   # digest_valid/signature_valid/ok: true
```

## Notes

- The revocation submission is idempotent on `Idempotency-Key`: resubmitting
  the exact same request with the same key returns the same
  `revocation_id` and creates zero duplicate quarantine/rebuild/verification/
  attestation/audit rows.
- `attestation_requested`/`sign_requested` are processed inside the same
  `revocation.execute` job (checkpoint-safe): a crashed-and-resumed job
  never builds a second attestation or produces a second signature for one
  that already exists.
- A replay-confirmed edge (step 4) never itself changes serving state —
  `GET /artifacts/{id}/resolve` for the descendant still returns `active`
  until the explicit `POST /revocations` in step 6 completes.
