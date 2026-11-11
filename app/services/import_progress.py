"""Saved progress for an unfinished import review (ab-265).

Only what the user did is saved - picks, "Who is this?" labels, Paybill
entries, rows they un-labelled. Suggestions are never saved; the wizard
re-reads them so a resumed review shows today's suggestions under the
user's own picks. Never holds statement passwords or statement text."""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.services.category_suggestions import _is_open, group_key
from app.services.merchant_keys import RowKey

MAX_ROWS = 5000

Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
Paybill = Annotated[str, StringConstraints(pattern=r"^[0-9]{5,8}$")]


class DraftRow(BaseModel):
    # Same shape rules as confirm: at most one of category_id /
    # new_category_name. unlabelled = "Choose a category instead" on a
    # loan/overdraft label (ab-260), possibly before a category is picked.
    category_id: UUID | None = None
    new_category_name: str | None = None
    unlabelled: bool = False

    @field_validator("new_category_name", mode="before")
    @classmethod
    def _strip(cls, value):
        if isinstance(value, str):
            value = value.strip() or None
        return value

    @field_validator("new_category_name")
    @classmethod
    def _name_length(cls, value):
        if value is not None and len(value) > 60:
            raise ValueError("Category name must be 60 characters or fewer")
        return value

    @model_validator(mode="after")
    def _one_choice(self):
        if self.category_id is not None and self.new_category_name is not None:
            raise ValueError("A row can't have both category_id and new_category_name")
        if self.category_id is None and self.new_category_name is None and not self.unlabelled:
            raise ValueError("A saved row needs a category, a new category name, or unlabelled")
        return self


class Draft(BaseModel):
    rows: dict[UUID, DraftRow] = Field(default_factory=dict)
    # Keyed by the preview's payee key (group_key, or "row:<id>").
    labels: dict[str, Label] = Field(default_factory=dict)
    paybills: dict[str, Paybill] = Field(default_factory=dict)

    @field_validator("rows")
    @classmethod
    def _max_rows(cls, value):
        if len(value) > MAX_ROWS:
            raise ValueError(f"At most {MAX_ROWS} rows")
        return value


def payee_keys(rows: list[dict], keys: dict[UUID, RowKey]) -> set[str]:
    """The payee keys the review screen uses (importReviewState.payeeKey)."""
    out = set()
    for row in rows:
        out.add(group_key(keys[row["id"]], row["direction"]) or f"row:{row['id']}")
    return out


def progress_counts(rows: list[dict], keys: dict[UUID, RowKey], draft: dict) -> tuple[int, int]:
    """(rows with a pick, rows needing a pick). Rows already allocated or
    tagged as transfers since don't count; loan/overdraft labels only
    count once the user un-labels them."""
    entries = draft.get("rows", {})
    reviewed = total = 0
    for row in rows:
        if not _is_open(row):
            continue
        entry = entries.get(row["id"]) or {}
        if keys[row["id"]].row_kind is not None and not entry.get("unlabelled"):
            continue
        total += 1
        if entry.get("category_id") or entry.get("new_category_name"):
            reviewed += 1
    return reviewed, total
