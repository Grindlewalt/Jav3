"""ask_user: a blocking question to the operator (backend/operator_ask.py)."""
from backend import operator_ask


async def run(questions: list) -> str:
    cleaned = operator_ask.clean_questions(questions)
    if isinstance(cleaned, str):
        return f"error: {cleaned}"
    try:
        got = await operator_ask.ask(cleaned)
    except operator_ask.AskCancelled:
        return "error: the operator stopped this turn before answering"
    return operator_ask.render_result(cleaned, got)
