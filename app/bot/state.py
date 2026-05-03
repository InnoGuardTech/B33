"""
Tiny in-memory FSM to track multi-step conversations:
  • waiting_email       — user tapped ➕ Add account, now must send email
  • waiting_password    — we have email, need password
  • waiting_qty_custom  — user tapped "custom" qty, now send a number
  • waiting_qty         — booking flow waiting for ticket count
  • waiting_event_link  — user tapped 🔗 send event link
  • waiting_captcha     — captcha screenshot sent, awaiting user answer
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class State:
    name: str
    data: dict[str, Any] = field(default_factory=dict)


_states: dict[str, State] = {}


def set_state(chat_id: str, name: str, **data: Any) -> None:
    _states[str(chat_id)] = State(name=name, data=dict(data))


def get_state(chat_id: str) -> Optional[State]:
    return _states.get(str(chat_id))


def clear_state(chat_id: str) -> None:
    _states.pop(str(chat_id), None)
