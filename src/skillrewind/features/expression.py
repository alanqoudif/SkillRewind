"""Expression-trace features: lexical similarity between artifact text content.

Deliberately simple and dependency-free (token/character n-gram Jaccard
similarity). An optional local-embedding adapter is a documented future
extension point (``docs/adr/0008-optional-embeddings.md``) and is not wired
into the default candidate pipeline, which must run fully offline.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import AbstractSet

_TOKEN_RE = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return normalized.casefold()


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(normalize_text(text))


def _ngrams(sequence: list[str], n: int) -> set[tuple[str, ...]]:
    if len(sequence) < n:
        return {tuple(sequence)} if sequence else set()
    return {tuple(sequence[i : i + n]) for i in range(len(sequence) - n + 1)}


def jaccard(a: AbstractSet, b: AbstractSet) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def token_ngram_similarity(text_a: str, text_b: str, *, n: int = 2) -> float:
    tokens_a = tokenize(text_a)
    tokens_b = tokenize(text_b)
    return jaccard(_ngrams(tokens_a, n), _ngrams(tokens_b, n))


def char_ngram_similarity(text_a: str, text_b: str, *, n: int = 4) -> float:
    norm_a = normalize_text(text_a)
    norm_b = normalize_text(text_b)
    grams_a = {norm_a[i : i + n] for i in range(max(0, len(norm_a) - n + 1))}
    grams_b = {norm_b[i : i + n] for i in range(max(0, len(norm_b) - n + 1))}
    return jaccard(grams_a, grams_b)


@dataclass(frozen=True, slots=True)
class ExpressionScore:
    token_ngram: float
    char_ngram: float
    combined: float
    language_hint: str = "unspecified"


def expression_similarity(text_a: str, text_b: str, *, language_hint: str = "unspecified") -> ExpressionScore:
    token_score = token_ngram_similarity(text_a, text_b)
    char_score = char_ngram_similarity(text_a, text_b)
    combined = 0.6 * token_score + 0.4 * char_score
    return ExpressionScore(
        token_ngram=token_score, char_ngram=char_score, combined=combined, language_hint=language_hint
    )
