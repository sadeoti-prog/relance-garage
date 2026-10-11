"""The fictional customer base of Garage Martel & Fils.

Hand written rather than randomly assembled: a demo is a story, and the story
is better when the same customers come back with the same vehicles every time.

Safety: every email uses the reserved domain example.com and every phone number
sits in a range reserved for fiction, so this dataset can never reach a real
person even if the project is switched out of demo mode by mistake.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .config import DEFAULT_PENALTY_TERMS


@dataclass(frozen=True)
class VehicleSpec:
    plate: str
    make: str
    model: str
    year: int
    mileage_km: int


@dataclass(frozen=True)
class CustomerSpec:
    key: str
    name: str
    kind: str                 # 'particulier' | 'professionnel'
    civility: str             # 'M.' | 'Mme' | '' for companies
    contact_name: str         # who to address at a company
    email_local: str
    phone: str
    address: str
    postal_code: str
    city: str
    customer_since: int
    is_regular: bool
    vehicles: tuple[VehicleSpec, ...]
    # Penalty terms as actually agreed with THIS customer. `None` rate or
    # penalty_terms_confirmed=False means the garage has not validated them
    # and the system will claim no interest.
    late_penalty_rate_bp: int | None = None
    cgv_reference: str | None = None
    penalty_terms_confirmed: bool = False


def V(plate, make, model, year, km) -> VehicleSpec:
    return VehicleSpec(plate=plate, make=make, model=model, year=year, mileage_km=km)


_BASE_CUSTOMERS: tuple[CustomerSpec, ...] = (
    # ---------------------------------------------------------------- particuliers
    CustomerSpec(
        key="perrin", name="Nathalie Perrin", kind="particulier",
        civility="Mme", contact_name="Nathalie Perrin",
        email_local="n.perrin", phone="06 39 98 14 27",
        address="8 rue Émile Zola", postal_code="69120", city="Vaulx-en-Velin",
        customer_since=2017, is_regular=True,
        vehicles=(V("DJ-482-QR", "Renault", "Clio IV", 2016, 118_400),),
    ),
    CustomerSpec(
        key="benali", name="Karim Benali", kind="particulier",
        civility="M.", contact_name="Karim Benali",
        email_local="k.benali", phone="06 39 98 02 55",
        address="24 avenue Roger Salengro", postal_code="69100", city="Villeurbanne",
        customer_since=2021, is_regular=False,
        vehicles=(V("FL-217-BC", "Peugeot", "208", 2019, 62_300),),
    ),
    CustomerSpec(
        key="roux", name="Sébastien Roux", kind="particulier",
        civility="M.", contact_name="Sébastien Roux",
        email_local="s.roux", phone="06 39 98 41 08",
        address="3 impasse des Lilas", postal_code="69150", city="Décines-Charpieu",
        customer_since=2015, is_regular=True,
        vehicles=(V("CQ-905-TN", "Volkswagen", "Golf VII", 2014, 187_900),),
    ),
    CustomerSpec(
        key="dubois", name="Aurélie Dubois", kind="particulier",
        civility="Mme", contact_name="Aurélie Dubois",
        email_local="a.dubois", phone="06 39 98 63 71",
        address="17 rue de la République", postal_code="69500", city="Bron",
        customer_since=2019, is_regular=True,
        vehicles=(V("EH-338-VD", "Citroën", "C3", 2018, 74_600),),
    ),
    CustomerSpec(
        key="faure", name="Jean-Michel Faure", kind="particulier",
        civility="M.", contact_name="Jean-Michel Faure",
        email_local="jm.faure", phone="06 39 98 20 94",
        address="42 route de Genas", postal_code="69800", city="Saint-Priest",
        customer_since=2012, is_regular=True,
        vehicles=(V("DZ-761-KM", "Peugeot", "3008", 2017, 96_200),),
    ),
    CustomerSpec(
        key="lemoine", name="Sandrine Lemoine", kind="particulier",
        civility="Mme", contact_name="Sandrine Lemoine",
        email_local="s.lemoine", phone="06 39 98 77 30",
        address="5 allée des Peupliers", postal_code="69330", city="Meyzieu",
        customer_since=2020, is_regular=False,
        vehicles=(V("FB-604-XJ", "Dacia", "Sandero", 2020, 41_800),),
    ),
    CustomerSpec(
        key="chevalier", name="Thierry Chevalier", kind="particulier",
        civility="M.", contact_name="Thierry Chevalier",
        email_local="t.chevalier", phone="06 39 98 55 12",
        address="11 rue du Progrès", postal_code="69120", city="Vaulx-en-Velin",
        customer_since=2013, is_regular=True,
        vehicles=(V("BR-159-HD", "Renault", "Mégane III", 2013, 214_500),),
    ),
    CustomerSpec(
        key="garnier", name="Émilie Garnier", kind="particulier",
        civility="Mme", contact_name="Émilie Garnier",
        email_local="e.garnier", phone="06 39 98 08 46",
        address="9 rue Jean Jaurès", postal_code="69100", city="Villeurbanne",
        customer_since=2022, is_regular=False,
        vehicles=(V("GA-772-LP", "Toyota", "Yaris", 2021, 28_900),),
    ),
    CustomerSpec(
        key="marchand", name="Patrick Marchand", kind="particulier",
        civility="M.", contact_name="Patrick Marchand",
        email_local="p.marchand", phone="06 39 98 31 60",
        address="30 avenue Franklin Roosevelt", postal_code="69150",
        city="Décines-Charpieu",
        customer_since=2011, is_regular=True,
        vehicles=(V("DK-046-SR", "Ford", "Focus", 2015, 143_700),),
    ),
    CustomerSpec(
        key="colin", name="Laëtitia Colin", kind="particulier",
        civility="Mme", contact_name="Laëtitia Colin",
        email_local="l.colin", phone="06 39 98 92 05",
        address="6 rue des Écoles", postal_code="69680", city="Chassieu",
        customer_since=2018, is_regular=False,
        vehicles=(V("EA-593-WF", "Opel", "Corsa", 2017, 88_100),),
    ),
    CustomerSpec(
        key="ould_braham", name="Mehdi Ould-Braham", kind="particulier",
        civility="M.", contact_name="Mehdi Ould-Braham",
        email_local="m.ouldbraham", phone="06 39 98 47 83",
        address="21 rue Anatole France", postal_code="69100", city="Villeurbanne",
        customer_since=2016, is_regular=True,
        vehicles=(V("DN-820-GT", "Citroën", "C4 Picasso", 2016, 129_300),),
    ),
    CustomerSpec(
        key="barbier", name="Christelle Barbier", kind="particulier",
        civility="Mme", contact_name="Christelle Barbier",
        email_local="c.barbier", phone="06 39 98 15 39",
        address="14 chemin du Vieux Moulin", postal_code="69140",
        city="Rillieux-la-Pape",
        customer_since=2023, is_regular=False,
        vehicles=(V("GE-455-BM", "Renault", "Captur", 2022, 19_400),),
    ),
    CustomerSpec(
        key="renaud", name="Olivier Renaud", kind="particulier",
        civility="M.", contact_name="Olivier Renaud",
        email_local="o.renaud", phone="06 39 98 68 21",
        address="2 place de la Mairie", postal_code="69120", city="Vaulx-en-Velin",
        customer_since=2014, is_regular=True,
        vehicles=(V("CX-318-NV", "Skoda", "Octavia", 2015, 172_600),),
    ),
    CustomerSpec(
        key="diallo", name="Fatou Diallo", kind="particulier",
        civility="Mme", contact_name="Fatou Diallo",
        email_local="f.diallo", phone="06 39 98 73 94",
        address="38 rue Léon Blum", postal_code="69500", city="Bron",
        customer_since=2020, is_regular=False,
        vehicles=(V("FD-901-JC", "Nissan", "Qashqai", 2019, 67_500),),
    ),
    CustomerSpec(
        key="petit", name="Guillaume Petit", kind="particulier",
        civility="M.", contact_name="Guillaume Petit",
        email_local="g.petit", phone="06 39 98 26 17",
        address="7 rue des Vignes", postal_code="69740", city="Genas",
        customer_since=2019, is_regular=False,
        vehicles=(V("EM-274-PA", "BMW", "Série 1", 2018, 82_300),),
    ),
    CustomerSpec(
        key="marechal", name="Sylvie Maréchal", kind="particulier",
        civility="Mme", contact_name="Sylvie Maréchal",
        email_local="s.marechal", phone="06 39 98 84 52",
        address="19 avenue du Général de Gaulle", postal_code="69330", city="Meyzieu",
        customer_since=2010, is_regular=True,
        vehicles=(V("DT-687-RL", "Peugeot", "308", 2016, 105_900),),
    ),
    # ------------------------------------------------------------- professionnels
    CustomerSpec(
        key="toitures_vidal", name="SARL Toitures Vidal", kind="professionnel",
        civility="M.", contact_name="Christophe Vidal",
        email_local="compta.toitures-vidal", phone="04 65 71 33 08",
        address="ZA des Chênes, 4 rue des Charpentiers", postal_code="69720",
        city="Saint-Bonnet-de-Mure",
        customer_since=2016, is_regular=True,
        vehicles=(
            V("EV-129-DF", "Renault", "Master", 2018, 156_000),
            V("FN-703-QB", "Citroën", "Jumper", 2020, 98_400),
        ),
    ),
    CustomerSpec(
        key="fournil", name="Boulangerie Le Fournil d’Antan", kind="professionnel",
        civility="Mme", contact_name="Céline Coste",
        email_local="lefournildantan", phone="04 65 71 12 44",
        address="45 rue Émile Zola", postal_code="69120", city="Vaulx-en-Velin",
        customer_since=2018, is_regular=True,
        vehicles=(V("FC-560-HK", "Peugeot", "Partner", 2019, 71_200),),
    ),
    CustomerSpec(
        key="elec_rhone", name="SAS Élec Rhône Services", kind="professionnel",
        civility="M.", contact_name="Damien Tissot",
        email_local="administratif.elec-rhone", phone="04 65 71 58 90",
        address="8 rue de l’Industrie", postal_code="69800", city="Saint-Priest",
        customer_since=2019, is_regular=False,
        vehicles=(
            V("FT-338-ZP", "Renault", "Kangoo", 2021, 54_700),
            V("DW-641-MC", "Renault", "Trafic", 2017, 189_300),
        ),
    ),
    CustomerSpec(
        key="ambulances_ozon", name="Ambulances Val d’Ozon", kind="professionnel",
        civility="Mme", contact_name="Karine Brun",
        email_local="facturation.ambulances-ozon", phone="04 65 71 71 26",
        address="12 avenue de la Gare", postal_code="69360", city="Communay",
        customer_since=2021, is_regular=False,
        vehicles=(V("FJ-085-LR", "Volkswagen", "Transporter", 2020, 212_800),),
    ),
    CustomerSpec(
        key="plomberie_ducret", name="SARL Plomberie Ducret", kind="professionnel",
        civility="M.", contact_name="Franck Ducret",
        email_local="contact.plomberie-ducret", phone="04 65 71 09 63",
        address="27 rue des Artisans", postal_code="69150", city="Décines-Charpieu",
        customer_since=2014, is_regular=True,
        vehicles=(V("DP-472-YN", "Citroën", "Berlingo", 2016, 168_500),),
    ),
    CustomerSpec(
        key="autoecole_parc", name="Auto-École du Parc", kind="professionnel",
        civility="M.", contact_name="Yanis Sahli",
        email_local="contact.autoecoleduparc", phone="04 65 71 40 17",
        address="3 cours de la Liberté", postal_code="69100", city="Villeurbanne",
        customer_since=2017, is_regular=True,
        vehicles=(
            V("FR-916-XT", "Renault", "Clio V", 2021, 88_700),
            V("FG-204-CD", "Peugeot", "208", 2020, 96_100),
        ),
    ),
    CustomerSpec(
        key="saveurs_co", name="Traiteur Saveurs & Co", kind="professionnel",
        civility="Mme", contact_name="Linh Nguyen",
        email_local="compta.saveurs-et-co", phone="04 65 71 85 32",
        address="60 rue Paul Bert", postal_code="69003", city="Lyon",
        customer_since=2022, is_regular=False,
        vehicles=(V("FE-857-VK", "Fiat", "Ducato", 2019, 122_400),),
    ),
    CustomerSpec(
        key="espaces_verts_bonnet", name="SARL Espaces Verts Bonnet",
        kind="professionnel",
        civility="M.", contact_name="Ludovic Bonnet",
        email_local="contact.ev-bonnet", phone="04 65 71 62 75",
        address="Route de Jonage, lieu-dit Les Sablons", postal_code="69330",
        city="Meyzieu",
        customer_since=2015, is_regular=True,
        vehicles=(V("DS-390-BG", "Renault", "Master", 2016, 198_700),),
    ),
)

# ---------------------------------------------------------------------------
# Penalty terms on file, customer by customer
# ---------------------------------------------------------------------------
# There is no global rate. What a customer owes on a late payment depends on
# what that customer actually signed, and the terms are not the same for a
# company and for a private individual.
#
# Two customers below are deliberately left unconfirmed. A demo has to show
# what the system does when nobody validated the terms: it claims no interest
# at all and says so on the invoice line, rather than applying a rate quietly.

_UNCONFIRMED = {
    "benali":     "Aucune condition de pénalité acceptée sur l’ordre de réparation",
    "elec_rhone": "CGV professionnelles envoyées, retour signé jamais reçu",
}

# Rates negotiated away from the default with long-standing customers.
_NEGOTIATED = {
    "plomberie_ducret": 1_000,
    "toitures_vidal": 1_200,
}


def _with_terms(spec: CustomerSpec) -> CustomerSpec:
    defaults = DEFAULT_PENALTY_TERMS[spec.kind]

    if spec.key in _UNCONFIRMED:
        return replace(
            spec,
            late_penalty_rate_bp=None,
            cgv_reference=_UNCONFIRMED[spec.key],
            penalty_terms_confirmed=False,
        )

    if spec.kind == "professionnel":
        reference = f"{defaults['cgv_reference']} — contrat-cadre de {spec.customer_since}"
    else:
        reference = f"{defaults['cgv_reference']} depuis {spec.customer_since}"

    return replace(
        spec,
        late_penalty_rate_bp=_NEGOTIATED.get(spec.key, defaults["rate_bp"]),
        cgv_reference=reference,
        penalty_terms_confirmed=True,
    )


CUSTOMERS: tuple[CustomerSpec, ...] = tuple(_with_terms(c) for c in _BASE_CUSTOMERS)
CUSTOMERS_BY_KEY = {c.key: c for c in CUSTOMERS}
