"""Local Ed25519 signing and verification for attestations.

Private keys are written with restrictive permissions (0600) and a loud
warning is printed by the CLI layer. Keys are never transmitted or logged.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ..canonical.json import canonical_bytes


@dataclass(frozen=True, slots=True)
class KeyPairPaths:
    private_key_path: Path
    public_key_path: Path


def generate_keypair(output_dir: str | Path) -> KeyPairPaths:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )

    private_path = out / "attestation-ed25519.key"
    public_path = out / "attestation-ed25519.pub"
    private_path.write_bytes(private_bytes)
    os.chmod(private_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    public_path.write_bytes(public_bytes.hex().encode("ascii"))
    return KeyPairPaths(private_key_path=private_path, public_key_path=public_path)


def sign_attestation(attestation: dict[str, Any], private_key_path: str | Path) -> dict[str, Any]:
    private_bytes = Path(private_key_path).read_bytes()
    private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    message = canonical_bytes({k: v for k, v in attestation.items() if k != "signature"})
    signature = private_key.sign(message)
    public_key_hex = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    ).hex()
    signed = dict(attestation)
    signed["signature"] = {
        "algorithm": "ed25519",
        "public_key_hex": public_key_hex,
        "signature_hex": signature.hex(),
    }
    return signed


def verify_signature(attestation: dict[str, Any], public_key_path: str | Path | None = None) -> bool:
    signature_block = attestation.get("signature")
    if not signature_block:
        return False
    if public_key_path is not None:
        pub_text = Path(public_key_path).read_text().strip()
        if pub_text != signature_block["public_key_hex"]:
            return False
    public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(signature_block["public_key_hex"]))
    message = canonical_bytes({k: v for k, v in attestation.items() if k != "signature"})
    try:
        public_key.verify(bytes.fromhex(signature_block["signature_hex"]), message)
        return True
    except InvalidSignature:
        return False
