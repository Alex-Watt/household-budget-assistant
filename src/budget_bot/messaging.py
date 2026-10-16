"""Question rules: duplicate handling and dry run. Answers come from the injected Responder."""

from budget_bot.config import Settings
from budget_bot.models import Question
from budget_bot.ports import MessagingState, ReplySender, Responder


def process(
    question: Question,
    state: MessagingState,
    replies: ReplySender,
    settings: Settings,
    responder: Responder,
) -> str:
    if question.user_id not in settings.member_ids:
        raise ValueError("unauthorized queue sender")
    lease = state.claim(question.interaction_id)
    if lease is None:
        return "duplicate"
    try:
        if settings.live_messaging:
            answer = responder.answer(question.text, question.user_id, question.interaction_id)
            replies.edit_reply(question.token, answer)
            result = "sent"
        else:
            result = "dry_run"
        # Record completion only after processing. An expired lease can be reclaimed after a crash.
        state.complete(question.interaction_id, lease)
        return result
    except Exception:
        state.release(question.interaction_id, lease)
        raise
