"""Per-user state.

`defaultdict` used to create — and keep forever — a UserState for every
Telegram ID that ever touched the bot. `release_if_idle()` lets handlers
hand back a state that turned out to hold nothing, so long-running
instances do not accumulate empty objects.
"""

from .models import UserState

_states: dict[int, UserState] = {}


def get_state(user_id: int) -> UserState:
    state = _states.get(user_id)

    if state is None:
        state = UserState()
        _states[user_id] = state

    return state


def peek_state(user_id: int) -> UserState | None:
    """Return an existing state without creating one."""
    return _states.get(user_id)


def reset_state(user_id: int) -> None:
    _states.pop(user_id, None)


def release_if_idle(user_id: int) -> None:
    """Drop a state that holds no batch, caption, cover or pending work."""
    state = _states.get(user_id)

    if state is not None and state.is_idle():
        del _states[user_id]


def state_count() -> int:
    return len(_states)
