"""ab-314: the AI layout proposer.

Runs in the worker for an upload no stored profile reads, before it goes to
the Columns step, and only when our own guess (headings + learned words)
doesn't already pass the balance check.

The model sees a MASKED page (app/parsers/profiles/masking.py): headings
and statement words verbatim, everything else reduced to its shape. It
answers a few questions about the layout - each column's role, the date
format, the opening balance and totals labels - never rows. (Which way the
rows run is tried both ways here: masked dates can't show it.)
When our guess found the table, it holds the geometry and the answer is
merged onto it. When it found none (e.g. a date format we don't know), the
table is found on the statement by the headings the model named. Either way
the result is validated against the profile schema and run through the
reader and the balance check, trying locally what masking hides: both row
orders, and the other date column when there are two. So the model can't
invent an amount: our code reads every row.

The proposal becomes the upload's suggested layout on Columns (still pending
until the uploader accepts it); one that failed the check is still a better
start than an empty screen. No key, over the daily limit, a failed call or a
reply that isn't valid leaves the upload exactly as it was without the AI.
"""

import asyncio
import io
import json
import logging
import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import pdfplumber
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.parsers.profiles.guess import _passes, dedupe_roles, layout_start
from app.parsers.profiles.layout import page_lines
from app.parsers.profiles.masking import mask_grid, mask_pdf
from app.parsers.profiles.reader import _header_band, read_statement
from app.parsers.profiles.schema import validate_profile
from app.parsers.profiles.tabular import load_grid
from app.parsers.profiles.text import normalise
from app.repositories.ai_layout_proposals import insert_proposal
from app.services.inference import InferenceError, complete, inference_enabled
from app.services.limits import AI_LAYOUT_PROPOSALS_PER_DAY, check_limit

logger = logging.getLogger(__name__)

ROLES = ("date", "description", "money_in", "money_out", "signed_amount", "balance", "reference", "status", "ignore")

PROMPT = """You read the LAYOUT of a bank or mobile-money statement. The page below is masked: \
column headings and common statement words are shown as they are; every other value is \
reduced to its shape (9 = a digit, X = a capital letter, x = a small letter, punctuation kept).

For each column heading in the transactions table, give its role:
- date: the transaction date (or date and time)
- description: what the transaction was
- money_in: amounts received / credits / paid in
- money_out: amounts sent / debits / withdrawn
- signed_amount: one amount column where the sign or DR/CR says the direction
- balance: the running balance after each row
- reference: a transaction id / receipt / reference number
- status: e.g. completed / successful
- ignore: anything else, e.g. a SECOND date column (a value date) when another column is the transaction date

Also give:
- date_format: a Python strftime format matching the date column's shape \
(e.g. 99/99/9999 -> "%d/%m/%Y", 99-99-99 99:99 XX -> "%d-%m-%y %I:%M %p", \
99-Xxx-99 -> "%d-%b-%y" where Xxx is a month name)
- opening_balance_label: the exact words of a line above the table that gives the opening \
balance (e.g. "Opening Balance"), or null
- totals_in_label and totals_out_label: the exact words of summary lines giving total money \
in and total money out (e.g. "Total Credit", "Total Debit"), or null

Reply with ONLY this JSON, no other text:
{"columns": [{"heading": "<heading exactly as shown>", "role": "<role>"}], "date_format": "...", \
"opening_balance_label": null, "totals_in_label": null, "totals_out_label": null}

Masked page:
"""


class _Column(BaseModel):
    model_config = ConfigDict(extra="ignore")
    heading: str = Field(min_length=1, max_length=60)
    role: Literal[ROLES]  # type: ignore[valid-type]


class LayoutAnswer(BaseModel):
    """What the model must reply; anything else is an invalid proposal."""

    model_config = ConfigDict(extra="ignore")
    columns: list[_Column] = Field(min_length=2, max_length=12)
    date_format: str | None = Field(default=None, max_length=40)
    opening_balance_label: str | None = Field(default=None, max_length=40)
    totals_in_label: str | None = Field(default=None, max_length=40)
    totals_out_label: str | None = Field(default=None, max_length=40)


def parse_answer(text: str) -> LayoutAnswer | None:
    """The JSON object in the reply, validated; None when there isn't one."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return LayoutAnswer.model_validate(json.loads(match.group(0)))
    except (ValueError, ValidationError):
        return None


def _from_headings(answer: LayoutAnswer, *, content: bytes, file_type: str) -> dict:
    """No guess: a profile that finds each column by the heading the model
    named. A PDF's headings may sit on up to three lines; the fewest that
    find them all on page 1 is used."""
    pdf = file_type == "pdf"
    raw = {
        "file_type": file_type,
        "date_format": answer.date_format or "%d/%m/%Y",
        "table": {
            "columns": [{"header": c.heading, "role": c.role} for c in answer.columns],
            "column_bounds": "header_gaps" if pdf else "cells",
        },
    }
    raw["table"]["columns"] = [{**c, "role": r} for c, r in zip(raw["table"]["columns"], dedupe_roles([c["role"] for c in raw["table"]["columns"]]), strict=True)]
    if pdf:
        raw["table"]["header_lines"] = _heading_lines(content, raw)
    return raw


def _heading_lines(content: bytes, raw: dict) -> int:
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            page = pdf.pages[0]
            for span in (1, 2, 3):
                trial = {**raw, "table": {**raw["table"], "header_lines": span}}
                profile = validate_profile(trial)
                if _header_band(page_lines(page, y_tolerance=profile.table.y_tolerance), profile) is not None:
                    return span
    except Exception:
        pass
    return 1


def _date_swaps(raw: dict) -> list[dict]:
    """The same profile with another date-headed column as the date: masked
    dates can't show which of two date columns is filled on every row."""
    columns = raw["table"]["columns"]
    date_at = next((i for i, c in enumerate(columns) if c["role"] == "date"), None)
    if date_at is None:
        return []
    out = []
    for i, c in enumerate(columns):
        if i != date_at and c["role"] == "ignore" and "date" in normalise(c.get("header") or "").split():
            swapped = [
                {**col, "role": "date" if n == i else "ignore" if n == date_at else col["role"]} for n, col in enumerate(columns)
            ]
            out.append({**raw, "table": {**raw["table"], "columns": swapped}})
    return out


def candidates(guess: dict | None, answer: LayoutAnswer, *, content: bytes = b"", file_type: str = "pdf") -> list[dict]:
    """The guess with the answer merged in: roles by heading, the date format
    and row order (or, with no guess, a profile built from the answer's
    headings); then the same with the printed opening balance and totals
    (and fees with no row of their own) when the answer named those labels,
    and with the other date column. Only those that make a valid profile,
    in that order."""
    if guess is None:
        base = _from_headings(answer, content=content, file_type=file_type)
    else:
        base = _merged(guess, answer)
    tries = [base]
    if answer.opening_balance_label and answer.totals_in_label and answer.totals_out_label:
        tries.append(
            {
                **base,
                "opening_balance": {"source": "labelled_line", "label": answer.opening_balance_label},
                "totals": {
                    "source": "labelled_lines",
                    "label": answer.totals_in_label,
                    "out_label": answer.totals_out_label,
                },
                "unlisted_charges": True,
            }
        )
    tries = tries + [swap for raw in tries for swap in _date_swaps(raw)]
    # The masked dates hide which way the rows run, so both orders are tried
    # here (ours first) - the balance check says which one is right.
    flipped = {"oldest_first": "newest_first", "newest_first": "oldest_first"}
    tries = [
        variant
        for raw in tries
        for variant in (
            raw,
            {**raw, "table": {**raw["table"], "row_order": flipped[raw["table"].get("row_order", "oldest_first")]}},
        )
    ]
    out = []
    for raw in tries:
        try:
            out.append(validate_profile(raw).model_dump(mode="json"))
        except Exception:
            continue
    return out


def _merged(guess: dict, answer: LayoutAnswer) -> dict:
    roles_by_heading = {normalise(c.heading): c.role for c in answer.columns}
    columns = guess["table"]["columns"]
    # When the answer names every column, its order is the page's order: a
    # column whose heading we couldn't store (e.g. "Narr.") takes the role
    # in its position.
    by_position = len(answer.columns) == len(columns)
    roles = dedupe_roles(
        [
            roles_by_heading.get(
                normalise(c.get("header") or ""),
                answer.columns[i].role if by_position and not c.get("header") else c["role"],
            )
            for i, c in enumerate(columns)
        ]
    )
    return {
        **guess,
        "table": {
            **guess["table"],
            "columns": [{**c, "role": r} for c, r in zip(columns, roles, strict=True)],
        },
        **({"date_format": answer.date_format} if answer.date_format else {}),
    }


@dataclass
class Evaluation:
    outcome: Literal["passed", "failed_check", "invalid"]
    profile: dict | None


def evaluate(content: bytes, guess: dict | None, reply_text: str, file_type: str = "pdf") -> Evaluation:
    """Merge the reply onto the guess (or build from it, with no guess) and
    run the balance check."""
    answer = parse_answer(reply_text)
    if answer is None:
        return Evaluation("invalid", None)
    tried = candidates(guess, answer, content=content, file_type=file_type)
    if not tried:
        return Evaluation("invalid", None)
    # Of those that pass, the one that reads the most rows: a wrong date
    # column can pass by reading only the rows it can date.
    best, most = None, 0
    for profile in tried:
        rows = _passing_rows(content, profile)
        if rows > most:
            best, most = profile, rows
    if best is not None:
        return Evaluation("passed", best)
    return Evaluation("failed_check", tried[0])


def _passing_rows(content: bytes, profile: dict) -> int:
    """How many rows the profile reads with the balances adding up; 0 when
    they don't or it can't read the file."""
    try:
        result = read_statement(content, validate_profile(profile), default_currency="KES")
    except Exception:
        return 0
    return len(result.transactions) if result.check.passed else 0


def masked_page(content: bytes, file_type: str) -> str | None:
    if file_type == "pdf":
        return mask_pdf(content, pages=1)
    return mask_grid(load_grid(content, file_type))


async def propose_layout(pool, *, record: dict, content: bytes, file_type: str) -> None:
    """For an upload about to go to Columns. Never raises: the upload goes
    to Columns either way, with or without a proposal."""
    try:
        await _propose(pool, record=record, content=content, file_type=file_type)
    except Exception:
        logger.error("AI layout proposal failed for import %s", record.get("id"), exc_info=True)


async def _propose(pool, *, record: dict, content: bytes, file_type: str) -> None:
    if not inference_enabled():
        return
    import_id: UUID = record["id"]
    start = await asyncio.to_thread(layout_start, content, file_type)
    guess = start.get("guess")
    if guess is not None and await asyncio.to_thread(_passes, content, guess):
        logger.info("Import %s: our own guess passes the balance check; no AI needed", import_id)
        return
    limit = await check_limit(pool, record["user_id"], AI_LAYOUT_PROPOSALS_PER_DAY)
    if limit.reached:
        logger.info("Import %s: AI layout proposals at the daily limit", import_id)
        return
    masked = await asyncio.to_thread(masked_page, content, file_type)
    if not masked:
        return
    try:
        reply = await complete(PROMPT + masked)
    except InferenceError:
        from app.core.config import get_settings

        await insert_proposal(
            pool, user_id=record["user_id"], import_id=import_id, model=get_settings().do_inference_model, outcome="error"
        )
        return
    result = await asyncio.to_thread(evaluate, content, guess, reply.content, file_type)
    await insert_proposal(
        pool,
        user_id=record["user_id"],
        import_id=import_id,
        model=reply.model,
        outcome=result.outcome,
        profile=result.profile,
        prompt_tokens=reply.prompt_tokens,
        completion_tokens=reply.completion_tokens,
    )
    logger.info(
        "Import %s: AI layout proposal %s (%s tokens in, %s out)",
        import_id,
        result.outcome,
        reply.prompt_tokens,
        reply.completion_tokens,
    )
