"""ab-312: the profile repository validates before it writes. SQL itself
is checked in the live run."""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.parsers.profiles.schema import InvalidStatementProfile
from app.repositories.statement_profiles import create_profile, list_active_profiles

VALID = {
    "date_format": "%d/%m/%Y",
    "table": {
        "columns": [
            {"role": "date", "header": "Date"},
            {"role": "description", "header": "Details"},
            {"role": "signed_amount", "header": "Amount"},
            {"role": "balance", "header": "Balance"},
        ],
        "column_bounds": "header_gaps",
    },
}


class Pool:
    def __init__(self, row=None, rows=()):
        self.fetchrow = AsyncMock(return_value=row)
        self.fetch = AsyncMock(return_value=list(rows))


async def test_an_invalid_profile_never_reaches_the_database():
    pool = Pool()
    with pytest.raises(InvalidStatementProfile):
        await create_profile(
            pool, institution_id=uuid4(), fingerprint="{}", profile={**VALID, "holder": "x"}, source="user"
        )
    pool.fetchrow.assert_not_called()


async def test_a_valid_profile_is_stored_normalised_with_defaults_filled():
    stored = {"id": uuid4(), "profile": "{}"}
    pool = Pool(row=stored)
    await create_profile(pool, institution_id=uuid4(), fingerprint="{}", profile=VALID, source="admin")
    args = pool.fetchrow.call_args.args
    saved = json.loads(args[3])
    assert saved["table"]["header_lines"] == 1 and saved["schema_version"] == 1
    assert args[4:] == ("admin", "draft", None)


async def test_active_profiles_come_back_with_their_json_decoded():
    pool = Pool(rows=[{"id": uuid4(), "profile": json.dumps(VALID)}])
    rows = await list_active_profiles(pool)
    assert rows[0]["profile"]["date_format"] == "%d/%m/%Y"
