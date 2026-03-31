from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, TypedDict


StrategyType = Literal["balanced", "fast_arrival", "min_wait", "min_detour"]
PreferenceType = Literal[
    "balanced",
    "fast_arrival",
    "min_wait",
    "min_detour",
    "low_transfer",
    "balanced_fairness",
]


class RetryPolicy(TypedDict):
    max_attempts: int
    backoff_sec: float
    planner_timeout_sec: int
    planner_max_retries: int


class StrategyPolicy(TypedDict):
    step_wait_min: int
    step_detour_min: int
    step_passenger_travel_min: int
    base_auto_radius_m: int
    auto_radius_step_m: int
    base_auto_limit: int
    auto_limit_step: int
    default_keywords: str


class JudgePolicy(TypedDict):
    max_avg_wait_min: int
    max_avg_detour_min: int
    min_options_required: int


class StrategyPlan(TypedDict):
    strategy_type: StrategyType
    reason: str
    constraint_adjustments: Dict[str, int]
    auto_pickup_adjustments: Dict[str, Any]


class JudgeResult(TypedDict):
    pass_: bool
    reason: str
    score: float
    risks: List[str]


class FeedbackSignal(TypedDict, total=False):
    kind: str
    value: str
    strength: str


class FeedbackEvent(TypedDict, total=False):
    type: str
    target_option: str
    signals: List[FeedbackSignal]
    reason: str


@dataclass
class SessionMemory:
    intents: List[Dict[str, Any]] = field(default_factory=list)
    strategies: List[StrategyPlan] = field(default_factory=list)
    judge_results: List[JudgeResult] = field(default_factory=list)
    feedback_events: List[FeedbackEvent] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class UserMemory:
    user_id: str = "default"
    preferred_wait_max_min: Optional[int] = None
    preferred_detour_max_min: Optional[int] = None
    preferred_strategy: Optional[StrategyType] = None
    preferred_overrides: List[PreferenceType] = field(default_factory=list)
