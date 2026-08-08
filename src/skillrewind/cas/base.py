"""Content-addressed storage protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Iterator, Protocol


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    digest_hex: str
    size_bytes: int


class ContentAddressedStore(Protocol):
    """Protocol implemented by local and remote CAS backends."""

    def put_bytes(self, data: bytes) -> ObjectMetadata: ...

    def put_stream(self, stream: BinaryIO) -> ObjectMetadata: ...

    def exists(self, digest_hex: str) -> bool: ...

    def get_bytes(self, digest_hex: str) -> bytes: ...

    def open_stream(self, digest_hex: str) -> Iterator[bytes]: ...

    def metadata(self, digest_hex: str) -> ObjectMetadata: ...

    def verify_integrity(self, digest_hex: str) -> bool: ...

    def export(self, digest_hex: str, destination: str) -> None: ...
