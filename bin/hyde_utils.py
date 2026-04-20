from __future__ import annotations
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_HYDE_SYSTEM = (
    "You rewrite a short question as a first-person passage that might "
    "appear in the user's past chat history as the answer. Write 2-3 "
    "sentences in the user's voice, as if they mentioned the fact in "
    "passing during an unrelated conversation. Include plausible "
    "surrounding detail so the passage sounds conversational, not like a "
    "direct answer. Do not invent specific entities you don't know — use "
    "placeholder phrasing when the fact is genuinely unknown."
)

_HYDE_USER_TEMPLATE = "Question: {question}\n\nPassage:"

async def hyde_expand(client, model: str, question: str, max_tokens: int = 150) -> str:
    """
    Shared primitive: HyDE-style query expansion.
    Generates a hypothetical-answer passage to bridge the query/evidence phrasing gap.
    """
    if not client or not model:
        return ""
    
    t0 = time.perf_counter()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _HYDE_SYSTEM},
                {"role": "user", "content": _HYDE_USER_TEMPLATE.format(question=question)},
            ],
            temperature=0,
            max_tokens=max_tokens,
        )
        passage = (resp.choices[0].message.content or "").strip()
        logger.debug(f"HyDE expansion generated in {time.perf_counter()-t0:.3f}s")
        return passage
    except Exception as e:
        logger.error(f"HyDE expansion failed: {e}")
        return ""
