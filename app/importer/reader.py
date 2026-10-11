"""Opening whatever file the garage actually has.

A garage owner exports from his management software and sends what comes out.
That file is a French CSV with semicolons in Windows-1252, or a UTF-8 one with
a byte-order mark, or an .xlsx straight from Excel. None of it is negotiable
from our side, so all of it is detected rather than assumed.

Every cell comes back as text. Numbers stored as floats in a spreadsheet are
converted through `Decimal(str(value))` — the shortest decimal string that
round-trips — so a float never reaches the money path. `values.parse_amount`
then works from the digits.
"""

from __future__ import annotations

import codecs
import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

# Candidates in the order a French export is likely to use them.
DELIMITERS = (";", ",", "\t", "|")

# Enough rows to judge a delimiter without reading a whole file.
_SNIFF_LINES = 40

# A ledger export beyond this is not something to load into a demo tool.
MAX_ROWS = 20_000

CSV_SUFFIXES = (".csv", ".txt", ".tsv")
EXCEL_SUFFIXES = (".xlsx", ".xlsm")


class FileProblem(Exception):
    """A file that cannot be opened at all. `reason` is shown to the user."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SourceFile:
    name: str
    kind: str                       # 'csv' | 'xlsx'
    headers: tuple[str, ...]
    rows: tuple[tuple[str | None, ...], ...]
    encoding: str | None = None     # csv only
    delimiter: str | None = None    # csv only
    sheet: str | None = None        # xlsx only
    truncated: bool = False
    notes: tuple[str, ...] = ()     # what had to be detected, in French

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def column(self, header: str) -> int | None:
        try:
            return self.headers.index(header)
        except ValueError:
            return None

    def cell(self, row: tuple[str | None, ...], header: str) -> str | None:
        index = self.column(header)
        if index is None or index >= len(row):
            return None
        return row[index]

    def preview(self, count: int = 5) -> tuple[tuple[str | None, ...], ...]:
        return self.rows[:count]


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def detect_encoding(data: bytes) -> tuple[str, str | None]:
    """Return (encoding, note). Never raises: something always decodes."""
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", "Fichier UTF-8 avec BOM."
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return "utf-16", "Fichier UTF-16."

    try:
        data.decode("utf-8")
        return "utf-8", None
    except UnicodeDecodeError:
        pass

    try:
        data.decode("cp1252")
        return "cp1252", "Fichier encodé en Windows-1252 (export Excel français)."
    except UnicodeDecodeError:
        pass

    # latin-1 maps every byte to a character: this cannot fail, and is the
    # difference between a readable "é" and a crash on the owner's file.
    return "latin-1", "Encodage non reconnu, lu en Latin-1. Vérifiez les accents."


# ---------------------------------------------------------------------------
# Delimiter
# ---------------------------------------------------------------------------

def detect_delimiter(text: str) -> tuple[str, str | None]:
    """Pick the separator that yields the most consistent table.

    A French Excel export uses ';'. Guessing ',' on such a file produces one
    single column and a useless mapping screen, so the choice is measured
    rather than assumed.
    """
    sample = [line for line in text.splitlines()[:_SNIFF_LINES] if line.strip()]
    if not sample:
        raise FileProblem("Le fichier est vide.")

    best_delimiter = None
    best_score = (0.0, 0)

    for candidate in DELIMITERS:
        try:
            counts = [
                len(row)
                for row in csv.reader(sample, delimiter=candidate)
                if row
            ]
        except csv.Error:
            continue
        if not counts:
            continue

        modal = max(set(counts), key=counts.count)
        if modal < 2:
            continue
        consistency = counts.count(modal) / len(counts)
        score = (consistency, modal)
        if score > best_score:
            best_score, best_delimiter = score, candidate

    if best_delimiter is None:
        raise FileProblem(
            "Impossible de reconnaître le séparateur de colonnes. "
            "Le fichier ne contient peut-être qu’une seule colonne."
        )

    note = None
    if best_delimiter != ",":
        readable = {";": "point-virgule", "\t": "tabulation", "|": "barre verticale"}
        note = f"Séparateur détecté : {readable.get(best_delimiter, best_delimiter)}."
    return best_delimiter, note


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------

def cell_to_text(value) -> str | None:
    """Every cell becomes text, and every number keeps its exact digits."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, datetime):
        if (value.hour, value.minute, value.second) == (0, 0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # The one place a float can appear. Decimal(str(x)) is the shortest
        # decimal that round-trips, so the digits below are the digits the
        # spreadsheet showed. From here on it is a string, and money.py's
        # integer arithmetic takes over.
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    text = str(value).strip()
    return text or None


def _clean_headers(raw_row: list) -> tuple[str, ...]:
    """Header labels as the user will see them, with blanks and clashes fixed."""
    headers: list[str] = []
    seen: dict[str, int] = {}
    for index, value in enumerate(raw_row):
        label = (cell_to_text(value) or "").strip()
        if not label:
            label = f"Colonne {index + 1}"
        if label in seen:
            seen[label] += 1
            label = f"{label} ({seen[label]})"
        else:
            seen[label] = 1
        headers.append(label)
    return tuple(headers)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def read_csv_bytes(data: bytes, name: str = "fichier.csv") -> SourceFile:
    encoding, encoding_note = detect_encoding(data)
    text = data.decode(encoding, errors="replace")
    delimiter, delimiter_note = detect_delimiter(text)

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)

    # Skip leading blank lines: some exports start with a title row. Keep only
    # the useful cap in memory instead of materialising an arbitrarily long
    # file and truncating it afterwards.
    header_row = None
    for raw in reader:
        if any((cell or "").strip() for cell in raw):
            header_row = raw
            break
    if header_row is None:
        raise FileProblem("Le fichier ne contient aucune ligne exploitable.")

    headers = _clean_headers(header_row)

    rows = []
    truncated = False
    for raw in reader:
        if not any((cell or "").strip() for cell in raw):
            continue
        if len(rows) >= MAX_ROWS:
            truncated = True
            break
        padded = list(raw) + [None] * (len(headers) - len(raw))
        rows.append(tuple(cell_to_text(cell) for cell in padded[:len(headers)]))

    notes = tuple(note for note in (encoding_note, delimiter_note) if note)
    return SourceFile(
        name=name, kind="csv", headers=headers, rows=tuple(rows),
        encoding=encoding, delimiter=delimiter, truncated=truncated, notes=notes,
    )


def read_excel_bytes(data: bytes, name: str = "fichier.xlsx",
                     sheet: str | None = None) -> SourceFile:
    try:
        import openpyxl
    except ImportError as error:                       # pragma: no cover
        raise FileProblem(
            "La lecture des fichiers Excel nécessite openpyxl. "
            "Lancez : pip install -r requirements.txt"
        ) from error

    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=True
        )
    except Exception as error:                         # noqa: BLE001
        raise FileProblem(
            "Fichier Excel illisible. S’il vient d’un très vieux logiciel, "
            "réenregistrez-le en .xlsx ou en CSV."
        ) from error

    try:
        worksheet = workbook[sheet] if sheet else workbook.active
        if worksheet is None:
            raise FileProblem("Le classeur ne contient aucune feuille.")

        iterator = worksheet.iter_rows(values_only=True)
        header_row = None
        for raw in iterator:
            if any(cell_to_text(cell) for cell in raw):
                header_row = list(raw)
                break
        if header_row is None:
            raise FileProblem("La feuille ne contient aucune ligne exploitable.")

        headers = _clean_headers(header_row)
        rows = []
        truncated = False
        for raw in iterator:
            values = [cell_to_text(cell) for cell in raw]
            if not any(values):
                continue
            if len(rows) >= MAX_ROWS:
                truncated = True
                break
            values += [None] * (len(headers) - len(values))
            rows.append(tuple(values[:len(headers)]))

        sheet_names = tuple(workbook.sheetnames)
        sheet_title = worksheet.title
    finally:
        workbook.close()

    notes = ()
    if len(sheet_names) > 1:
        notes = (
            f"Le classeur contient {len(sheet_names)} feuilles ; "
            f"« {sheet_title} » a été lue.",
        )

    return SourceFile(
        name=name, kind="xlsx", headers=headers, rows=tuple(rows),
        sheet=sheet_title, truncated=truncated, notes=notes,
    )


def read_bytes(data: bytes, name: str) -> SourceFile:
    """Read a file by its name's suffix."""
    suffix = Path(name).suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        return read_excel_bytes(data, name)
    if suffix in CSV_SUFFIXES or not suffix:
        return read_csv_bytes(data, name)
    raise FileProblem(
        f"Format non pris en charge : « {suffix} ». "
        "Exportez en CSV ou en Excel (.xlsx)."
    )


def read_path(path: str | Path) -> SourceFile:
    path = Path(path)
    if not path.exists():
        raise FileProblem(f"Fichier introuvable : {path}")
    return read_bytes(path.read_bytes(), path.name)
