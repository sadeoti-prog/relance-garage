"""Build example export files, in the formats garages actually send.

These are not test fixtures dressed up as demo data: they are the awkward cases
on purpose — semicolons, Windows-1252, a UTF-8 BOM, Excel serial dates, amounts
in four different notations, a duplicate invoice number, a customer with no
email, an invoice already paid.

    python scripts/make_samples.py

Writes into samples/. Safe to run at any time; it touches no database.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SAMPLES = Path(__file__).resolve().parent.parent / "samples"

NBSP = chr(0x00A0)

# --- 1. The French classic: semicolons, Windows-1252, dd/mm/yyyy ------------
CSV_FR = """N° facture;Client;Email;Date facture;Échéance;Véhicule;Immatriculation;Prestation;Total TTC;Réglé
FA-2026-0412;Boulangerie Chez Léa;compta@chez-lea.example.com;12/06/2026;12/07/2026;Renault Kangoo;FT-338-ZP;Révision 60 000 km;1 284,60 €;0
FA-2026-0413;M. Hervé Lecoq;h.lecoq@example.com;18/06/2026;03/07/2026;Peugeot 308;DT-687-RL;Plaquettes avant;189,90 €;0
FA-2026-0414;Mme Anne Vasseur;;24/06/2026;09/07/2026;Citroën C3;EH-338-VD;Contrôle technique;98,00 €;0
FA-2026-0415;SARL Menuiserie Thibault;facturation@thibault-bois.example.com;02/07/2026;01/08/2026;Citroën Jumper;FN-703-QB;Embrayage;2 340,00 €;500,00 €
FA-2026-0416;M. Hervé Lecoq;h.lecoq@example.com;09/07/2026;24/07/2026;Peugeot 308;DT-687-RL;Vidange;96,50 €;96,50 €
FA-2026-0413;M. Hervé Lecoq;h.lecoq@example.com;18/06/2026;03/07/2026;Peugeot 308;DT-687-RL;Plaquettes avant (doublon);189,90 €;0
FA-2026-0417;Ambulances Saint-Roch;compta@ambulances-st-roch.example.com;15/07/2026;14/08/2026;Volkswagen Transporter;FJ-085-LR;Distribution;1{nbsp}890,00;0
FA-2026-0418;M. Damien Ferry;d.ferry@example.com;21/07/2026;;Dacia Duster;FB-604-XJ;Amortisseurs arrière;437,20;0
FA-2026-0419;Pressing du Centre;contact@pressing-centre.example.com;28/07/2026;27/08/2026;Renault Trafic;DW-641-MC;Pare-brise;;0
FA-2026-0420;M. Bruno Salvi;b.salvi@example.com;03/08/2026;18/08/2026;Toyota Yaris;GA-772-LP;Climatisation;pas un montant;0
""".replace("{nbsp}", NBSP)

# --- 2. Another software: commas, UTF-8 with BOM, ISO dates, HT + TVA -------
CSV_ISO = """invoice_no,customer,customer_email,issue_date,net_amount,vat_amount,gross_amount,paid_amount,vehicle
2026-0912,Garage Central Pièces Auto,contact@central-pieces.example.com,2026-06-30,1070.00,214.00,1284.00,0.00,Fiat Ducato
2026-0913,Élodie Marchetti,e.marchetti@example.com,2026-07-08,158.25,31.65,189.90,0.00,Opel Corsa
2026-0914,Transports Bugeaud,compta@bugeaud-transports.example.com,2026-07-15,1950.00,390.00,2340.00,780.00,Renault Master
2026-0915,Sébastien Nkemé,s.nkeme@example.com,2026-07-22,80.42,16.08,96.50,96.50,Ford Fiesta
2026-0916,Le Comptoir Fleuri,gerance@comptoir-fleuri.example.com,2026-08-01,364.33,72.87,437.20,0.00,Peugeot Partner
"""



# --- The same software, one week later -------------------------------------
# What the second import actually looks like: the same invoices again, one of
# them now settled, one new one, and two that the shorter export no longer
# mentions. None of the absent ones may be deleted.
CSV_FR_WEEK2 = """N° facture;Client;Email;Date facture;Échéance;Véhicule;Immatriculation;Prestation;Total TTC;Réglé
FA-2026-0413;M. Hervé Lecoq;h.lecoq@example.com;18/06/2026;03/07/2026;Peugeot 308;DT-687-RL;Plaquettes avant;189,90 €;0
FA-2026-0414;Mme Anne Vasseur;;24/06/2026;09/07/2026;Citroën C3;EH-338-VD;Contrôle technique;98,00 €;98,00 €
FA-2026-0415;SARL Menuiserie Thibault;facturation@thibault-bois.example.com;02/07/2026;01/08/2026;Citroën Jumper;FN-703-QB;Embrayage;2 340,00 €;500,00 €
FA-2026-0416;M. Hervé Lecoq;h.lecoq@example.com;09/07/2026;24/07/2026;Peugeot 308;DT-687-RL;Vidange;96,50 €;96,50 €
FA-2026-0417;Ambulances Saint-Roch;compta@ambulances-st-roch.example.com;15/07/2026;14/08/2026;Volkswagen Transporter;FJ-085-LR;Distribution;1{nbsp}890,00;1{nbsp}890,00
FA-2026-0418;M. Damien Ferry;d.ferry@example.com;21/07/2026;;Dacia Duster;FB-604-XJ;Amortisseurs arrière;437,20;0
FA-2026-0422;BOULANGERIE CHEZ LEA S.A.R.L.;compta@chez-lea.example.com;10/08/2026;09/09/2026;Renault Kangoo;FT-338-ZP;Freins avant;312,50 €;0
""".replace("{nbsp}", NBSP)


# --- Three files that exercise the column-convention rule -------------------
# The separator question is settled by the column, never by the value. These
# three make the rule visible: one settles as decimal, one as thousands, one
# does not settle at all and must be refused.

CSV_CONVENTION_DECIMAL = """Numero;Client;Date;Total
D-001;Boucherie Vasseur;04/07/2026;189,90
D-002;M. Paul Nguyen;11/07/2026;1,234
D-003;Mme Irène Kaczmarek;18/07/2026;98,00
D-004;Taxis Belmont;25/07/2026;2 340,00
"""

CSV_CONVENTION_THOUSANDS = """Numero;Client;Date;Total
T-001;Ets Rouvier;04/07/2026;189.90
T-002;M. Paul Nguyen;11/07/2026;1,234
T-003;Mme Irène Kaczmarek;18/07/2026;98.00
T-004;Taxis Belmont;25/07/2026;12,345
"""

CSV_CONVENTION_UNDECIDED = """Numero;Client;Date;Total
A-001;Ets Rouvier;04/07/2026;1,234
A-002;M. Paul Nguyen;11/07/2026;5,678
A-003;Mme Irène Kaczmarek;18/07/2026;2,500
"""

def build_excel(path: Path) -> None:
    """A third format: Excel serial dates and native numeric cells."""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Factures"

    sheet.append([
        "Num", "Nom client", "Mail", "Date", "Echeance",
        "Vehicule", "Plaque", "Travaux", "Montant TTC", "Acompte",
    ])

    # Dates as raw Excel serials, the way a bad export leaves them.
    rows = [
        ("F-2026-1001", "SARL Couverture Riviera", "compta@couverture-riviera.example.com",
         46180, 46210, "Renault Master", "DS-390-BG", "Freins avant + arrière", 1284.6, 0),
        ("F-2026-1002", "Mme Chantal Ropert", "c.ropert@example.com",
         46188, 46203, "Skoda Fabia", "CX-318-NV", "Batterie", 189.9, 0),
        ("F-2026-1003", "M. Yacine Tahiri", "y.tahiri@example.com",
         46195, 46210, "BMW Série 1", "EM-274-PA", "Pneumatiques x4", 812.0, 0),
        ("F-2026-1004", "Fromagerie Bellon", "contact@fromagerie-bellon.example.com",
         46202, 46232, "Citroën Berlingo", "DP-472-YN", "Distribution + pompe à eau", 1890.0, 600),
        ("F-2026-1005", "M. Olivier Sanz", "o.sanz@example.com",
         46209, 46224, "Renault Clio V", "FR-916-XT", "Vidange + filtres", 96.5, 96.5),
    ]
    for row in rows:
        sheet.append(list(row))

    workbook.save(path)


def main() -> int:
    SAMPLES.mkdir(exist_ok=True)

    french = SAMPLES / "export-logiciel-A.csv"
    french.write_bytes(CSV_FR.encode("cp1252"))

    iso = SAMPLES / "export-logiciel-B.csv"
    iso.write_bytes(b"\xef\xbb\xbf" + CSV_ISO.encode("utf-8"))

    excel = SAMPLES / "export-logiciel-C.xlsx"
    build_excel(excel)

    week2 = SAMPLES / "export-logiciel-A-semaine2.csv"
    week2.write_bytes(CSV_FR_WEEK2.encode("cp1252"))

    convention_files = {
        "convention-decimale.csv": CSV_CONVENTION_DECIMAL,
        "convention-milliers.csv": CSV_CONVENTION_THOUSANDS,
        "convention-indecidable.csv": CSV_CONVENTION_UNDECIDED,
    }
    for filename, content in convention_files.items():
        (SAMPLES / filename).write_bytes(content.encode("cp1252"))

    print("Fichiers d’exemple créés dans samples/ :")
    for path in (french, week2, iso, excel,
                 *(SAMPLES / name for name in convention_files)):
        print(f"  {path.name:<28}{path.stat().st_size:>7} octets")
    print()
    print("Ils contiennent volontairement des cas pénibles : point-virgule,")
    print("Windows-1252, BOM UTF-8, dates en série Excel, doublon de numéro,")
    print("client sans e-mail, facture déjà réglée, montant illisible.")
    print()
    print("export-logiciel-A-semaine2.csv est le MEME export une semaine plus")
    print("tard : une facture réglée, une nouvelle, deux absentes. C'est le")
    print("fichier qui prouve que le second import réconcilie au lieu d'insérer.")
    print()
    print("Les trois fichiers convention-*.csv portent sur une seule")
    print("question : ce que « 1,234 » veut dire dans CE fichier.")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main())
