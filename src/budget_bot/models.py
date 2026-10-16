"""Provider-independent question type and the version-two queue contract."""

from dataclasses import dataclass, field
from typing import Any

MAX_QUESTION = 500


@dataclass(frozen=True)
class Question:
    interaction_id: str
    user_id: str
    text: str = field(repr=False)
    # Discord's interaction token lets the worker edit the reply for 15 minutes. Never log it.
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.interaction_id, str)
            or not self.interaction_id.isdecimal()
            or len(self.interaction_id) > 32
        ):
            raise ValueError("invalid interaction ID")
        if not isinstance(self.user_id, str) or not self.user_id.isdecimal():
            raise ValueError("invalid user ID")
        if not isinstance(self.text, str) or len(self.text) > MAX_QUESTION:
            raise ValueError("invalid text")
        if not isinstance(self.token, str) or not self.token or len(self.token) > 1000:
            raise ValueError("invalid interaction token")

    def to_dict(self) -> dict[str, str]:
        return {
            "interaction_id": self.interaction_id,
            "user_id": self.user_id,
            "text": self.text,
            "token": self.token,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Question:
        if not isinstance(value, dict):
            raise ValueError("queue body must be an object")
        return cls(
            interaction_id=value.get("interaction_id", ""),
            user_id=value.get("user_id", ""),
            text=value.get("text", ""),
            token=value.get("token", ""),
        )
