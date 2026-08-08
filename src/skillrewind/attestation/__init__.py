"""Attestation generation, rendering, signing, and verification.

``recorded_attestation`` is the original v0.1 recorded-only attestation
function, preserved unmodified for backward compatibility (``skillrewind
attest --edges ...``). The v0.2 bounded revocation attestation builder lives
in :mod:`skillrewind.attestation.builder`.
"""

from .legacy import recorded_attestation

__all__ = ["recorded_attestation"]
