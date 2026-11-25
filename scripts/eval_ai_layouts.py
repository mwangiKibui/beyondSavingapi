"""ab-333: how well the AI reads statement layouts it has never seen.

For each statement: page 1 is masked (shapes only, as in production) and
sent to the model with the production prompt; its answer is built into a
layout from scratch (no guess of ours), read and checked like any other.
Scored against what we know is right: the rows a trusted read produced,
the column roles of the layout that read it, and which row groupings read
it. Prints a Markdown report - outcomes and counts only, never a value
from a statement.

Run inside the worker container (needs the database, storage and
DO_INFERENCE_KEY):  python scripts/eval_ai_layouts.py [--runs 2]
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

import asyncpg

from app.core.config import get_settings
from app.core.storage import get_storage_client
from app.parsers.profiles.masking import mask_pdf
from app.parsers.profiles.reader import ROW_GROUPINGS, read_best_grouping, read_statement, with_grouping
from app.parsers.profiles.schema import validate_profile
from app.parsers.profiles.text import normalise
from app.repositories.statement_profiles import list_active_profiles
from app.services.inference import complete
from app.services.layout_proposer import PROMPT, evaluate, parse_answer
from app.services.statement_profiles import read_with_profiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
KEY_ROLES = {"date", "description", "money_in", "money_out", "signed_amount", "balance"}


def generated() -> list[dict]:
    from tests.statement_pdfs import Layout, build, sample_rows

    profile = {
        "date_format": "%d/%m/%Y",
        "table": {
            "columns": [
                {"role": "date", "header": "Date"},
                {"role": "description", "header": "Details"},
                {"role": "money_in", "header": "Money In"},
                {"role": "money_out", "header": "Money Out"},
                {"role": "balance", "header": "Balance"},
            ],
            "column_bounds": "header_gaps",
            "end_markers": ["End of statement"],
        },
    }
    return [
        {"name": f"Generated: text {pos} the date line", "content": build(sample_rows(30), Layout(text_position=pos)),
         "truth": profile, "rows": 30}
        for pos in ("on", "above", "around")
    ]


async def uploads(pool) -> list[dict]:
    settings = get_settings()
    recs = await pool.fetch(
        """
        SELECT DISTINCT ON (a.provider) si.storage_key, si.file_name, si.row_count, a.provider, a.currency,
               i.id AS institution_id
        FROM statement_imports si
        JOIN accounts a ON a.id = si.account_id
        LEFT JOIN institutions i ON i.name = a.provider AND i.type = a.account_type
        WHERE si.status = 'parsed' AND si.storage_key IS NOT NULL AND si.file_name ILIKE '%.pdf'
        ORDER BY a.provider, si.created_at DESC
        """
    )
    profiles = await list_active_profiles(pool)
    by_id = {p["id"]: p for p in profiles}
    out = []
    for r in recs:
        content = get_storage_client().get_object(Bucket=settings.storage_bucket, Key=r["storage_key"])["Body"].read()
        read = read_with_profiles(content, file_name=r["file_name"], institution_id=r["institution_id"],
                                  currency=r["currency"] or "KES", candidates=profiles)
        truth = by_id[read.profile_id]["profile"] if read.profile_id in by_id else None
        out.append({"name": r["provider"], "content": content, "truth": truth, "rows": r["row_count"]})
    return out


def mentor_fixture() -> list[dict]:
    from app.parsers.mentor_sacco import parse_mentor_sacco_statement

    path = ROOT / "tests/fixtures/statements/mentor_sacco/member_statement_sample.pdf"
    if not path.exists():
        return []
    content = path.read_bytes()
    rows = sum(len(s["transactions"]) for s in parse_mentor_sacco_statement(content))
    return [{"name": "Mentor Sacco (sample, sub-ledgers)", "content": content, "truth": None, "rows": rows}]


def groupings_that_read(content: bytes, truth: dict | None) -> set[str]:
    if truth is None:
        return set()
    out = set()
    for g in ROW_GROUPINGS:
        try:
            if read_statement(content, with_grouping(validate_profile(truth), g), default_currency="KES").check.passed:
                out.add(g)
        except Exception:
            pass
    return out


def roles_right(answer, truth: dict | None) -> str:
    if truth is None or answer is None:
        return "-"
    want = {normalise(c.get("header") or ""): c["role"] for c in truth["table"]["columns"] if c.get("header")}
    want = {h: r for h, r in want.items() if r in KEY_ROLES}
    got = {normalise(c.heading): c.role for c in answer.columns}
    right = sum(1 for h, r in want.items() if got.get(h) == r)
    return f"{right}/{len(want)}"


async def one(statement: dict, readable: set[str]) -> dict:
    masked = mask_pdf(statement["content"], pages=1)
    if not masked:
        return {"outcome": "no text layer"}
    started = time.monotonic()
    try:
        reply = await complete(PROMPT + masked)
    except Exception as e:
        return {"outcome": f"call failed ({type(e).__name__})"}
    seconds = time.monotonic() - started
    answer = parse_answer(reply.content)
    ev = evaluate(statement["content"], None, reply.content)
    rows, gaps = 0, None
    if ev.outcome == "passed":
        _, read = read_best_grouping(statement["content"], validate_profile(ev.profile), default_currency="KES")
        rows, gaps = len(read.transactions), read.description_gaps
    said = answer.row_grouping if answer else None
    return {
        "outcome": ev.outcome,
        "rows": rows,
        "gaps": gaps,
        "roles": roles_right(answer, statement["truth"]),
        "grouping_said": said or "-",
        "grouping_ok": ("yes" if said in readable else "no") if readable and said else "-",
        "used": ev.profile["table"].get("continuation", "below") if ev.profile else "-",
        "tokens": f"{reply.prompt_tokens}/{reply.completion_tokens}",
        "seconds": round(seconds),
    }


async def main(runs: int) -> None:
    pool = await asyncpg.create_pool(get_settings().database_url)
    statements = await uploads(pool)
    if not any(s["name"] == "Mentor Sacco" for s in statements):
        statements += mentor_fixture()
    statements += generated()
    settings = get_settings()
    print(f"# AI layout evaluation\n\nModel `{settings.do_inference_model}`, {runs} run(s) per statement, "
          "page 1 masked, no guess of ours (the AI reads the layout from scratch).\n")
    print("| Statement | Run | Outcome | Rows read / expected | Rows w/o description | Key roles right "
          "| Row grouping said (reads?) | Grouping used | Tokens in/out | Seconds |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for s in statements:
        readable = groupings_that_read(s["content"], s["truth"])
        for run in range(1, runs + 1):
            r = await one(s, readable)
            print(f"| {s['name']} | {run} | {r['outcome']} | {r.get('rows', 0)} / {s['rows']} | "
                  f"{r.get('gaps') if r.get('gaps') is not None else '-'} | {r.get('roles', '-')} | "
                  f"{r.get('grouping_said', '-')} ({r.get('grouping_ok', '-')}) | {r.get('used', '-')} | "
                  f"{r.get('tokens', '-')} | {r.get('seconds', '-')} |", flush=True)
        print(f"<!-- {s['name']}: groupings that read it with the known layout: "
              f"{', '.join(sorted(readable)) or 'n/a'} -->")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=2)
    asyncio.run(main(parser.parse_args().runs))
