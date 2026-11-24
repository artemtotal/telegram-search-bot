"""PDF of «📁 Папка заявника» (25.11.2026): one German document a landlord can read.

Pages: title, cover letter (Anschreiben), Mieterselbstauskunft, list of
documents to attach. Only facts the person gave are printed; a question they
skipped becomes an empty line (or an empty Ja/Nein box) to fill in by hand.

The font is DejaVu Sans - it has umlauts, Cyrillic (names and addresses are
often written in Ukrainian) and the check boxes. It ships inside matplotlib,
which the bot already needs; MAPPE_FONT_DIR can point elsewhere.
"""

import os
from typing import Dict, List

LEFT = 20
RIGHT = 20
LABEL_W = 62
LINE_H = 6
TEXT_RGB = (30, 30, 30)
MUTED_RGB = (110, 110, 110)
ACCENT_RGB = (35, 70, 110)
RULE_RGB = (190, 195, 200)

BOX_ON = "☒"
BOX_OFF = "☐"


def font_dir() -> str:
    custom = os.getenv("MAPPE_FONT_DIR")
    if custom and os.path.isdir(custom):
        return custom
    import matplotlib  # noqa: PLC0415 - heavy import, only when a PDF is built

    return os.path.join(matplotlib.get_data_path(), "fonts", "ttf")


def available() -> bool:
    try:
        import fpdf  # noqa: F401, PLC0415

        return os.path.isfile(os.path.join(font_dir(), "DejaVuSans.ttf"))
    except Exception:
        return False


def _text(value) -> str:
    return str(value or "").strip()


def occupation_label(kind: str) -> str:
    return {"works": "Berufstätig", "jobcenter": "Leistungsbezug (Jobcenter)", "other": "Sonstiges"}.get(kind, "")


def persons_line(adults: str, children: str) -> str:
    parts = []
    if adults:
        parts.append(f"{adults} {'Erwachsener' if adults == '1' else 'Erwachsene'}")
    if children and children != "0":
        parts.append(f"{children} {'Kind' if children == '1' else 'Kinder'}")
    elif children == "0" and adults:
        parts.append("keine Kinder")
    return ", ".join(parts)


def documents_list(occupation: str) -> List[str]:
    """What to attach, in German - depends on what the person does."""
    if occupation == "works":
        docs = ["Gehaltsabrechnungen der letzten 3 Monate"]
    elif occupation == "jobcenter":
        docs = ["Aktueller Bescheid des Jobcenters", "Mietübernahmebescheinigung des Jobcenters"]
    elif occupation == "other":
        docs = ["Einkommensnachweis"]
    else:
        docs = ["Einkommensnachweis (Gehaltsabrechnungen oder Bescheid des Jobcenters)"]
    docs += [
        "Kopie des Ausweisdokuments (Pass oder Personalausweis, ggf. Aufenthaltstitel)",
        "SCHUFA-Auskunft (BonitätsAuskunft)",
        "Mietschuldenfreiheitsbescheinigung des bisherigen Vermieters",
    ]
    return docs


def build_pdf(data: Dict[str, object]) -> bytes:
    """`data` holds ready German strings (see housing_mappe.collect_data)."""
    from fpdf import FPDF  # noqa: PLC0415

    fonts = font_dir()
    name = _text(data.get("name"))

    class Pdf(FPDF):
        def footer(self):
            self.set_y(-14)
            self.set_font("DejaVu", "", 8)
            self.set_text_color(*MUTED_RGB)
            who = f"{name} · " if name else ""
            self.cell(0, 8, f"{who}Bewerbungsmappe · Seite {self.page_no()}/{{nb}}", align="C")

    pdf = Pdf(format="A4")
    pdf.set_margins(LEFT, 18, RIGHT)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_font("DejaVu", "", os.path.join(fonts, "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", os.path.join(fonts, "DejaVuSans-Bold.ttf"))
    pdf.alias_nb_pages()
    right_edge = pdf.w - RIGHT
    width = pdf.w - LEFT - RIGHT

    def font(size: float, bold: bool = False, rgb=TEXT_RGB) -> None:
        pdf.set_font("DejaVu", "B" if bold else "", size)
        pdf.set_text_color(*rgb)

    def rule(gap: float = 3) -> None:
        pdf.ln(gap)
        pdf.set_draw_color(*RULE_RGB)
        pdf.line(LEFT, pdf.get_y(), right_edge, pdf.get_y())
        pdf.ln(gap)

    def need(height: float) -> None:
        if pdf.get_y() + height > pdf.h - 20:
            pdf.add_page()

    def lines_of(w: float, h: float, txt: str) -> int:
        return max(1, len(pdf.multi_cell(w, h, txt, dry_run=True, output="LINES")))

    def heading(text: str, size: float = 18) -> None:
        font(size, True, ACCENT_RGB)
        pdf.multi_cell(0, size * 0.55, text, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    def subheading(text: str) -> None:
        need(20)
        pdf.ln(3)
        font(11, True, ACCENT_RGB)
        pdf.multi_cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*RULE_RGB)
        pdf.line(LEFT, pdf.get_y(), right_edge, pdf.get_y())
        pdf.ln(2.5)

    def row(label: str, value: str = "") -> None:
        value = _text(value)
        lh = LINE_H
        value_w = width - LABEL_W
        font(9.5, True, MUTED_RGB)  # measure with the fonts the row is drawn in
        label_lines = lines_of(LABEL_W - 2, lh, label)
        font(10.5)
        height = max(label_lines, lines_of(value_w, lh, value) if value else 1) * lh
        need(height + 3)
        y0 = pdf.get_y()
        font(9.5, True, MUTED_RGB)
        pdf.set_xy(LEFT, y0)
        pdf.multi_cell(LABEL_W - 2, lh, label, new_x="RIGHT", new_y="TOP")
        font(10.5)
        pdf.set_xy(LEFT + LABEL_W, y0)
        if value:
            pdf.multi_cell(value_w, lh, value, new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.set_draw_color(120, 120, 120)
            pdf.line(LEFT + LABEL_W, y0 + lh - 1, right_edge, y0 + lh - 1)
        pdf.set_y(y0 + height + 2.5)

    def statement(text: str) -> None:
        font(10.5)
        need(LINE_H + 3)
        pdf.multi_cell(0, LINE_H, text, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2.5)

    def yes_no(question: str, answer: str) -> None:
        box_w = 42
        q_w = width - box_w
        font(10.5)
        height = lines_of(q_w - 2, LINE_H, question) * LINE_H
        need(height + 3)
        y0 = pdf.get_y()
        font(10.5)
        pdf.set_xy(LEFT, y0)
        pdf.multi_cell(q_w - 2, LINE_H, question, new_x="RIGHT", new_y="TOP")
        yes = BOX_ON if answer == "yes" else BOX_OFF
        no = BOX_ON if answer == "no" else BOX_OFF
        pdf.set_xy(LEFT + q_w, y0)
        pdf.cell(box_w, LINE_H, f"{yes} Ja     {no} Nein")
        pdf.set_y(y0 + height + 2.5)

    # ---- 1. title page ----
    pdf.add_page()
    pdf.ln(28)
    font(30, True, ACCENT_RGB)
    pdf.cell(0, 14, "Bewerbungsmappe", new_x="LMARGIN", new_y="NEXT")
    font(12, False, MUTED_RGB)
    pdf.cell(0, 8, "für die Anmietung einer Wohnung", new_x="LMARGIN", new_y="NEXT")
    rule(5)
    font(22, True)
    pdf.multi_cell(0, 11, name or "Name: ______________________", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    listing = data.get("listing") if isinstance(data.get("listing"), dict) else None
    if listing:
        row("Bewerbung um", " · ".join(x for x in (_text(listing.get("title")), _text(listing.get("address"))) if x))
    row("Einziehende Personen", persons_line(_text(data.get("adults")), _text(data.get("children"))))
    row("Gewünschter Einzug", _text(data.get("move_in")))
    row("Anschrift", _text(data.get("address")))
    row("Kontakt", _text(data.get("contact")))
    pdf.ln(10)
    font(12, True, ACCENT_RGB)
    pdf.cell(0, 8, "Inhalt", new_x="LMARGIN", new_y="NEXT")
    font(11)
    for item in ("1.  Anschreiben", "2.  Mieterselbstauskunft", "3.  Beizufügende Unterlagen"):
        pdf.cell(0, 8, item, new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(-40)
    font(8.5, False, MUTED_RGB)
    pdf.multi_cell(0, 4.5, f"Erstellt am {_text(data.get('today'))}", new_x="LMARGIN", new_y="NEXT")

    # ---- 2. cover letter ----
    pdf.add_page()
    heading("Anschreiben")
    font(10.5)
    sender = [x for x in (name, _text(data.get("address")), _text(data.get("contact"))) if x]
    if sender:
        pdf.multi_cell(0, 5.5, "\n".join(sender), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    font(10.5, False, MUTED_RGB)
    pdf.cell(0, 6, _text(data.get("today")), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    font(11, True)
    subject = "Bewerbung um die Wohnung"
    if listing and (_text(listing.get("title")) or _text(listing.get("address"))):
        subject += ": " + " · ".join(x for x in (_text(listing.get("title")), _text(listing.get("address"))) if x)
    else:
        subject = "Wohnungsbewerbung"
    pdf.multi_cell(0, 6.5, subject, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    font(10.5)
    letter = _text(data.get("letter"))
    pdf.multi_cell(0, 5.8, letter if letter else "(Anschreiben)", new_x="LMARGIN", new_y="NEXT")

    # ---- 3. Mieterselbstauskunft ----
    pdf.add_page()
    heading("Mieterselbstauskunft")
    font(9.5, False, MUTED_RGB)
    pdf.multi_cell(0, 5, "Angaben des Mietinteressenten. Leere Felder werden handschriftlich ergänzt.",
                   new_x="LMARGIN", new_y="NEXT")
    occupation = _text(data.get("occupation"))
    subheading("1. Angaben zur Person")
    row("Name, Vorname", name)
    row("Geburtsdatum", _text(data.get("birth_date")))
    row("Derzeitige Anschrift", _text(data.get("address")))
    row("Telefon / E-Mail", _text(data.get("contact")))
    subheading("2. Beschäftigung und Einkommen")
    row("Beschäftigung", occupation_label(occupation))
    if occupation == "jobcenter":
        statement("Miete wird vom Jobcenter übernommen.")
    else:
        row("Arbeitgeber / Tätigkeit", _text(data.get("employer")))
        row("Monatliches Nettoeinkommen", _text(data.get("net_income")))
    subheading("3. Einziehende Personen")
    row("Erwachsene", _text(data.get("adults")))
    row("Kinder", _text(data.get("children")))
    subheading("4. Weitere Angaben")
    row("Gewünschter Einzugstermin", _text(data.get("move_in")))
    yes_no("Haustiere", _text(data.get("pets")))
    if _text(data.get("pets")) == "yes":
        row("Art der Tiere", _text(data.get("pet_kind")))
    yes_no("Raucher im Haushalt", _text(data.get("smoking")))
    yes_no("Bestehen Mietrückstände?", _text(data.get("debts")))
    yes_no("Besteht oder bestand ein Insolvenzverfahren?", _text(data.get("insolvency")))
    pdf.ln(4)
    need(50)
    font(9.5, False, MUTED_RGB)
    pdf.multi_cell(0, 5, "Ich versichere, dass meine Angaben vollständig und wahrheitsgemäß sind.",
                   new_x="LMARGIN", new_y="NEXT")
    pdf.ln(14)
    y = pdf.get_y()
    pdf.set_draw_color(120, 120, 120)
    pdf.line(LEFT, y, LEFT + 70, y)
    pdf.line(right_edge - 70, y, right_edge, y)
    pdf.set_xy(LEFT, y + 1)
    pdf.cell(70, 5, "Ort, Datum")
    pdf.set_xy(right_edge - 70, y + 1)
    pdf.cell(70, 5, "Unterschrift")

    # ---- 4. documents ----
    pdf.add_page()
    heading("Beizufügende Unterlagen")
    font(9.5, False, MUTED_RGB)
    pdf.multi_cell(0, 5, "Bitte diese Unterlagen der Bewerbung beilegen.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    docs = data.get("docs") if isinstance(data.get("docs"), list) else documents_list(occupation)
    font(11)
    for item in docs:
        need(14)
        y0 = pdf.get_y()
        pdf.set_xy(LEFT, y0)
        pdf.cell(9, 7, BOX_OFF)
        pdf.set_xy(LEFT + 9, y0)
        pdf.multi_cell(width - 9, 7, _text(item), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2.5)

    return bytes(pdf.output())
