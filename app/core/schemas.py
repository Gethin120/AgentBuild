from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, TypedDict


StrategyType = Literal["balanced", "fast_arrival", "min_wait", "min_detour"]


class RetryPolicy(TypedDict):
    max_attempts: int
    backoff_sec: float
    planner_timeout_sec: int
    planner_max_retries: int


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


@dataclass
class SessionMemory:
    intents: List[Dict[str, Any]] = field(default_factory=list)
    strategies: List[StrategyPlan] = field(default_factory=list)
    judge_results: List[JudgeResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class UserMemory:
    user_id: str = "default"
    preferred_wait_max_min: Optional[int] = None
    preferred_detour_max_min: Optional[int] = None
    preferred_strategy: Optional[StrategyType] = None

