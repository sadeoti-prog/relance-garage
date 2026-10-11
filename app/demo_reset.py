"""Atomic, fail-closed regeneration of the demonstration ledger only."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import date

from . import db, followups, seed
from .config import DB_PATH, REAL_DB_PATH, today as today_fn


@dataclass(frozen=True)
class DemoResetResult:
    summary: dict
    queued: int
    rejected: int
    problems: tuple[str, ...]


def reset_demo(reference: date | None = None) -> DemoResetResult:
    """Build a complete demo beside the live file, then replace it atomically.

    The target is always ``DB_PATH`` and the operation refuses to start if the
    configured demo and real paths resolve to the same place. The real ledger
    is never opened by this function.
    """
    reference = reference or today_fn()
    demo_path = DB_PATH.resolve()
    real_path = REAL_DB_PATH.resolve()
    if demo_path == real_path:
        raise RuntimeError(
            "Réinitialisation refusée : les données de démonstration et les "
            "données réelles pointent vers le même emplacement."
        )

    demo_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = demo_path.with_name(
        f".{demo_path.name}.reset-{uuid.uuid4().hex}.tmp"
    )
    conn = None
    try:
        conn = db.connect(temporary)
        summary = seed.generate(conn, reference)
        followups.clear_drafts(conn)
        # A reset must stay offline even if somebody enabled a paid drafting
        # backend in their environment.
        run = followups.create_drafts(conn, reference, mode="mock")
        problems = tuple(seed.check(conn, reference))
        conn.close()
        conn = None
        if problems:
            raise RuntimeError(
                "Le nouveau jeu de démonstration est incohérent : "
                + " ; ".join(problems)
            )
        os.replace(temporary, demo_path)
        return DemoResetResult(
            summary=summary,
            queued=len(run.queued),
            rejected=len(run.rejected),
            problems=problems,
        )
    finally:
        if conn is not None:
            conn.close()
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
