"""Portable normalized float32 BLOB codec and bounded cosine math."""

from __future__ import annotations

import math
import struct
from operator import mul

from yuki.memory.embedding.models import EmbeddingVector


class Float32VectorCodec:
    """Encode normalized dense vectors as stable little-endian IEEE float32."""

    @staticmethod
    def normalize(vector: EmbeddingVector) -> EmbeddingVector:
        norm = math.sqrt(math.fsum(map(mul, vector.values, vector.values)))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("embedding vector cannot be normalized")
        return EmbeddingVector(
            values=tuple(value / norm for value in vector.values),
            dimensions=vector.dimensions,
        )

    def encode(self, vector: EmbeddingVector) -> bytes:
        normalized = self.normalize(vector)
        return struct.pack(f"<{normalized.dimensions}f", *normalized.values)

    def decode(self, payload: bytes, *, dimensions: int) -> EmbeddingVector:
        if dimensions <= 0 or len(payload) != dimensions * 4:
            raise ValueError("invalid embedding vector byte length")
        values = struct.unpack(f"<{dimensions}f", payload)
        vector = EmbeddingVector(values=values, dimensions=dimensions)
        norm = math.sqrt(math.fsum(map(mul, values, values)))
        if not math.isclose(norm, 1.0, rel_tol=1e-5, abs_tol=1e-5):
            raise ValueError("stored embedding vector is not normalized")
        return vector

    def decode_normalized(self, payload: bytes, *, dimensions: int) -> EmbeddingVector:
        """Validate the stored float32 norm before reusing it for normalization."""
        if dimensions <= 0 or len(payload) != dimensions * 4:
            raise ValueError("invalid embedding vector byte length")
        values = struct.unpack(f"<{dimensions}f", payload)
        norm = math.sqrt(math.fsum(map(mul, values, values)))
        if not math.isclose(norm, 1.0, rel_tol=1e-5, abs_tol=1e-5):
            raise ValueError("stored embedding vector is not normalized")
        # A finite norm near one proves finite, nonzero raw float32 values before
        # division. Construct the validated vector only once, after scaling.
        return EmbeddingVector(
            values=tuple(value / norm for value in values), dimensions=dimensions
        )

    @staticmethod
    def dot(left: EmbeddingVector, right: EmbeddingVector) -> float:
        if left.dimensions != right.dimensions:
            raise ValueError("embedding vector dimensions differ")
        left_norm = Float32VectorCodec.normalize(left)
        right_norm = Float32VectorCodec.normalize(right)
        return Float32VectorCodec.dot_normalized(left_norm, right_norm)

    @staticmethod
    def dot_normalized(left: EmbeddingVector, right: EmbeddingVector) -> float:
        """Score vectors already normalized by this codec in the current scan."""
        if left.dimensions != right.dimensions:
            raise ValueError("embedding vector dimensions differ")
        score = math.fsum(map(mul, left.values, right.values))
        return max(-1.0, min(1.0, score))
