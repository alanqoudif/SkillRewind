"""Statistical handling of deterministic and repeated stochastic replay runs.

For a single deterministic fixture run, the result is treated as exact
(no interval). For repeated runs (>1 repetition) a paired bootstrap over
per-repetition binary "canary changed" outcomes is used to report an effect
size with an uncertainty interval, without requiring SciPy. Sample sizes
below :data:`MIN_REPETITIONS_FOR_SIGNIFICANCE` are explicitly reported as
insufficient rather than silently treated as significant.
"""

from __future__ import annotations

import random
import statistics as _stats
from dataclasses import dataclass

MIN_REPETITIONS_FOR_SIGNIFICANCE = 5


@dataclass(frozen=True, slots=True)
class PairedResult:
    repetitions: int
    changed_count: int
    effect_size: float
    ci_low: float
    ci_high: float
    sufficient_sample: bool
    excluded: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "repetitions": self.repetitions,
            "changed_count": self.changed_count,
            "effect_size": round(self.effect_size, 6),
            "ci_low": round(self.ci_low, 6),
            "ci_high": round(self.ci_high, 6),
            "sufficient_sample": self.sufficient_sample,
            "excluded": list(self.excluded),
        }


def paired_binary_bootstrap(
    outcomes: list[bool], *, seed: int = 0, resamples: int = 1000
) -> PairedResult:
    """Paired bootstrap over booleans indicating whether the target behavior
    changed between present and withheld in each matched repetition."""

    n = len(outcomes)
    if n == 0:
        return PairedResult(0, 0, 0.0, 0.0, 0.0, sufficient_sample=False)

    changed_count = sum(1 for o in outcomes if o)
    effect = changed_count / n

    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(resamples):
        resampled = [outcomes[rng.randrange(n)] for _ in range(n)]
        samples.append(sum(1 for o in resampled if o) / n)
    samples.sort()
    lo_idx = int(0.025 * len(samples))
    hi_idx = min(int(0.975 * len(samples)), len(samples) - 1)

    return PairedResult(
        repetitions=n,
        changed_count=changed_count,
        effect_size=effect,
        ci_low=samples[lo_idx],
        ci_high=samples[hi_idx],
        sufficient_sample=n >= MIN_REPETITIONS_FOR_SIGNIFICANCE,
    )


def paired_utility_bootstrap(
    deltas: list[float], *, seed: int = 0, resamples: int = 1000
) -> tuple[float, float, float]:
    """Paired bootstrap CI for a continuous utility difference. Returns
    ``(mean_delta, ci_low, ci_high)``."""

    if not deltas:
        return 0.0, 0.0, 0.0
    mean_delta = _stats.fmean(deltas)
    rng = random.Random(seed)
    n = len(deltas)
    samples = [_stats.fmean(deltas[rng.randrange(n)] for _ in range(n)) for _ in range(resamples)]
    samples.sort()
    lo_idx = int(0.025 * len(samples))
    hi_idx = min(int(0.975 * len(samples)), len(samples) - 1)
    return mean_delta, samples[lo_idx], samples[hi_idx]
