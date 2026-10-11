"""Saying what each column of the file means.

Twenty garage management tools export twenty different headers for the same
thing. Rather than write twenty connectors, the system shows the owner his own
column names and asks him what they are — with a guess already filled in.

The guess uses two signals: the header's wording, and what the column actually
contains. A column called `Date` that holds dates is a strong candidate; one
called `Total` that holds no readable amount is not a candidate at all,
whatever it is named.

Once corrected, the mapping is saved under a profile name and matched back to
the next file from the same software by its headers. That is what covers the
twenty tools.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .convention import detect_amount_convention
from .values import ValueProblem, parse_amount, parse_date

# A guess below this is not offered: an empty box is better than a wrong one
# the owner has to notice and undo.
MIN_SCORE = 45


@dataclass(frozen=True)
class Field:
    key: str
    label: str                 # French, shown on the mapping screen
    required: bool
    kind: str                  # 'text' | 'amount' | 'date' | 'email'
    hint: str                  # French
    synonyms: tuple[str, ...]


FIELDS: tuple[Field, ...] = (
    Field("number", "Numéro de facture", True, "text",
          "Identifie la facture. Un doublon est refusé.",
          ("numero facture", "numero", "num facture", "num", "no facture", "no",
           "n facture", "facture", "invoice no", "invoice number", "invoice",
           "reference", "ref", "piece", "document", "id facture")),
    Field("customer_name", "Nom du client", True, "text",
          "Le nom tel qu’il apparaîtra dans la relance.",
          ("client", "nom client", "nom", "customer", "customer name",
           "raison sociale", "tiers", "compte", "destinataire", "societe")),
    Field("issue_date", "Date de facture", True, "date",
          "Sert à calculer l’échéance si elle n’est pas dans le fichier.",
          ("date facture", "date", "date emission", "emission", "issue date",
           "invoice date", "date piece", "date document")),
    Field("total_ttc", "Total TTC", True, "amount",
          "Le montant réclamé au client, toutes taxes comprises.",
          ("total ttc", "ttc", "montant ttc", "total", "montant", "gross",
           "gross amount", "total amount", "amount", "total general")),

    Field("due_date", "Échéance", False, "date",
          "Si absente, elle est calculée et signalée comme telle.",
          ("echeance", "date echeance", "due date", "due", "date limite",
           "limite", "date reglement", "payable le")),
    Field("amount_paid", "Déjà réglé", False, "amount",
          "Acompte ou règlement partiel déjà encaissé.",
          ("regle", "deja regle", "paye", "acompte", "paid", "paid amount",
           "encaisse", "reglement", "montant regle", "avance")),
    Field("total_ht", "Total HT", False, "amount",
          "Facultatif. Sert à vérifier la cohérence avec le TTC.",
          ("total ht", "ht", "montant ht", "net", "net amount", "hors taxes")),
    Field("vat_amount", "TVA", False, "amount",
          "Facultatif. Sert à vérifier la cohérence avec le TTC.",
          ("tva", "vat", "vat amount", "taxe", "montant tva", "tax")),

    Field("email", "E-mail du client", False, "email",
          "Sans e-mail, le client reste relançable par courrier seulement.",
          ("email", "e mail", "mail", "courriel", "customer email",
           "adresse mail", "adresse email", "mel")),
    Field("phone", "Téléphone", False, "text",
          "Facultatif, affiché dans la file de validation.",
          ("telephone", "tel", "phone", "portable", "mobile", "gsm")),
    Field("vehicle", "Véhicule", False, "text",
          "Marque et modèle, cités dans la relance.",
          ("vehicule", "vehicle", "modele", "marque", "voiture", "auto",
           "marque modele")),
    Field("plate", "Immatriculation", False, "text",
          "Citée dans la relance pour que le client identifie la facture.",
          ("immatriculation", "immat", "plaque", "plate", "registration",
           "numero immatriculation")),
    Field("job_label", "Prestation", False, "text",
          "Ce qui a été fait sur le véhicule.",
          ("prestation", "travaux", "libelle", "designation", "description",
           "objet", "intitule", "nature", "job")),
    Field("address", "Adresse", False, "text",
          "Nécessaire pour imprimer une mise en demeure.",
          ("adresse", "address", "rue", "voie", "adresse1", "adresse client")),
    Field("postal_code", "Code postal", False, "text",
          "Nécessaire pour imprimer une mise en demeure.",
          ("code postal", "cp", "postal code", "zip", "postcode")),
    Field("city", "Ville", False, "text",
          "Nécessaire pour imprimer une mise en demeure.",
          ("ville", "city", "commune", "localite")),
)

FIELDS_BY_KEY = {item.key: item for item in FIELDS}
REQUIRED_KEYS = tuple(item.key for item in FIELDS if item.required)


# ---------------------------------------------------------------------------
# Header matching
# ---------------------------------------------------------------------------

def normalise(label: str) -> str:
    """`N° facture` and `num_facture` both become `n facture` / `num facture`."""
    text = unicodedata.normalize("NFKD", str(label or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("°", " ").replace("º", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _name_score(header: str, synonyms: tuple[str, ...]) -> int:
    clean = normalise(header)
    if not clean:
        return 0
    best = 0
    header_tokens = set(clean.split())
    for synonym in synonyms:
        if clean == synonym:
            return 100
        if clean.startswith(synonym) or synonym.startswith(clean):
            best = max(best, 82)
        elif synonym in clean:
            best = max(best, 72)
        else:
            tokens = set(synonym.split())
            if tokens and tokens <= header_tokens:
                best = max(best, 66)
            elif tokens & header_tokens:
                overlap = len(tokens & header_tokens) / len(tokens | header_tokens)
                best = max(best, int(55 * overlap))
    return best


def _content_signal(values: list[str | None], kind: str) -> int:
    """What the column's contents say, independently of its name.

    Returns a bonus, or a large negative when the contents rule the field out.
    """
    present = [value for value in values if value]
    if not present:
        return 0
    sample = present[:60]

    if kind == "email":
        share = sum(1 for value in sample if "@" in value) / len(sample)
        return 45 if share >= 0.6 else (-100 if share == 0 else 0)

    if kind == "amount":
        convention = detect_amount_convention(present)
        readable = 0
        for value in sample:
            try:
                parse_amount(value, convention)
                readable += 1
            except ValueProblem:
                pass
        share = readable / len(sample)
        if share >= 0.8:
            return 28
        return -100 if share < 0.2 else 0

    if kind == "date":
        readable = 0
        for value in sample:
            try:
                parse_date(value)
                readable += 1
            except ValueProblem:
                pass
        share = readable / len(sample)
        if share >= 0.8:
            return 28
        return -100 if share < 0.2 else 0

    return 0


@dataclass
class Suggestion:
    field_key: str
    header: str | None
    score: int
    reason: str                 # French, shown next to the guess


def guess_mapping(source) -> dict[str, Suggestion]:
    """Best guess for every field, from header wording plus column contents."""
    columns = {
        header: [row[index] for row in source.rows if index < len(row)]
        for index, header in enumerate(source.headers)
    }

    scores: list[tuple[int, str, str]] = []
    for item in FIELDS:
        for header, values in columns.items():
            name = _name_score(header, item.synonyms)
            content = _content_signal(values, item.kind)
            if content <= -100:
                continue                      # contents rule this field out
            total = name + content
            if name == 0 and content < 40:
                continue                      # nothing in the name to go on
            if total >= MIN_SCORE:
                scores.append((total, item.key, header))

    scores.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))

    chosen: dict[str, Suggestion] = {}
    used_headers: set[str] = set()
    for total, field_key, header in scores:
        if field_key in chosen or header in used_headers:
            continue
        item = FIELDS_BY_KEY[field_key]
        name = _name_score(header, item.synonyms)
        reason = (
            "d’après le nom de la colonne" if name >= 66
            else "d’après le contenu de la colonne"
        )
        chosen[field_key] = Suggestion(field_key, header, total, reason)
        used_headers.add(header)

    for item in FIELDS:
        chosen.setdefault(item.key, Suggestion(item.key, None, 0, ""))
    return chosen


# ---------------------------------------------------------------------------
# Validating a mapping
# ---------------------------------------------------------------------------

@dataclass
class MappingCheck:
    ok: bool
    missing: tuple[str, ...] = ()          # French labels of missing required fields
    duplicates: tuple[str, ...] = ()       # headers used twice
    unknown: tuple[str, ...] = ()          # headers that are not in the file

    @property
    def message(self) -> str | None:
        parts = []
        if self.missing:
            parts.append("Colonnes obligatoires non renseignées : "
                         + ", ".join(self.missing) + ".")
        if self.duplicates:
            parts.append("Une même colonne est utilisée deux fois : "
                         + ", ".join(self.duplicates) + ".")
        if self.unknown:
            parts.append("Colonnes inconnues dans ce fichier : "
                         + ", ".join(self.unknown) + ".")
        return " ".join(parts) or None


def check_mapping(mapping: dict[str, str | None], headers) -> MappingCheck:
    headers = tuple(headers)
    assigned = [header for header in mapping.values() if header]

    missing = tuple(
        FIELDS_BY_KEY[key].label
        for key in REQUIRED_KEYS
        if not mapping.get(key)
    )
    duplicates = tuple(
        sorted({header for header in assigned if assigned.count(header) > 1})
    )
    unknown = tuple(
        sorted({header for header in assigned if header not in headers})
    )
    return MappingCheck(
        ok=not (missing or duplicates or unknown),
        missing=missing, duplicates=duplicates, unknown=unknown,
    )


# ---------------------------------------------------------------------------
# Saved profiles
# ---------------------------------------------------------------------------

@dataclass
class Profile:
    name: str
    headers: list[str]
    mapping: dict[str, str]
    created_at: str
    updated_at: str = ""
    kind: str = "csv"
    use_count: int = 0
    payment_term_days: int | None = None    # answered once, not asked again
    notes: list[str] = field(default_factory=list)

    def fingerprint(self) -> frozenset[str]:
        return frozenset(normalise(header) for header in self.headers if header)


class ProfileStore:
    """Mapping profiles, in a plain JSON file next to the databases.

    Deliberately not in the demo database: a profile is a setting, not data,
    and regenerating the demo must never lose the owner's mappings.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[Profile]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        profiles = []
        for entry in payload.get("profiles", []):
            try:
                profiles.append(Profile(**entry))
            except TypeError:
                continue
        return profiles

    def save_all(self, profiles: list[Profile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"profiles": [asdict(profile) for profile in profiles]}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save(self, name: str, headers, mapping: dict[str, str | None],
             kind: str = "csv", payment_term_days: int | None = None) -> Profile:
        now = datetime.now().isoformat(timespec="seconds")
        cleaned = {key: value for key, value in mapping.items() if value}
        profiles = self.load()

        for existing in profiles:
            if existing.name == name:
                existing.headers = list(headers)
                existing.mapping = cleaned
                existing.updated_at = now
                existing.kind = kind
                existing.payment_term_days = payment_term_days
                self.save_all(profiles)
                return existing

        profile = Profile(
            name=name, headers=list(headers), mapping=cleaned,
            created_at=now, updated_at=now, kind=kind,
            payment_term_days=payment_term_days,
        )
        profiles.append(profile)
        self.save_all(profiles)
        return profile

    def delete(self, name: str) -> bool:
        profiles = self.load()
        remaining = [profile for profile in profiles if profile.name != name]
        if len(remaining) == len(profiles):
            return False
        self.save_all(remaining)
        return True

    def match(self, headers) -> tuple[Profile | None, str]:
        """Find the profile that fits these headers, and say how well.

        An exact set of headers is a certain match. Otherwise the best profile
        whose mapped columns all exist in the file is offered — that covers a
        software adding a column between two exports.
        """
        fingerprint = frozenset(normalise(header) for header in headers if header)
        available = set(headers)
        best: tuple[Profile | None, float] = (None, 0.0)

        for profile in self.load():
            if profile.fingerprint() == fingerprint:
                return profile, "exact"
            mapped = set(profile.mapping.values())
            if mapped and mapped <= available:
                overlap = len(profile.fingerprint() & fingerprint)
                share = overlap / max(1, len(profile.fingerprint() | fingerprint))
                if share > best[1]:
                    best = (profile, share)

        if best[0] is not None and best[1] >= 0.5:
            return best[0], "partiel"
        return None, "aucun"

    def record_use(self, name: str) -> None:
        profiles = self.load()
        for profile in profiles:
            if profile.name == name:
                profile.use_count += 1
                self.save_all(profiles)
                return
