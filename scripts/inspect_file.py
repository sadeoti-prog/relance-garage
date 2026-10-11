"""Show what the reader makes of an export file. Reads only — writes nothing.

    python scripts/inspect_file.py samples/export-logiciel-A.csv
    python scripts/inspect_file.py samples/export-logiciel-A.csv --parse
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.importer.convention import detect_amount_convention          # noqa: E402
from app.importer.reader import FileProblem, read_path                # noqa: E402
from app.importer.values import (                                     # noqa: E402
    ValueProblem, parse_amount, parse_date,
)
from app.money import format_eur                                      # noqa: E402

BAR = "─" * 100


def _shorten(value: str | None, width: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def show_parsing(source) -> None:
    """Try every cell as an amount and as a date, and report what sticks.

    This is the honest way to see whether a column is what it looks like,
    before any mapping is chosen.
    """
    print()
    print(BAR)
    print("  LECTURE DES COLONNES — ce que le système reconnaît")
    print(BAR)
    print(f"  {'Colonne':<26}{'Montants':>12}{'Dates':>10}   Exemples interprétés")
    print("  " + "-" * 98)

    for index, header in enumerate(source.headers):
        # The convention is read from the WHOLE column, not from the preview:
        # the value that settles it may be on line 300.
        whole_column = [row[index] for row in source.rows if index < len(row)]
        convention = detect_amount_convention(whole_column)

        values = [value for value in whole_column[:12] if value]
        if not values:
            print(f"  {_shorten(header, 24):<26}{'—':>12}{'—':>10}   (colonne vide)")
            continue

        # Both readings are tried independently: a column of Excel serials is
        # a valid amount AND a valid date, and hiding one of the two would make
        # the mapping screen look more certain than it is.
        amounts, dates = 0, 0
        amount_example = date_example = None
        for value in values:
            try:
                parsed = parse_amount(value, convention)
                amounts += 1
                if amount_example is None:
                    amount_example = f"{value} → {format_eur(parsed.cents)}"
            except ValueProblem:
                pass
            try:
                parsed_date = parse_date(value)
                dates += 1
                if date_example is None:
                    date_example = f"{value} → {parsed_date.value.strftime('%d/%m/%Y')}"
            except ValueProblem:
                pass

        share_amount = f"{round(100 * amounts / len(values))} %" if amounts else "—"
        if convention.blocks_import:
            share_amount = "ambigu"
        share_date = f"{round(100 * dates / len(values))} %" if dates else "—"
        examples = [item for item in (amount_example, date_example) if item]
        if not examples:
            examples = [_shorten(values[0], 40)]
        print(f"  {_shorten(header, 24):<26}{share_amount:>12}{share_date:>10}   "
              f"{_shorten(' · '.join(examples), 46)}")

    print()
    print("  Note : un même texte peut se lire comme un montant ET comme une date")
    print("  (« 46022 »). C’est l’écran de correspondance qui tranche, pas la machine.")

    print()
    print("  Convention des séparateurs, colonne par colonne :")
    for index, header in enumerate(source.headers):
        column = [row[index] for row in source.rows if index < len(row)]
        convention = detect_amount_convention(column)
        if convention.sample_count and (convention.settled or convention.has_ambiguous):
            flag = "  ⚠" if convention.blocks_import else "   "
            print(f"  {flag} {_shorten(header, 22):<24}{convention.evidence}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1

    try:
        source = read_path(argv[0])
    except FileProblem as error:
        print(f"ÉCHEC : {error.reason}")
        return 1

    print(BAR)
    print(f"  {source.name}")
    print(BAR)
    print(f"  Format         : {source.kind}")
    if source.encoding:
        print(f"  Encodage       : {source.encoding}")
    if source.delimiter:
        readable = {";": "point-virgule", ",": "virgule", "\t": "tabulation",
                    "|": "barre verticale"}
        print(f"  Séparateur     : {readable.get(source.delimiter, source.delimiter)}")
    if source.sheet:
        print(f"  Feuille        : {source.sheet}")
    print(f"  Colonnes       : {len(source.headers)}")
    print(f"  Lignes de données : {source.row_count}"
          + ("  (tronqué)" if source.truncated else ""))
    for note in source.notes:
        print(f"  → {note}")

    print()
    print("  En-têtes trouvés :")
    for index, header in enumerate(source.headers, start=1):
        print(f"    {index:>2}. {header}")

    width = max(12, min(20, 96 // max(1, len(source.headers))))
    print()
    print("  5 premières lignes, telles que lues :")
    print("  " + "-" * 98)
    for row in source.preview(5):
        cells = [_shorten(cell, width) for cell in row]
        print("  " + " | ".join(f"{cell:<{width}}" for cell in cells)[:98])

    if "--parse" in argv:
        show_parsing(source)

    print()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main(sys.argv[1:]))
