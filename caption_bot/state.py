from collections import defaultdict
from .models import UserState

_states: dict[int, UserState] = defaultdict(UserState)


def get_state(user_id: int) -> UserState:
    return _states[user_id]


def reset_state(user_id: int) -> None:
    _states.pop(user_id, None)
