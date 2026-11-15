"""Job queue lanes for the self-hosted app: a single normal lane.

The hosted edition split free users into lanes by country and load. This distribution has no
accounts or plans, so every job sits in the normal lane at the priority it was given, and the
saturation policy is always off. The names match what app.py's queue code asks for.
"""
NORMAL = "normal"
SLOW = "slow"
PAID_PRIORITY = 1
FREE_PRIORITY = 2
SLOW_PRIORITY = 3


def enabled() -> bool:
    return False


def killed() -> bool:
    return True


def slow_countries() -> frozenset:
    return frozenset()


def slow_lane_on() -> bool:
    return False


def limits() -> dict:
    return {}


def request_country(headers, client_ip=None) -> str:
    return ""


def lane_for(plan, country) -> str:
    return NORMAL


def priority_for(plan_priority: int, lane: str) -> int:
    return int(plan_priority)


def lane_of_priority(priority) -> str:
    return NORMAL


def effective_priority(priority) -> int:
    """A job left at the old slow priority (e.g. from an old manifest) queues with the free ones."""
    return min(int(priority), FREE_PRIORITY) if int(priority) >= SLOW_PRIORITY else int(priority)


def recent_first(lane) -> bool:
    return False


def brake_active() -> bool:
    return False


def note_rejected() -> None:
    return None


def enabled_since():
    return None


def forget_activation(state_dir) -> None:
    return None


def register_on_change(callback) -> None:
    """No lane state ever changes here, so the callback is never needed."""
    return None
