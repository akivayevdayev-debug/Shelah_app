"""The AI answer's community-customs list names each community by the `name`
in its customs/*.json file (backend/customs.py), and the Hebrew interface
renders it through COMMUNITY_HE_MAP in templates/index.html. A new or renamed
customs file with no entry there leaves its name in Latin letters in an
otherwise Hebrew answer, so this keeps the two in step."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_NOT_COMMUNITIES = {"schema.json", "customs_db.json"}


def _community_he_map_keys() -> set[str]:
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    block = re.search(r"const COMMUNITY_HE_MAP = \{(.*?)\n        \};", html, re.DOTALL)
    assert block, "COMMUNITY_HE_MAP not found in templates/index.html"
    return set(re.findall(r"^\s*'([^']+)':\s*'[^']+',?\s*$", block.group(1), re.MULTILINE))


def _customs_community_names() -> list[str]:
    names = []
    for path in sorted((ROOT / "customs").glob("*.json")):
        if path.name in _NOT_COMMUNITIES:
            continue
        name = json.loads(path.read_text(encoding="utf-8")).get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def test_every_customs_community_has_a_hebrew_name():
    covered = _community_he_map_keys()
    missing = [name for name in _customs_community_names() if name not in covered]
    assert not missing, f"customs communities with no Hebrew name in COMMUNITY_HE_MAP: {missing}"


def test_the_customs_directory_still_yields_communities():
    # Guards the test above against passing vacuously if the layout moves.
    assert len(_customs_community_names()) >= 10
