"""The one player-name normalizer, shared by every join in the repo.

Sources disagree on suffixes and punctuation — ESPN "Patrick Mahomes" vs
FantasyPros "Patrick Mahomes II", "James Cook III" vs "James Cook", "D.J. Moore"
vs "DJ Moore" — so joins key on this loose form and keep a preferred display
name separately. Kept deliberately conservative: a miss costs a blank column,
not a wrong number, and we would rather miss than mis-join two players.

Moved here from draft_sheets/extract_csg.py (which now imports it) so the
manage side — including the deployed bundle, which ships fftiers/ but not the
draft_sheets code — shares the definition instead of forking it.
"""
from __future__ import annotations

import re

_SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm_name(s) -> str:
    s = str(s or "").lower()
    s = s.replace("&", "and")
    s = re.sub(r"[.'`,]", "", s)
    s = _SUFFIXES.sub("", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())
