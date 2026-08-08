"""Attestation generation, rendering, signing, and verification.

``recorded_attestation`` is the original v0.1 recorded-only attestation
function, preserved unmodified for backward compatibility (``skillrewind
attest --edges ...``). The v0.2 bounded revocation attestation builder is
:func:`build_attestation`.
"""

from .builder import ATTESTATION_SCHEMA_VERSION, build_attestation
from .legacy import recorded_attestation
from .render import render_html, render_markdown
from .signing import generate_keypair, sign_attestation, verify_signature
from .verify import VerificationOutcome, verify_attestation

__all__ = [
    "recorded_attestation",
    "build_attestation",
    "ATTESTATION_SCHEMA_VERSION",
    "render_markdown",
    "render_html",
    "generate_keypair",
    "sign_attestation",
    "verify_signature",
    "verify_attestation",
    "VerificationOutcome",
]
