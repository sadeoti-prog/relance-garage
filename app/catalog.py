"""Catalogue of jobs a French garage actually invoices.

Each job produces realistic invoice lines: parts (fournitures) plus labour
(main d'oeuvre) billed at the trade's hourly rate. Line labels are in French
because they are printed on the invoice; the code around them stays English.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MECA = "Main d’œuvre mécanique"
CARR = "Main d’œuvre carrosserie"
DIAG = "Main d’œuvre diagnostic"


@dataclass(frozen=True)
class PartSpec:
    label: str
    unit: str            # 'u', 'L', 'forfait'
    qty_milli: int       # quantity x1000
    price_min: float     # euros HT
    price_max: float


@dataclass(frozen=True)
class JobSpec:
    code: str
    label: str                       # what appears as the invoice subject
    trade: str                       # key of config.LABOUR_RATES_CENTS
    hours_min_milli: int             # labour hours x1000
    hours_max_milli: int
    parts: tuple[PartSpec, ...] = field(default_factory=tuple)
    labour_label: str = MECA
    for_fleet: bool = False          # only invoiced to business customers


def P(label: str, unit: str, qty: int, lo: float, hi: float) -> PartSpec:
    return PartSpec(label=label, unit=unit, qty_milli=qty, price_min=lo, price_max=hi)


JOBS: tuple[JobSpec, ...] = (
    JobSpec(
        code="vidange_simple",
        label="Vidange moteur",
        trade="mecanique", hours_min_milli=500, hours_max_milli=700,
        parts=(
            P("Huile moteur 5W-30", "L", 4000, 8.90, 11.50),
            P("Filtre à huile", "u", 1000, 9.00, 19.00),
            P("Joint de bouchon de vidange", "u", 1000, 2.00, 4.00),
        ),
    ),
    JobSpec(
        code="vidange_complete",
        label="Vidange + filtres + contrôle 20 points",
        trade="mecanique", hours_min_milli=800, hours_max_milli=1100,
        parts=(
            P("Huile moteur 5W-30", "L", 5000, 8.90, 11.50),
            P("Filtre à huile", "u", 1000, 9.00, 19.00),
            P("Filtre à air", "u", 1000, 14.00, 29.00),
            P("Filtre d’habitacle", "u", 1000, 12.00, 24.00),
            P("Joint de bouchon de vidange", "u", 1000, 2.00, 4.00),
        ),
    ),
    JobSpec(
        code="plaquettes_av",
        label="Remplacement plaquettes de frein avant",
        trade="mecanique", hours_min_milli=700, hours_max_milli=1000,
        parts=(
            P("Jeu de plaquettes de frein avant", "u", 1000, 39.00, 89.00),
            P("Kit de montage plaquettes", "u", 1000, 8.00, 15.00),
        ),
    ),
    JobSpec(
        code="freins_av_complet",
        label="Remplacement disques et plaquettes avant",
        trade="mecanique", hours_min_milli=1500, hours_max_milli=2000,
        parts=(
            P("Disque de frein avant", "u", 2000, 42.00, 89.00),
            P("Jeu de plaquettes de frein avant", "u", 1000, 39.00, 89.00),
            P("Kit de montage plaquettes", "u", 1000, 8.00, 15.00),
            P("Liquide de frein DOT 4", "L", 1000, 9.00, 14.00),
        ),
    ),
    JobSpec(
        code="distribution",
        label="Remplacement kit de distribution + pompe à eau",
        trade="mecanique", hours_min_milli=4500, hours_max_milli=6000,
        parts=(
            P("Kit de distribution", "u", 1000, 169.00, 349.00),
            P("Pompe à eau", "u", 1000, 49.00, 119.00),
            P("Liquide de refroidissement", "L", 5000, 4.90, 7.90),
            P("Courroie d’accessoires", "u", 1000, 29.00, 59.00),
        ),
    ),
    JobSpec(
        code="embrayage",
        label="Remplacement de l’embrayage",
        trade="mecanique", hours_min_milli=5000, hours_max_milli=7000,
        parts=(
            P("Kit d’embrayage", "u", 1000, 229.00, 449.00),
            P("Butée d’embrayage hydraulique", "u", 1000, 59.00, 139.00),
            P("Huile de boîte de vitesses", "L", 2000, 12.00, 19.00),
        ),
    ),
    JobSpec(
        code="carrosserie_aile",
        label="Réparation aile avant droite + peinture",
        trade="carrosserie", hours_min_milli=4000, hours_max_milli=7000,
        labour_label=CARR,
        parts=(
            P("Aile avant droite", "u", 1000, 139.00, 289.00),
            P("Ingrédients peinture", "forfait", 1000, 89.00, 159.00),
            P("Petites fournitures carrosserie", "forfait", 1000, 12.00, 25.00),
        ),
    ),
    JobSpec(
        code="carrosserie_lourde",
        label="Remplacement pare-chocs et aile avant + peinture",
        trade="carrosserie", hours_min_milli=8000, hours_max_milli=14000,
        labour_label=CARR,
        parts=(
            P("Pare-chocs avant", "u", 1000, 219.00, 479.00),
            P("Aile avant gauche", "u", 1000, 139.00, 319.00),
            P("Ingrédients peinture", "forfait", 1000, 139.00, 279.00),
            P("Petites fournitures carrosserie", "forfait", 1000, 25.00, 49.00),
        ),
    ),
    JobSpec(
        code="controle_technique",
        label="Présentation au contrôle technique",
        trade="mecanique", hours_min_milli=200, hours_max_milli=400,
        parts=(
            P("Contrôle technique (prestation partenaire)", "forfait", 1000, 69.00, 79.00),
            P("Prise en charge et convoyage du véhicule", "forfait", 1000, 15.00, 25.00),
        ),
    ),
    JobSpec(
        code="pneus_2",
        label="Montage de 2 pneumatiques + équilibrage",
        trade="mecanique", hours_min_milli=400, hours_max_milli=600,
        parts=(
            P("Pneumatique 195/65 R15 91V", "u", 2000, 59.00, 119.00),
            P("Valve", "u", 2000, 3.00, 4.50),
            P("Équilibrage de roue", "u", 2000, 9.00, 14.00),
            P("Recyclage pneumatiques usagés", "forfait", 1000, 4.00, 8.00),
        ),
    ),
    JobSpec(
        code="pneus_4",
        label="Montage de 4 pneumatiques + équilibrage + géométrie",
        trade="mecanique", hours_min_milli=600, hours_max_milli=900,
        parts=(
            P("Pneumatique 205/55 R16 91V", "u", 4000, 59.00, 139.00),
            P("Valve", "u", 4000, 3.00, 4.50),
            P("Équilibrage de roue", "u", 4000, 9.00, 14.00),
            P("Réglage de géométrie", "forfait", 1000, 59.00, 89.00),
            P("Recyclage pneumatiques usagés", "forfait", 1000, 8.00, 12.00),
        ),
    ),
    JobSpec(
        code="revision_60k",
        label="Révision constructeur 60 000 km",
        trade="mecanique", hours_min_milli=2200, hours_max_milli=3000,
        parts=(
            P("Huile moteur 5W-30", "L", 5000, 8.90, 11.50),
            P("Filtre à huile", "u", 1000, 9.00, 19.00),
            P("Filtre à air", "u", 1000, 14.00, 29.00),
            P("Filtre d’habitacle", "u", 1000, 12.00, 24.00),
            P("Filtre à carburant", "u", 1000, 25.00, 49.00),
            P("Bougie d’allumage", "u", 4000, 9.00, 22.00),
        ),
    ),
    JobSpec(
        code="climatisation",
        label="Recharge climatisation + désinfection du circuit",
        trade="mecanique", hours_min_milli=700, hours_max_milli=1000,
        parts=(
            P("Recharge gaz climatisation R134a", "forfait", 1000, 69.00, 119.00),
            P("Désinfection du circuit de climatisation", "forfait", 1000, 25.00, 39.00),
        ),
    ),
    JobSpec(
        code="batterie",
        label="Remplacement de la batterie",
        trade="mecanique", hours_min_milli=300, hours_max_milli=500,
        parts=(
            P("Batterie 12V 70Ah", "u", 1000, 89.00, 189.00),
        ),
    ),
    JobSpec(
        code="amortisseurs_ar",
        label="Remplacement des amortisseurs arrière",
        trade="mecanique", hours_min_milli=1600, hours_max_milli=2200,
        parts=(
            P("Amortisseur arrière", "u", 2000, 55.00, 129.00),
            P("Kit de butées et soufflets", "u", 1000, 29.00, 59.00),
        ),
    ),
    JobSpec(
        code="echappement",
        label="Remplacement de la ligne d’échappement arrière",
        trade="mecanique", hours_min_milli=1000, hours_max_milli=1500,
        parts=(
            P("Silencieux arrière", "u", 1000, 99.00, 249.00),
            P("Jeu de colliers et joints d’échappement", "u", 1000, 19.00, 39.00),
        ),
    ),
    JobSpec(
        code="diagnostic",
        label="Diagnostic électronique et recherche de panne",
        trade="diagnostic", hours_min_milli=1000, hours_max_milli=1500,
        labour_label=DIAG,
        parts=(
            P("Forfait recherche de panne", "forfait", 1000, 25.00, 45.00),
        ),
    ),
    JobSpec(
        code="demarreur",
        label="Remplacement du démarreur",
        trade="mecanique", hours_min_milli=1300, hours_max_milli=1800,
        parts=(
            P("Démarreur", "u", 1000, 139.00, 329.00),
        ),
    ),
    JobSpec(
        code="parebrise",
        label="Remplacement du pare-brise",
        trade="carrosserie", hours_min_milli=1300, hours_max_milli=1800,
        labour_label=CARR,
        parts=(
            P("Pare-brise", "u", 1000, 249.00, 489.00),
            P("Kit de collage pare-brise", "u", 1000, 29.00, 49.00),
        ),
    ),
    JobSpec(
        code="courroie_accessoires",
        label="Remplacement courroie d’accessoires + galet tendeur",
        trade="mecanique", hours_min_milli=1000, hours_max_milli=1400,
        parts=(
            P("Courroie d’accessoires", "u", 1000, 29.00, 69.00),
            P("Galet tendeur", "u", 1000, 39.00, 89.00),
        ),
    ),
    JobSpec(
        code="entretien_flotte",
        label="Entretien de flotte — vidange et filtration (3 véhicules)",
        trade="mecanique", hours_min_milli=2400, hours_max_milli=3200,
        for_fleet=True,
        parts=(
            P("Huile moteur 5W-30", "L", 15000, 8.90, 11.50),
            P("Filtre à huile", "u", 3000, 9.00, 19.00),
            P("Filtre à air", "u", 3000, 14.00, 29.00),
            P("Filtre d’habitacle", "u", 3000, 12.00, 24.00),
        ),
    ),
    JobSpec(
        code="freins_utilitaire",
        label="Remplacement disques et plaquettes (véhicule utilitaire)",
        trade="mecanique", hours_min_milli=2000, hours_max_milli=2600,
        for_fleet=True,
        parts=(
            P("Disque de frein avant renforcé", "u", 2000, 69.00, 129.00),
            P("Jeu de plaquettes de frein avant", "u", 1000, 55.00, 109.00),
            P("Disque de frein arrière", "u", 2000, 55.00, 99.00),
            P("Jeu de plaquettes de frein arrière", "u", 1000, 45.00, 89.00),
            P("Liquide de frein DOT 4", "L", 1000, 9.00, 14.00),
        ),
    ),
)

JOBS_BY_CODE = {j.code: j for j in JOBS}

# Jobs typically invoiced to private individuals vs to business fleets.
PRIVATE_JOBS: tuple[JobSpec, ...] = tuple(j for j in JOBS if not j.for_fleet)

_FLEET_SUITABLE = (
    "vidange_complete", "freins_av_complet", "distribution", "embrayage",
    "pneus_4", "revision_60k", "echappement", "diagnostic", "demarreur",
    "controle_technique", "carrosserie_aile", "parebrise",
)
FLEET_JOBS: tuple[JobSpec, ...] = tuple(
    j for j in JOBS if j.for_fleet or j.code in _FLEET_SUITABLE
)
