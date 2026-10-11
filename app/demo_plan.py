"""The invoices of the demo dataset.

Two blocks:

* `EXPLICIT` — hand placed. The overdue book of the garage, spread on purpose
  across the four escalation levels, plus a few invoices sitting just below the
  first threshold and a few not due yet. This is the part that tells the story.
* `HISTORICAL` — generated. Settled invoices going back six months, so the
  ledger looks like a working garage rather than an insolvent one, and so the
  activity counters have a real history to draw on.

`due_offset` is the number of days between today and the invoice due date:
negative means already overdue, positive means not due yet.

Amounts are NOT written here: they come from the job catalogue and are computed
in Python at generation time. A plan row can steer the *size* of an invoice
with `target_min_eur` / `target_max_eur`, but never dictate the figure.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

from .catalog import FLEET_JOBS, PRIVATE_JOBS
from .config import SEED
from .demo_roster import CUSTOMERS, CUSTOMERS_BY_KEY


@dataclass(frozen=True)
class InvoicePlan:
    customer: str          # key in demo_roster.CUSTOMERS_BY_KEY
    job: str               # code in catalog.JOBS_BY_CODE
    due_offset: int        # days from today (negative = overdue)
    status: str            # 'paid' | 'unpaid' | 'partial'
    vehicle_index: int = 0
    paid_offset: int | None = None   # for 'paid': days from today of payment
    paid_ratio_bp: int = 0           # for 'partial': share already settled
    target_min_eur: float | None = None   # optional size band, TTC
    target_max_eur: float | None = None


I = InvoicePlan

# ---------------------------------------------------------------------------
# The overdue book — hand placed
# ---------------------------------------------------------------------------

EXPLICIT: tuple[InvoicePlan, ...] = (
    # ------------------------------------------------------------------ réglées
    I("perrin", "vidange_complete", due_offset=-95, status="paid", paid_offset=-97),
    I("faure", "freins_av_complet", due_offset=-88, status="paid", paid_offset=-90),
    I("fournil", "revision_60k", due_offset=-76, status="paid", paid_offset=-70),
    I("roux", "distribution", due_offset=-70, status="paid", paid_offset=-72),
    I("colin", "pneus_2", due_offset=-64, status="paid", paid_offset=-64),
    I("marechal", "controle_technique", due_offset=-55, status="paid", paid_offset=-57),
    I("plomberie_ducret", "freins_utilitaire", due_offset=-48, status="paid",
      paid_offset=-35),   # settled after a follow-up
    I("garnier", "vidange_simple", due_offset=-40, status="paid", paid_offset=-42),
    I("diallo", "climatisation", due_offset=-33, status="paid", paid_offset=-33),
    I("autoecole_parc", "pneus_4", due_offset=-27, status="paid", paid_offset=-20,
      vehicle_index=0),   # settled after a follow-up
    I("petit", "batterie", due_offset=-20, status="paid", paid_offset=-21),
    I("chevalier", "echappement", due_offset=-12, status="paid", paid_offset=-14),

    # ------------------------------------------------------- pas encore échues
    I("barbier", "vidange_complete", due_offset=11, status="unpaid"),
    I("saveurs_co", "diagnostic", due_offset=6, status="unpaid"),
    I("dubois", "plaquettes_av", due_offset=2, status="unpaid"),

    # ------------------------------ échues mais sous le seuil du 1er niveau (J+3)
    I("lemoine", "controle_technique", due_offset=-1, status="unpaid"),
    I("ould_braham", "courroie_accessoires", due_offset=-2, status="unpaid"),

    # -------------------------------------------- niveau 1 : rappel courtois (J+3)
    I("perrin", "plaquettes_av", due_offset=-3, status="unpaid"),
    I("elec_rhone", "vidange_complete", due_offset=-4, status="unpaid", vehicle_index=0),
    I("petit", "pneus_2", due_offset=-5, status="unpaid"),
    I("marchand", "batterie", due_offset=-6, status="unpaid"),
    I("garnier", "climatisation", due_offset=-7, status="unpaid"),
    I("autoecole_parc", "controle_technique", due_offset=-8, status="unpaid",
      vehicle_index=1),
    I("renaud", "vidange_simple", due_offset=-9, status="unpaid"),

    # ------------------------------------------- niveau 2 : relance ferme (J+10)
    I("dubois", "freins_av_complet", due_offset=-10, status="unpaid"),
    I("toitures_vidal", "freins_utilitaire", due_offset=-12, status="partial",
      vehicle_index=0, paid_ratio_bp=4000),      # acompte versé
    I("colin", "amortisseurs_ar", due_offset=-14, status="unpaid"),
    I("diallo", "pneus_4", due_offset=-16, status="unpaid"),
    I("fournil", "echappement", due_offset=-18, status="unpaid"),
    I("benali", "diagnostic", due_offset=-20, status="unpaid"),

    # -------------------------------------------- niveau 3 : dernier avis (J+21)
    I("marechal", "revision_60k", due_offset=-21, status="unpaid"),
    I("ambulances_ozon", "distribution", due_offset=-24, status="partial",
      paid_ratio_bp=5000),                        # acompte versé
    I("chevalier", "demarreur", due_offset=-27, status="unpaid"),
    I("lemoine", "parebrise", due_offset=-30, status="unpaid"),
    I("elec_rhone", "freins_av_complet", due_offset=-33, status="unpaid",
      vehicle_index=1),

    # ---------------------------------------- niveau 4 : mise en demeure (J+35)
    I("roux", "embrayage", due_offset=-36, status="unpaid"),
    I("espaces_verts_bonnet", "entretien_flotte", due_offset=-41, status="unpaid"),
    I("faure", "carrosserie_aile", due_offset=-48, status="unpaid"),
    # The headline debt of the demo: the biggest bodywork job, deep in the red.
    I("ould_braham", "carrosserie_lourde", due_offset=-57, status="unpaid",
      target_min_eur=2150, target_max_eur=2250),
    I("plomberie_ducret", "distribution", due_offset=-68, status="unpaid"),
)

# ---------------------------------------------------------------------------
# Six months of settled invoices — generated
# ---------------------------------------------------------------------------

HISTORICAL_COUNT = 30

# How many of them are settled late, inside the current month, after a
# follow-up. Feeds the "montant récupéré" counter on the dashboard.
RECOVERED_THIS_MONTH = 4

# Jobs a given vehicle should not plausibly need twice in six months.
_MAJOR_JOBS = frozenset(
    {"distribution", "embrayage", "carrosserie_lourde", "carrosserie_aile",
     "parebrise", "demarreur"}
)

# How customers actually settle: mostly on time, some drifting, a few late.
_PAYMENT_BEHAVIOUR = (
    ("on_time", 70, (-6, 0)),
    ("drifting", 22, (1, 12)),
    ("late", 8, (14, 32)),
)


def _pick_delay(rng: random.Random) -> int:
    roll = rng.randint(1, 100)
    cumulative = 0
    for _name, weight, (low, high) in _PAYMENT_BEHAVIOUR:
        cumulative += weight
        if roll <= cumulative:
            return rng.randint(low, high)
    return 0


def build_historical(count: int = HISTORICAL_COUNT) -> tuple[InvoicePlan, ...]:
    """Settled invoices spread over the last six months.

    Deterministic: the seed fixes who paid what and when. Customers are drawn in
    reshuffled passes so the same names do not come back in a visible pattern.
    """
    rng = random.Random(f"{SEED}|historical")

    # Major jobs already invoiced in the explicit block stay off the table.
    used_major: dict[str, set[str]] = {c.key: set() for c in CUSTOMERS}
    for plan in EXPLICIT:
        if plan.job in _MAJOR_JOBS:
            used_major[plan.customer].add(plan.job)

    # Unique due dates spread over roughly six months, oldest last.
    offsets = sorted(rng.sample(range(8, 186), count))

    order: list[str] = []
    while len(order) < count:
        cycle = [c.key for c in CUSTOMERS]
        rng.shuffle(cycle)
        order.extend(cycle)
    order = order[:count]

    plans: list[InvoicePlan] = []
    for index, offset in enumerate(offsets):
        key = order[index]
        spec = CUSTOMERS_BY_KEY[key]
        pool = PRIVATE_JOBS if spec.kind == "particulier" else FLEET_JOBS

        candidates = [j for j in pool if j.code not in used_major[key]]
        job = rng.choice(candidates or list(pool))
        if job.code in _MAJOR_JOBS:
            used_major[key].add(job.code)

        due_offset = -offset
        paid_offset = min(due_offset + _pick_delay(rng), -1)
        vehicle_index = rng.randrange(len(spec.vehicles))

        plans.append(
            InvoicePlan(
                customer=key,
                job=job.code,
                due_offset=due_offset,
                status="paid",
                vehicle_index=vehicle_index,
                paid_offset=paid_offset,
            )
        )

    # The activity counters need something to count. Random payment behaviour
    # alone leaves too few recent recoveries to be worth showing, so a fixed
    # handful of invoices are settled late, within the current month, after a
    # follow-up would have gone out. These are the "récupéré après relance"
    # cases the dashboard reports.
    recovered = 0
    for index, plan in enumerate(plans):
        if recovered >= RECOVERED_THIS_MONTH:
            break
        if not (-60 <= plan.due_offset <= -18):
            continue
        paid_offset = -rng.randint(3, 24)
        if paid_offset <= plan.due_offset + 3:
            continue                      # must be a genuinely late payment
        plans[index] = replace(plan, paid_offset=paid_offset)
        recovered += 1

    return tuple(plans)


HISTORICAL: tuple[InvoicePlan, ...] = build_historical()

PLAN: tuple[InvoicePlan, ...] = EXPLICIT + HISTORICAL

PAYMENT_METHODS = ("virement", "carte bancaire", "chèque", "espèces")
