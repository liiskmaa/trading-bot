"""
Bot-level state machine.

Transitions:
  STARTING → RUNNING          (grid built, WS connected)
  RUNNING  → PAUSED           (AI: trending/high_volatility)
  PAUSED   → RUNNING          (AI: ranging)
  RUNNING  → COOLDOWN         (risk: consecutive losses)
  COOLDOWN → RUNNING          (cooldown expired)
  *        → EMERGENCY_STOP   (drawdown limit / price spike)
  *        → STOPPING         (graceful shutdown requested)
"""

from enum import Enum


class BotState(Enum):
    STARTING       = "starting"
    RUNNING        = "running"
    PAUSED         = "paused"        # AI says market is unfavourable
    COOLDOWN       = "cooldown"      # Risk: consecutive losses
    EMERGENCY_STOP = "emergency_stop"
    STOPPING       = "stopping"
