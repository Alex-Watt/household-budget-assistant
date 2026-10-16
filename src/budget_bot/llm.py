"""Model access. The assistant targets the highest Claude Sonnet this AWS account can call.

Bedrock only serves current Claude models through inference profiles, so the adapter lists the
account's Sonnet profiles, orders them by version, and uses the first one that works. When
AWS ships a newer Sonnet, it is picked up automatically. If the newest is not enabled for the
account (Bedrock answers "not available for this account"), the next highest is used instead.
"""

import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from botocore.exceptions import ClientError

# us.anthropic.claude-sonnet-5-5, us.anthropic.claude-sonnet-4-6 or ...sonnet-4-5-20250929-v1:0
SONNET = re.compile(
    r"(?P<scope>us|global)\.anthropic\.claude-sonnet-(?P<major>\d+)(?:-(?P<minor>\d{1,2}))?"
    r"(?:-\d{8}-v\d+:\d+)?"
)
# The model exists but this account cannot use it (yet). Try the next one down.
NOT_USABLE = {"AccessDeniedException", "ResourceNotFoundException"}
REFRESH_SECONDS = 1800
MAX_ATTEMPTS = 5


def _not_usable(error: Mapping[str, Any]) -> bool:
    """Should the next-highest Sonnet be tried instead of failing the question?"""
    code, message = error.get("Code"), str(error.get("Message", ""))
    return bool(
        code in NOT_USABLE or (code == "ValidationException" and "isn't supported" in message)
        # A used-up daily token allowance is a real stop. Falling through to another Sonnet
        # would spend more, so it is not treated as "try the next model". Per-minute throttling
        # is also not matched: that clears by itself.
    )


class LlmUnavailable(RuntimeError):
    """No usable model, or the model returned nothing usable."""


@dataclass(frozen=True)
class ToolCall:
    tool_use_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LlmReply:
    text: str
    tool_calls: tuple[ToolCall, ...]
    # The assistant message in Converse format, to append to the conversation verbatim.
    message: dict[str, Any]
    model_id: str


class LlmClient(Protocol):
    """Conversation in Bedrock Converse format; tools are Converse `toolSpec` wrappers."""

    def converse(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> LlmReply: ...


def sonnet_candidates(profile_ids: Iterable[str], scope: str) -> list[str]:
    """Sonnet inference profiles for one scope, highest version first."""
    found: list[tuple[tuple[int, int], str]] = []
    for profile_id in profile_ids:
        match = SONNET.fullmatch(profile_id)
        if match and match["scope"] == scope:
            found.append(((int(match["major"]), int(match["minor"] or 0)), profile_id))
    return [profile_id for _, profile_id in sorted(found, reverse=True)]


def _parse(response: dict[str, Any], model_id: str) -> LlmReply:
    message = response["output"]["message"]
    blocks = message.get("content", [])
    text = "".join(block["text"] for block in blocks if "text" in block).strip()
    calls = tuple(
        ToolCall(use["toolUseId"], use["name"], use.get("input") or {})
        for use in (block["toolUse"] for block in blocks if "toolUse" in block)
    )
    if response.get("stopReason") == "max_tokens" and not calls:
        raise LlmUnavailable("the model's answer was cut off")
    return LlmReply(text, calls, message, model_id)


class BedrockSonnet:
    def __init__(
        self,
        runtime: Any,
        control: Any,
        scope: str = "us",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runtime = runtime
        self.control = control
        self.scope = scope
        self.clock = clock
        self._candidates: list[str] = []
        self._loaded_at: float | None = None
        self._unusable: set[str] = set()
        self.model_id: str | None = None

    def candidates(self) -> list[str]:
        now = self.clock()
        if self._loaded_at is None or now - self._loaded_at > REFRESH_SECONDS:
            ids: list[str] = []
            token: str | None = None
            while True:
                kwargs: dict[str, Any] = {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000}
                if token:
                    kwargs["nextToken"] = token
                page = self.control.list_inference_profiles(**kwargs)
                ids += [
                    profile["inferenceProfileId"]
                    for profile in page.get("inferenceProfileSummaries", [])
                    if profile.get("status", "ACTIVE") == "ACTIVE"
                ]
                token = page.get("nextToken")
                if not token:
                    break
            self._candidates = sonnet_candidates(ids, self.scope)
            # Access can change (a form submitted, a plan upgraded, a quota raised), so retry
            # denied models and look for the newest one again instead of sticking to a fallback.
            self._unusable.clear()
            self.model_id = None
            self._loaded_at = now
        return self._candidates

    def converse(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> LlmReply:
        candidates = self.candidates()  # may refresh, which forgets the remembered fallback
        order = [self.model_id] if self.model_id else []
        order += [m for m in candidates if m not in self._unusable and m != self.model_id]
        last: ClientError | None = None
        for model_id in order[:MAX_ATTEMPTS]:
            try:
                response = self.runtime.converse(
                    modelId=model_id,
                    system=[{"text": system}],
                    messages=messages,
                    toolConfig={"tools": tools},
                    # Always set: an unset limit reserves the model's whole output quota.
                    inferenceConfig={"maxTokens": max_tokens},
                )
            except ClientError as exc:
                if _not_usable(exc.response.get("Error", {})):
                    self._unusable.add(model_id)
                    if self.model_id == model_id:
                        self.model_id = None
                    last = exc
                    continue
                raise
            self.model_id = model_id
            return _parse(response, model_id)
        raise LlmUnavailable("no Claude Sonnet model is available to this account") from last
