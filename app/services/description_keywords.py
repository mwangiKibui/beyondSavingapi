"""Category suggestions from meaningful words in a row's description
(ab-290): SALARY -> Salary. Comes after the user's rules, others' picks
and the shared merchant list.

Within a keyword: the user's own choice for it, then what 3+ other users
agreed on (no admin gate, as with shared merchants), then the list's
category. Descriptions are matched in memory on the user's own rows only;
they may hold person names, so they're never stored with a keyword, shared
or sent to the AI. AI suggestions (ab-256) ride on the same list.
"""

from dataclasses import dataclass, field
from uuid import UUID

from app.services.category_suggestions import _compile_pattern
from app.services.merchant_keys import normalize_text

# Users who must file a keyword under the same category name before it's
# suggested to everyone.
SHARED_KEYWORD_MIN_USERS = 3


@dataclass(frozen=True)
class Keyword:
    id: UUID
    pattern: str
    direction: str
    # A category NAME, or for target 'transfer' (ab-285) a transfer reason name.
    category_name: str
    target: str = "category"

    @property
    def word(self) -> str:
        """How the UI names it: Description mentions "salary"."""
        return " ".join(self.pattern.lower().split())


class KeywordList:
    def __init__(self, keywords: list[Keyword]):
        self._by_direction: dict[str, list[tuple[int, object, Keyword]]] = {}
        for k in keywords:
            entry = (len(k.pattern.strip()), _compile_pattern(k.pattern), k)
            self._by_direction.setdefault(k.direction, []).append(entry)
        # Category keywords first, then the longest: income wording beats
        # transfer wording ("RTGS ... SALARY" is Salary, ab-285).
        for entries in self._by_direction.values():
            entries.sort(key=lambda e: (e[2].target != "category", -e[0]))

    @classmethod
    def from_rows(cls, rows: list[dict]) -> "KeywordList":
        return cls([
            Keyword(id=r["id"], pattern=r["pattern"], direction=r["direction"], category_name=r["category_name"],
                    target=r.get("target") or "category")
            for r in rows
        ])

    def match(self, description: str | None, direction: str) -> Keyword | None:
        """The keyword found in the description, whole words only, for the
        row's direction: a category keyword before a transfer one, then the
        longest."""
        text = normalize_text(description)
        if not text:
            return None
        for _, rx, keyword in self._by_direction.get(direction, []):
            if rx.search(text):
                return keyword
        return None

    def matched_ids(self, rows: list[dict]) -> list[UUID]:
        found = {k.id for r in rows if (k := self.match(r["description"], r["direction"]))}
        return sorted(found, key=str)


@dataclass
class KeywordContext:
    """What the suggestion pipeline needs about keywords for one user."""
    keywords: KeywordList = field(default_factory=lambda: KeywordList([]))
    # keyword id -> the user's own category id
    choices: dict[UUID, UUID] = field(default_factory=dict)
    # keyword id -> [{category_name, users}] across all users
    shared: dict[UUID, list[dict]] = field(default_factory=dict)


def pick_shared_keyword(votes: list[dict]) -> str | None:
    """The category name SHARED_KEYWORD_MIN_USERS+ users filed the keyword
    under, if it clearly leads (a tie gives nothing)."""
    ranked = sorted(votes, key=lambda v: v["users"], reverse=True)
    if not ranked or ranked[0]["users"] < SHARED_KEYWORD_MIN_USERS:
        return None
    if len(ranked) > 1 and ranked[1]["users"] == ranked[0]["users"]:
        return None
    return ranked[0]["category_name"]


async def load_keyword_context(conn, *, user_id: UUID, rows: list[dict]) -> KeywordContext:
    """The keyword list plus, for the keywords these rows mention, the
    user's own choices and what users chose overall."""
    from app.repositories.description_keywords import (
        get_active_keywords,
        get_keyword_choices,
        get_shared_keyword_choices,
    )

    keywords = KeywordList.from_rows(await get_active_keywords(conn))
    ids = keywords.matched_ids(rows)
    if not ids:
        return KeywordContext(keywords=keywords)
    return KeywordContext(
        keywords=keywords,
        choices=await get_keyword_choices(conn, user_id=user_id, keyword_ids=ids),
        shared=await get_shared_keyword_choices(conn, keyword_ids=ids),
    )
