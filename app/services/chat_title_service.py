from __future__ import annotations

import re

from groq import AsyncGroq

from app.core.config import settings


class ChatTitleService:
    MAX_LENGTH = 60

    def __init__(self) -> None:
        self._client: AsyncGroq | None = None

    async def generate(self, question: str) -> str:
        fallback = self._fallback_title(question)
        if fallback == "New AWS Chat" or not settings.groq_api_key:
            return fallback

        if self._client is None:
            self._client = AsyncGroq(api_key=settings.groq_api_key)

        try:
            response = await self._client.chat.completions.create(
                model=settings.groq_intent_model,
                temperature=0,
                max_tokens=32,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Create a concise chat title of 3 to 6 words from the user's "
                            "question. Preserve AWS service names and acronyms accurately. "
                            "Return only the title; do not add quotes or a period."
                        ),
                    },
                    {"role": "user", "content": question.strip()},
                ],
            )
            content = response.choices[0].message.content or ""
            title = " ".join(content.strip().strip("\"'`").split())
            title = re.sub(r"^title:\s*", "", title, flags=re.IGNORECASE)
            return title[:self.MAX_LENGTH].rstrip() or fallback
        except Exception:
            return fallback

    @classmethod
    def _fallback_title(cls, question: str) -> str:
        value = " ".join(question.strip().split())
        if not value:
            return "New AWS Chat"

        return value[:cls.MAX_LENGTH].rstrip()
