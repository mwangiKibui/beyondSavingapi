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


# ab-337: for checking a text (an AI-named footer) before it goes into a profile.
layout_text = _layout_text
LayoutText = Annotated[str, AfterValidator(_layout_text)]
DateFormat = Annotated[str, AfterValidator(_date_format)]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

Role = Literal[
    "date", "description", "money_in", "money_out", "signed_amount", "balance", "reference", "status", "ignore"
]


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
    # explicit: each column's x0/x1; cells: a CSV/XLSX file's own columns,
    # found by their heading (ab-319).
    column_bounds: Literal["ruled", "header_gaps", "explicit", "cells"]
    # XLSX: which sheet (0 = the first).
    sheet: int = Field(default=0, ge=0, le=20)
    header_lines: int = Field(default=1, ge=1, le=3)
    header_repeats_each_page: bool = True
    # The table ends at a line starting with one of these ("Disclaimer").
    end_markers: list[LayoutText] = Field(default_factory=list, max_length=5)
    skip_rows: list[SkipRow] = Field(default_factory=list, max_length=10)
    # Drop "Page 2 of 5", "2/5" and bare page-number lines.
    skip_page_numbers: bool = True
    # A line with no date continues a row's description: the row above
    # ("below"), or the nearest row on the page, above or below, the
    # earlier on a tie ("nearest" - Equity prints details around its
    # amounts line, ab-320), or the row below ("above" - the text sits
    # over its date line). ab-333: tried automatically when a read's
    # descriptions don't add up.
    continuation: Literal["below", "nearest", "above"] = "below"
    y_tolerance: float = Field(default=3, ge=1, le=6)
    row_order: Literal["oldest_first", "newest_first"] = "oldest_first"
    # newest_first statements: rows with the same date/time either reverse
    # with the rest (most banks) or keep their printed order (M-Pesa's
    # Fuliza pairs, ab-313).
    tie_order: Literal["reverse", "as_printed"] = "reverse"
    # Order the statement's first same-time cluster by the one ordering
    # whose balances follow on from each other, when exactly one does.
    resolve_first_tie: bool = False
    # Drop description lines that are only digits (M-Pesa's parser always
    # has, so its dedupe hashes depend on it - ab-313).
    drop_digit_only_lines: bool = False
    # Within a line, words go into cells left to right, or by their exact
    # height then left to right ("top_then_left": NCBA's legacy order,
    # kept so its descriptions - and dedupe hashes - don't change, ab-320).
    word_order: Literal["left_to_right", "top_then_left"] = "left_to_right"
    # With a status column: keep only rows whose status is this (or blank).
    status_keep: LayoutText | None = None


class RowSource(_Strict):
    # first_row: the first transaction's balance minus its amount;
    # table_row: a row whose description starts with `label`;
    # labelled_line: the first amount after `label` on a line above the
    # table (NCBA's "Opening Balance", ab-320); from_totals: the totals
    # row's closing balance minus money in plus money out (Equity).
    source: Literal["first_row", "table_row", "labelled_line", "from_totals"] = "first_row"
    label: LayoutText | None = None


class TotalsSource(_Strict):
    # The statement's own money in / money out totals, checked against the
    # rows' sums. table_row: a "Total" row inside the table; summary_row: a
    # line above the table starting with `label` and holding two amounts,
    # money in then money out (M-Pesa's SUMMARY "TOTAL:", ab-313).
    # labelled_lines: money in is the first amount after `label`, money out
    # the first after `out_label`, on lines above the table (NCBA's
    # "Payments In" / "Payments Out", ab-320).
    source: Literal["none", "table_row", "summary_row", "labelled_lines"] = "none"
    label: LayoutText | None = None
    out_label: LayoutText | None = None


class StatementProfile(_Strict):
    schema_version: Literal[1] = 1
    file_type: Literal["pdf", "csv", "xlsx"] = "pdf"
    currency: CurrencyCode | None = None  # None: the account's currency
    date_format: DateFormat
    amounts: Amounts = Field(default_factory=Amounts)
    # With a signed amount column: whether a positive amount is money in.
    signed_amount_positive_is: Literal["in", "out"] = "in"
    table: Table
    opening_balance: RowSource = Field(default_factory=RowSource)
    totals: TotalsSource = Field(default_factory=TotalsSource)
    # running: every row's balance follows from the one before (the
    # default proof). totals: the rows add up to the statement's stated
    # totals - for layouts whose balance column isn't one continuous
    # ledger (M-Pesa around Fuliza, ab-313).
    balance_check: Literal["running", "totals"] = "running"
    # columns: the amount cells; balance_change: each row's amount and
    # direction are its balance minus the one before (Equity, NCBA - their
    # amount cells can't be told apart, ab-320). Needs a balance column and
    # the totals check (a running check would only prove itself).
    amounts_from: Literal["columns", "balance_change"] = "columns"
    # standard: reference + description + amount, or date + amount +
    # direction + balance + description; description_amount: NCBA's legacy
    # key, kept so re-uploads of its statements don't duplicate.
    dedupe: Literal["standard", "description_amount"] = "standard"
    # Read the currency code (3 capital letters) after this label on the
    # first page (Equity's "Currency KES"); falls back to `currency`.
    currency_label: LayoutText | None = None
    # ab-325: fees come off the balance without a row of their own (Airtel
    # Money). A money-out row whose balance falls by more than its amount
    # gets a "Transaction charge" row for the difference - accepted only
    # when the statement's totals include those charges.
    unlisted_charges: bool = False

    @model_validator(mode="after")
    def _readable_and_checkable(self):
        roles = [c.role for c in self.table.columns]
        count = roles.count
        if count("date") != 1 or count("description") != 1:
            raise ValueError("a profile needs exactly one date and one description column")
        if count("reference") > 1 or count("balance") > 1 or count("status") > 1:
            raise ValueError("at most one reference, balance and status column")
        if self.table.status_keep and count("status") == 0:
            raise ValueError("status_keep needs a status column")
        signed, ins, outs = count("signed_amount"), count("money_in"), count("money_out")
        if not ((signed == 1 and ins == outs == 0) or (signed == 0 and ins == outs == 1)):
            raise ValueError("use one signed amount column, or one money in and one money out column")

        spreadsheet = self.file_type in ("csv", "xlsx")
        if spreadsheet != (self.table.column_bounds == "cells"):
            raise ValueError("CSV and XLSX profiles use cells column bounds, and only they do")
        if spreadsheet and self.table.header_lines != 1:
            raise ValueError("a CSV or XLSX heading is one row")
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

        if self.opening_balance.source in ("table_row", "labelled_line") and not self.opening_balance.label:
            raise ValueError("an opening balance row needs a label")
        if self.opening_balance.source == "from_totals" and self.totals.source != "table_row":
            raise ValueError("an opening balance from the totals needs a totals row")
        if self.totals.source == "labelled_lines" and not self.totals.out_label:
            raise ValueError("labelled totals need a money out label")
        if self.amounts_from == "balance_change" and (count("balance") == 0 or self.balance_check != "totals"):
            raise ValueError("amounts from the balance change need a balance column and the totals check")
        if self.totals.source != "none" and not self.totals.label:
            raise ValueError("a totals row needs a label")
        if count("balance") == 0 and self.totals.source == "none":
            raise ValueError("a profile needs a balance column or a totals row to check against")
        if self.balance_check == "totals" and self.totals.source == "none":
            raise ValueError("a totals check needs a totals row to check against")
        if self.unlisted_charges and (
            count("balance") == 0
            or self.totals.source == "none"
            or self.amounts_from != "columns"
            or self.balance_check != "running"
        ):
            raise ValueError("unlisted charges need a balance column, the running check and the statement's totals")
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
