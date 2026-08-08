"""Unit tests for the Phase 1 foundation layer: canonical JSON, IDs, CAS, audit chain."""

from __future__ import annotations

import io
import sqlite3

import pytest

from skillrewind.audit.log import GENESIS_HASH, AuditLog
from skillrewind.canonical.json import CanonicalizationError, canonical_bytes, canonical_hash, sha256_hex
from skillrewind.cas.local import LocalCAS
from skillrewind.domain.enums import ArtifactKind
from skillrewind.domain.errors import AuditChainError, CASIntegrityError, InvalidArtifactIdError
from skillrewind.domain.ids import build_artifact_id, is_valid_artifact_id, parse_artifact_id

# --- canonical JSON -----------------------------------------------------


def test_canonical_json_sorts_keys_deterministically():
    a = canonical_bytes({"b": 1, "a": 2})
    b = canonical_bytes({"a": 2, "b": 1})
    assert a == b
    assert a == b'{"a":2,"b":1}'


def test_canonical_json_serializes_enum_by_value():
    assert canonical_bytes({"kind": ArtifactKind.AGENT_SKILL}) == b'{"kind":"agent-skill"}'


def test_canonical_json_rejects_nan_and_infinity():
    with pytest.raises(CanonicalizationError):
        canonical_bytes({"x": float("nan")})
    with pytest.raises(CanonicalizationError):
        canonical_bytes({"x": float("inf")})


def test_canonical_json_rejects_raw_bytes():
    with pytest.raises(CanonicalizationError):
        canonical_bytes({"x": b"raw"})


def test_canonical_hash_is_deterministic_and_content_sensitive():
    h1 = canonical_hash({"a": 1, "b": 2})
    h2 = canonical_hash({"b": 2, "a": 1})
    h3 = canonical_hash({"a": 1, "b": 3})
    assert h1 == h2
    assert h1 != h3
    assert h1.startswith("sha256:")


def test_sha256_hex_matches_manual_computation():
    import hashlib

    expected = hashlib.sha256(b'{"a":1}').hexdigest()
    assert sha256_hex({"a": 1}) == expected


# --- artifact IDs --------------------------------------------------------


def test_build_and_parse_artifact_id_roundtrip():
    digest = "9" * 64
    artifact_id = build_artifact_id("skill", "deploy-fastapi", digest)
    assert artifact_id == f"skill://deploy-fastapi@sha256:{digest}"
    scheme, name, parsed_digest = parse_artifact_id(artifact_id)
    assert (scheme, name, parsed_digest) == ("skill", "deploy-fastapi", digest)


def test_build_artifact_id_rejects_short_digest():
    with pytest.raises(InvalidArtifactIdError):
        build_artifact_id("skill", "x", "deadbeef")


def test_parse_artifact_id_rejects_malformed_without_legacy():
    with pytest.raises(InvalidArtifactIdError):
        parse_artifact_id("skill://x@sha256:short")
    assert not is_valid_artifact_id("skill://x@sha256:short")


def test_legacy_mode_accepts_short_demo_ids():
    assert is_valid_artifact_id("skill://legacy-demo", allow_legacy=True)
    assert not is_valid_artifact_id("skill://legacy-demo", allow_legacy=False)


# --- local CAS -----------------------------------------------------------


def test_cas_put_get_roundtrip_and_dedup(tmp_path):
    cas = LocalCAS(tmp_path / "cas")
    meta1 = cas.put_bytes(b"hello world")
    meta2 = cas.put_bytes(b"hello world")
    assert meta1.digest_hex == meta2.digest_hex
    assert cas.get_bytes(meta1.digest_hex) == b"hello world"
    assert cas.exists(meta1.digest_hex)
    assert cas.verify_integrity(meta1.digest_hex)


def test_cas_put_stream_matches_put_bytes(tmp_path):
    cas = LocalCAS(tmp_path / "cas")
    data = b"x" * (1024 * 1024 + 17)
    meta_bytes = cas.put_bytes(data)
    meta_stream = cas.put_stream(io.BytesIO(data))
    assert meta_bytes.digest_hex == meta_stream.digest_hex
    assert meta_bytes.size_bytes == meta_stream.size_bytes == len(data)


def test_cas_detects_corruption(tmp_path):
    cas = LocalCAS(tmp_path / "cas")
    meta = cas.put_bytes(b"integrity check")
    path = cas._path_for(meta.digest_hex)
    path.write_bytes(b"corrupted!!")
    assert not cas.verify_integrity(meta.digest_hex)


def test_cas_rejects_oversized_object(tmp_path):
    cas = LocalCAS(tmp_path / "cas", max_object_bytes=10)
    with pytest.raises(CASIntegrityError):
        cas.put_bytes(b"this is definitely more than ten bytes")


def test_cas_missing_object_raises(tmp_path):
    cas = LocalCAS(tmp_path / "cas")
    with pytest.raises(CASIntegrityError):
        cas.get_bytes("0" * 64)


def test_cas_export(tmp_path):
    cas = LocalCAS(tmp_path / "cas")
    meta = cas.put_bytes(b"export me")
    dest = tmp_path / "exported.bin"
    cas.export(meta.digest_hex, str(dest))
    assert dest.read_bytes() == b"export me"


# --- audit log -------------------------------------------------------------


def _new_log() -> AuditLog:
    conn = sqlite3.connect(":memory:")
    return AuditLog(conn)


def test_audit_log_genesis_and_chain():
    log = _new_log()
    assert log.head_hash() == GENESIS_HASH
    e1 = log.append("artifact.ingested", "tester", {"artifact_id": "a1"})
    e2 = log.append("artifact.ingested", "tester", {"artifact_id": "a2"})
    assert e1.prev_hash == GENESIS_HASH
    assert e2.prev_hash == e1.event_hash
    assert log.head_hash() == e2.event_hash
    result = log.verify()
    assert result.ok
    assert result.checked == 2


def test_audit_log_detects_payload_tampering():
    log = _new_log()
    log.append("revocation.requested", "tester", {"root": "skill://x"})
    log.append("revocation.barrier-applied", "tester", {"root": "skill://x"})
    log._conn.execute(
        "UPDATE audit_log SET payload_json = ? WHERE sequence = 1", ('{"root":"skill://tampered"}',)
    )
    log._conn.commit()
    result = log.verify()
    assert not result.ok
    with pytest.raises(AuditChainError):
        result.raise_if_invalid()


def test_audit_log_detects_deletion():
    log = _new_log()
    log.append("a", "tester", {})
    log.append("b", "tester", {})
    log.append("c", "tester", {})
    log._conn.execute("DELETE FROM audit_log WHERE sequence = 2")
    log._conn.commit()
    result = log.verify()
    assert not result.ok


def test_audit_log_export_writes_jsonl(tmp_path):
    log = _new_log()
    log.append("a", "tester", {"x": 1})
    out = tmp_path / "audit.jsonl"
    log.export(out)
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert '"event_type":"a"' in lines[0]
