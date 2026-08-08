"""Metrics computed from raw predictions vs. the (scorer-only) oracle."""

from __future__ import annotations

from dataclasses import dataclass

from .cases import BenchmarkCase
from .harness import Prediction


@dataclass(frozen=True, slots=True)
class CaseScore:
    case_id: str
    method: str
    true_positives: int
    false_positives: int
    false_negatives: int
    false_quarantined_negatives: int  # strict negatives incorrectly predicted as hidden descendants
    replay_calls: int

    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else (1.0 if self.true_positives == 0 else 0.0)

    def f1(self) -> float:
        p, r = self.precision(), self.recall()
        return 2 * p * r / (p + r) if (p + r) else 0.0


def score_prediction(case: BenchmarkCase, prediction: Prediction) -> CaseScore:
    expected = set(case.expected_hidden_descendants)
    predicted = set(prediction.predicted_hidden_descendants)
    tp = len(expected & predicted)
    fp = len(predicted - expected)
    fn = len(expected - predicted)
    false_quarantined = len(predicted & set(case.strict_negatives))
    return CaseScore(
        case_id=case.case_id, method=prediction.method,
        true_positives=tp, false_positives=fp, false_negatives=fn,
        false_quarantined_negatives=false_quarantined, replay_calls=prediction.replay_calls,
    )


@dataclass(frozen=True, slots=True)
class AggregateScore:
    method: str
    n_cases: int
    micro_precision: float
    micro_recall: float
    micro_f1: float
    false_quarantine_rate: float
    total_replay_calls: int
    sufficient_for_confidence_intervals: bool

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "n_cases": self.n_cases,
            "micro_precision": round(self.micro_precision, 4),
            "micro_recall": round(self.micro_recall, 4),
            "micro_f1": round(self.micro_f1, 4),
            "false_quarantine_rate": round(self.false_quarantine_rate, 4),
            "total_replay_calls": self.total_replay_calls,
            "sufficient_for_confidence_intervals": self.sufficient_for_confidence_intervals,
        }


MIN_CASES_FOR_CI = 30


def aggregate(scores: list[CaseScore], *, method: str) -> AggregateScore:
    relevant = [s for s in scores if s.method == method]
    tp = sum(s.true_positives for s in relevant)
    fp = sum(s.false_positives for s in relevant)
    fn = sum(s.false_negatives for s in relevant)
    fq = sum(s.false_quarantined_negatives for s in relevant)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else (1.0 if tp == 0 else 0.0)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return AggregateScore(
        method=method, n_cases=len(relevant), micro_precision=precision, micro_recall=recall, micro_f1=f1,
        false_quarantine_rate=fq / len(relevant) if relevant else 0.0,
        total_replay_calls=sum(s.replay_calls for s in relevant),
        sufficient_for_confidence_intervals=len(relevant) >= MIN_CASES_FOR_CI,
    )
