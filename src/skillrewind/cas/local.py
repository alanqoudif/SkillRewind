"""Local filesystem content-addressed store.

Objects are stored under ``<root>/objects/<aa>/<bb>/<digest-hex>`` (git-style
sharding). Writes are atomic: content is hashed while streaming to a temp
file in the same filesystem, then moved into place with ``os.replace``.
Identical content is deduplicated by digest. Reads can optionally re-verify
the digest.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterator

from ..domain.errors import CASIntegrityError, CASPathTraversalError
from .base import ObjectMetadata

_CHUNK = 1024 * 1024
_HEX = set("0123456789abcdef")


def _validate_digest(digest_hex: str) -> str:
    digest_hex = digest_hex.lower()
    if len(digest_hex) != 64 or any(ch not in _HEX for ch in digest_hex):
        raise CASIntegrityError(f"invalid sha256 digest: {digest_hex!r}")
    return digest_hex


class LocalCAS:
    def __init__(self, root: str | Path, *, max_object_bytes: int = 200 * 1024 * 1024) -> None:
        self.root = Path(root).resolve()
        self.objects_dir = self.root / "objects"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.max_object_bytes = max_object_bytes

    def _path_for(self, digest_hex: str) -> Path:
        digest_hex = _validate_digest(digest_hex)
        shard_a, shard_b = digest_hex[:2], digest_hex[2:4]
        path = (self.objects_dir / shard_a / shard_b / digest_hex).resolve()
        # Defense in depth: the digest is validated as pure hex above, so a
        # traversal escape should be structurally impossible, but we still
        # assert containment before ever touching the filesystem.
        if self.objects_dir not in path.parents:
            raise CASPathTraversalError(f"resolved object path escapes CAS root: {path}")
        return path

    def put_bytes(self, data: bytes) -> ObjectMetadata:
        if len(data) > self.max_object_bytes:
            raise CASIntegrityError(
                f"object of {len(data)} bytes exceeds max_object_bytes={self.max_object_bytes}"
            )
        digest_hex = hashlib.sha256(data).hexdigest()
        path = self._path_for(digest_hex)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.replace(tmp_name, path)
            finally:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
        return ObjectMetadata(digest_hex=digest_hex, size_bytes=len(data))

    def put_stream(self, stream: BinaryIO) -> ObjectMetadata:
        hasher = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(dir=str(self.objects_dir), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_object_bytes:
                        raise CASIntegrityError(
                            f"stream exceeds max_object_bytes={self.max_object_bytes}"
                        )
                    hasher.update(chunk)
                    handle.write(chunk)
            digest_hex = hasher.hexdigest()
            final_path = self._path_for(digest_hex)
            if final_path.exists():
                os.remove(tmp_name)
            else:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_name, final_path)
            return ObjectMetadata(digest_hex=digest_hex, size_bytes=size)
        finally:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)

    def exists(self, digest_hex: str) -> bool:
        return self._path_for(digest_hex).is_file()

    def get_bytes(self, digest_hex: str) -> bytes:
        path = self._path_for(digest_hex)
        if not path.is_file():
            raise CASIntegrityError(f"object not found: sha256:{digest_hex}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest_hex:
            raise CASIntegrityError(f"CAS object corrupted (digest mismatch): sha256:{digest_hex}")
        return data

    def open_stream(self, digest_hex: str) -> Iterator[bytes]:
        path = self._path_for(digest_hex)
        if not path.is_file():
            raise CASIntegrityError(f"object not found: sha256:{digest_hex}")
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
                yield chunk
        if hasher.hexdigest() != digest_hex:
            raise CASIntegrityError(f"CAS object corrupted (digest mismatch): sha256:{digest_hex}")

    def metadata(self, digest_hex: str) -> ObjectMetadata:
        path = self._path_for(digest_hex)
        if not path.is_file():
            raise CASIntegrityError(f"object not found: sha256:{digest_hex}")
        return ObjectMetadata(digest_hex=digest_hex, size_bytes=path.stat().st_size)

    def verify_integrity(self, digest_hex: str) -> bool:
        path = self._path_for(digest_hex)
        if not path.is_file():
            return False
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest() == digest_hex

    def export(self, digest_hex: str, destination: str) -> None:
        path = self._path_for(digest_hex)
        if not path.is_file():
            raise CASIntegrityError(f"object not found: sha256:{digest_hex}")
        shutil.copyfile(path, destination)

    def iter_all_digests(self) -> Iterator[str]:
        for shard_a in sorted(self.objects_dir.glob("*")):
            for shard_b in sorted(shard_a.glob("*")):
                for obj in sorted(shard_b.glob("*")):
                    if not obj.name.startswith(".tmp-"):
                        yield obj.name
