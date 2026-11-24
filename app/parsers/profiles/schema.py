"""ab-312: the statement profile format.

A profile is a statement's LAYOUT - which column is the date, the
description, the money in and out; how dates and amounts are written; the
words that head the table - and NEVER its contents. Profiles are written
by people (the mapping wizard, ab-315), by the AI proposer (ab-314) and by
admins, then shared with every matching upload, so the format is built so
that nobody's data can be stored in one:

- Every model forbids unknown keys; there is no free-text field.
- The only strings are LayoutText (letters and a little punctuation, no
  digits, at most 5 words / 40 characters - headings like "Paid In", never
  amounts, dates, account or phone numbers), strptime date formats built
  from directives and separators only, and 3-letter currency codes.
- No regular expressions: a pattern could embed a value ("^JANE DOE") and
  a careless one could hang the worker. Matching rules are enumerated
  (equals / starts_with / contains); page numbers are a built-in rule.

What syntax can't tell apart - "Jane Doe" vs "Paid In" - is closed by the
wizard's save-time check that no layout_texts() appear among the file's
own descriptions (ab-315).
"""

import json
import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_PROFILE_BYTES = 8 * 1024

_LAYOUT_TEXT = re.compile(r"^[A-Za-z][A-Za-z .:/()&'%-]*$")
_DATE_DIRECTIVES = {"d", "m", "b", "B", "y", "Y", "H", "M", "S", "I", "p"}
_DATE_SEPARATORS = set(" -/.,:")


class InvalidStatementProfile(ValueError):
    """A profile that can't be stored: unknown fields, text that could be
    someone's data, or a layout the reader can't run or check."""


def _layout_text(value: str) -> str:
    value = " ".join(value.split())
    if not 1 <= len(value) <= 40:
        raise ValueError("layout text must be 1-40 characters")
    if len(value.split()) > 5:
        raise ValueError("layout text must be at most 5 words")
    if not _LAYOUT_TEXT.match(value):
        raise ValueError("layout text may only hold letters and simple punctuation (no digits)")
    return value


def _date_format(value: str) -> str:
    if not 1 <= len(value) <= 30:
        raise ValueError("date format must be 1-30 characters")
    seen, i = set(), 0
    while i < len(value):
        ch = value[i]
        if ch == "%":
            if i + 1 >= len(value) or value[i + 1] not in _DATE_DIRECTIVES:
                raise ValueError("date format has an unsupported directive")
            seen.add(value[i + 1])
            i += 2
        elif ch in _DATE_SEPARATORS:
            i += 1
        else:
            raise ValueError("date format may only hold directives and separators")
    if not (seen & {"d"} and seen & {"m", "b", "B"} and seen & {"y", "Y"}):
        raise ValueError("date format needs a day, a month and a year")
    return value


LayoutText = Annotated[str, AfterValidator(_layout_text)]
DateFormat = Annotated[str, AfterValidator(_date_format)]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

Role = Literal["date", "description", "money_in", "money_out", "signed_amount", "balance", "reference", "ignore"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Amounts(_Strict):
    decimal_mark: Literal[".", ","] = "."
    thousands_sep: Literal[",", ".", " ", "'", ""] = ","
    # How a negative amount is written: -1.00, (1.00) or 1.00 DR.
    negative: Literal["minus", "parentheses", "dr_suffix"] = "minus"

    @model_validator(mode="after")
    def _marks_differ(self):
        if self.decimal_mark == self.thousands_sep:
            raise ValueError("decimal mark and thousands separator must differ")
        return self


class Column(_Strict):
    role: Role
    # The heading over the column ("Paid In"), used to find the table and,
    # for ruled/header_gaps bounds, the column. None with explicit bounds.
    header: LayoutText | None = None
    # Explicit bounds, as fractions of the page width.
    x0: float | None = Field(default=None, ge=0, le=1)
    x1: float | None = Field(default=None, ge=0, le=1)


class SkipRow(_Strict):
    match: Literal["equals", "starts_with", "contains"]
    text: LayoutText


class Table(_Strict):
    columns: list[Column] = Field(min_length=2, max_length=12)
    # ruled: the PDF's vertical lines; header_gaps: midway between headings;
    # explicit: each column's x0/x1.
    column_bounds: Literal["ruled", "header_gaps", "explicit"]
    header_lines: int = Field(default=1, ge=1, le=3)
    header_repeats_each_page: bool = True
    # The table ends at a line starting with one of these ("Disclaimer").
    end_markers: list[LayoutText] = Field(default_factory=list, max_length=5)
    skip_rows: list[SkipRow] = Field(default_factory=list, max_length=10)
    # Drop "Page 2 of 5", "2/5" and bare page-number lines.
    skip_page_numbers: bool = True
    # A line with no date continues the row above (wrapped descriptions).
    continuation: Literal["below"] = "below"
    y_tolerance: float = Field(default=3, ge=1, le=6)
    row_order: Literal["oldest_first", "newest_first"] = "oldest_first"


class RowSource(_Strict):
    # first_row: the first transaction's balance minus its amount;
    # table_row: a row whose description starts with `label`.
    source: Literal["first_row", "table_row"] = "first_row"
    label: LayoutText | None = None


class TotalsSource(_Strict):
    # table_row: a "Total" row stating money in and out, checked against
    # the rows' own sums when the table has no balance column.
    source: Literal["none", "table_row"] = "none"
    label: LayoutText | None = None


class StatementProfile(_Strict):
    schema_version: Literal[1] = 1
    file_type: Literal["pdf"] = "pdf"  # ab-319 adds csv / xlsx
    currency: CurrencyCode | None = None  # None: the account's currency
    date_format: DateFormat
    amounts: Amounts = Field(default_factory=Amounts)
    # With a signed amount column: whether a positive amount is money in.
    signed_amount_positive_is: Literal["in", "out"] = "in"
    table: Table
    opening_balance: RowSource = Field(default_factory=RowSource)
    totals: TotalsSource = Field(default_factory=TotalsSource)

    @model_validator(mode="after")
    def _readable_and_checkable(self):
        roles = [c.role for c in self.table.columns]
        count = roles.count
        if count("date") != 1 or count("description") != 1:
            raise ValueError("a profile needs exactly one date and one description column")
        if count("reference") > 1 or count("balance") > 1:
            raise ValueError("at most one reference and one balance column")
        signed, ins, outs = count("signed_amount"), count("money_in"), count("money_out")
        if not ((signed == 1 and ins == outs == 0) or (signed == 0 and ins == outs == 1)):
            raise ValueError("use one signed amount column, or one money in and one money out column")

        if self.table.column_bounds == "explicit":
            previous_end = 0.0
            for column in self.table.columns:
                if column.x0 is None or column.x1 is None or column.x0 >= column.x1:
                    raise ValueError("explicit bounds need x0 < x1 on every column")
                if column.x0 < previous_end - 1e-9:
                    raise ValueError("explicit bounds must be in order and not overlap")
                previous_end = column.x1
        elif any(c.header is None for c in self.table.columns if c.role != "ignore"):
            raise ValueError("every column needs a header unless bounds are explicit")

        if self.opening_balance.source == "table_row" and not self.opening_balance.label:
            raise ValueError("an opening balance row needs a label")
        if self.totals.source == "table_row" and not self.totals.label:
            raise ValueError("a totals row needs a label")
        if count("balance") == 0 and self.totals.source != "table_row":
            raise ValueError("a profile needs a balance column or a totals row to check against")
        return self


def validate_profile(raw: dict) -> StatementProfile:
    """The only way a profile gets in: refuses anything outside the format."""
    try:
        size = len(json.dumps(raw))
    except (TypeError, ValueError):
        raise InvalidStatementProfile("profile must be plain JSON") from None
    if size > MAX_PROFILE_BYTES:
        raise InvalidStatementProfile("profile is too large")
    try:
        return StatementProfile.model_validate(raw)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "profile"
        raise InvalidStatementProfile(f"{where}: {first['msg']}") from None


def layout_texts(profile: StatementProfile) -> list[str]:
    """Every word the profile stores - for ab-315's check that none of them
    is actually one of the file's own descriptions."""
    texts = [c.header for c in profile.table.columns if c.header]
    texts += profile.table.end_markers
    texts += [s.text for s in profile.table.skip_rows]
    texts += [t for t in (profile.opening_balance.label, profile.totals.label) if t]
    return texts
