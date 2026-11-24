"""ab-313: profiles shipped with the app (source=builtin).

Each JSON file here is one layout: the institution it belongs to (by
name + type, as accounts reference providers), its page-1 fingerprint and
the profile. ensure_builtin_profiles() (app/services/statement_profiles.py)
loads them into statement_profiles at startup. Golden tests prove each
one reads exactly like the bespoke parser it replaces.
"""

import json
from dataclasses import dataclass
from pathlib import Path

_DIR = Path(__file__).parent


@dataclass(frozen=True)
class BuiltinProfile:
    key: str
    institution_name: str
    institution_type: str
    fingerprint: str
    profile: dict


def load_builtin_profiles() -> list[BuiltinProfile]:
    out = []
    for path in sorted(_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        out.append(
            BuiltinProfile(
                key=path.stem,
                institution_name=data["institution"]["name"],
                institution_type=data["institution"]["type"],
                fingerprint=data["fingerprint"],
                profile=data["profile"],
            )
        )
    return out
