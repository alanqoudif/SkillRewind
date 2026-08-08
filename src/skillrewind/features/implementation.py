"""Implementation-trace features: static structural similarity for Python code.

Extracts a normalized signature (import names, fully-qualified call names,
and a coarse control-flow shape) that survives identifier renaming and
paraphrasing but tracks actual operations performed. Code is never executed
to extract these features (``ast.parse`` only).

Non-Python or unparsable content falls back to a generic token-level
similarity so the pipeline degrades gracefully rather than erroring.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from .expression import jaccard, token_ngram_similarity

_SECRET_LITERAL_RE = re.compile(r"(?i)(key|token|secret|password)")


def _qualified_call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        parts = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    if isinstance(node, ast.Name):
        return node.id
    return None


@dataclass(frozen=True, slots=True)
class ImplementationSignature:
    imports: frozenset[str]
    calls: frozenset[str]
    control_flow_shape: tuple[str, ...]
    literal_fingerprints: frozenset[str]
    parsed: bool


def extract_python_signature(source: str) -> ImplementationSignature:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ImplementationSignature(frozenset(), frozenset(), (), frozenset(), parsed=False)

    imports: set[str] = set()
    calls: set[str] = set()
    control_flow: list[str] = []
    literals: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.update(f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            name = _qualified_call_name(node.func)
            if name:
                calls.add(name)
        elif isinstance(node, (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.FunctionDef, ast.ClassDef)):
            control_flow.append(type(node).__name__)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and _SECRET_LITERAL_RE.search(node.value):
                literals.add("[REDACTED-LITERAL]")
            elif isinstance(node.value, (str, int, float, bool)):
                literals.add(f"{type(node.value).__name__}:{str(node.value)[:32]}")

    return ImplementationSignature(
        imports=frozenset(imports),
        calls=frozenset(calls),
        control_flow_shape=tuple(control_flow),
        literal_fingerprints=frozenset(literals),
        parsed=True,
    )


@dataclass(frozen=True, slots=True)
class ImplementationScore:
    import_overlap: float
    call_overlap: float
    control_flow_similarity: float
    literal_overlap: float
    combined: float
    used_ast: bool


def _sequence_similarity(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    if not a and not b:
        return 0.0
    return jaccard(set(a), set(b))


def implementation_similarity(source_a: str, source_b: str) -> ImplementationScore:
    sig_a = extract_python_signature(source_a)
    sig_b = extract_python_signature(source_b)

    if not sig_a.parsed or not sig_b.parsed:
        fallback = token_ngram_similarity(source_a, source_b)
        return ImplementationScore(0.0, 0.0, 0.0, 0.0, combined=fallback, used_ast=False)

    import_overlap = jaccard(sig_a.imports, sig_b.imports)
    call_overlap = jaccard(sig_a.calls, sig_b.calls)
    flow_similarity = _sequence_similarity(sig_a.control_flow_shape, sig_b.control_flow_shape)
    literal_overlap = jaccard(sig_a.literal_fingerprints, sig_b.literal_fingerprints)
    combined = 0.3 * import_overlap + 0.4 * call_overlap + 0.2 * flow_similarity + 0.1 * literal_overlap
    return ImplementationScore(
        import_overlap=import_overlap,
        call_overlap=call_overlap,
        control_flow_similarity=flow_similarity,
        literal_overlap=literal_overlap,
        combined=combined,
        used_ast=True,
    )
