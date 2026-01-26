"""
Risk management package.

Phase 5 introduces a risk layer that validates and sizes trades before they are
sent to an executor.
"""

from .circuit_breaker import CircuitBreaker, CircuitBreakerStatus, CircuitState
from .exposure_monitor import ExposureCheckResult, ExposureConfig, ExposureMonitor
from .position_sizer import EdgeEstimate, KellyPositionSizer, PositionSizeResult
from .risk_manager import RiskConfig, RiskDecision, RiskManager

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerStatus",
    "CircuitState",
    "ExposureCheckResult",
    "ExposureConfig",
    "ExposureMonitor",
    "EdgeEstimate",
    "KellyPositionSizer",
    "PositionSizeResult",
    "RiskConfig",
    "RiskDecision",
    "RiskManager",
]

