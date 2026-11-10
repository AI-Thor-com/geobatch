"""Tabellen lesen/schreiben und Adressspalten erkennen."""
from __future__ import annotations

import io
import os
import unicodedata

import pandas as pd

ADDRESS_NAMES = ["adresse", "anschrift", "address", "full_address", "vollständige adresse"]
STREET_NAMES = ["straße", "strasse", "str", "street", "strasse_hausnummer", "straße und hausnummer"]
HNR_NAMES = ["hausnummer", "hausnr", "hnr", "nr", "housenumber", "house_number"]
ZIP_NAMES = ["plz", "postleitzahl", "zip", "postcode", "postal_code", "zipcode"]
CITY_NAMES = ["ort", "stadt", "city", "gemeinde", "town", "wohnort"]
COUNTRY_NAMES = ["land", "country", "staat"]


def _norm(c: str) -> str:
    return unicodedata.normalize("NFC", str(c)).strip().lower()


def read_table(src, filename: str | None = None) -> pd.DataFrame:
    """CSV (Trennzeichen/Encoding automatisch) oder Excel. src = Pfad oder Bytes/Dateiobjekt."""
    name = (filename or (src if isinstance(src, str) else "")).lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(src, dtype=str)
    raw = open(src, "rb").read() if isinstance(src, str) else (src if isinstance(src, bytes) else src.read())
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return pd.read_csv(io.StringIO(text), sep=detect_sep(text), engine="python", dtype=str,
                       keep_default_na=False)


def detect_sep(text: str) -> str:
    """Trennzeichen aus der Kopfzeile. Ohne Trennzeichen -> eine Spalte (Kommas bleiben Teil der Adresse)."""
    header = text.lstrip("﻿").splitlines()[0] if text.strip() else ""
    counts = {c: header.count(c) for c in (";", "\t", ",", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else "\x1f"


def write_table(df: pd.DataFrame, path: str) -> None:
    tmp = path + ".tmp"
    if path.lower().endswith(".xlsx"):
        df.to_excel(tmp, index=False, engine="openpyxl")
    else:
        df.to_csv(tmp, index=False, sep=";", encoding="utf-8-sig")
    os.replace(tmp, path)  # atomar: bei Abbruch bleibt die letzte vollständige Datei stehen


def to_bytes(df: pd.DataFrame, fmt: str) -> bytes:
    buf = io.BytesIO()
    if fmt == "xlsx":
        df.to_excel(buf, index=False, engine="openpyxl")
    else:
        buf.write(df.to_csv(index=False, sep=";").encode("utf-8-sig"))
    return buf.getvalue()


def guess_template(columns: list[str]) -> str | None:
    """Schlägt eine Adressvorlage anhand der Spaltennamen vor (oder None)."""
    lookup = {_norm(c): c for c in columns}

    def find(names):
        for n in names:
            if n in lookup:
                return lookup[n]
        return None

    full = find(ADDRESS_NAMES)
    if full:
        return "{%s}" % full
    street, hnr, zipc, city, country = (find(STREET_NAMES), find(HNR_NAMES), find(ZIP_NAMES),
                                        find(CITY_NAMES), find(COUNTRY_NAMES))
    if not (street or zipc or city):
        return None
    parts = []
    if street:
        parts.append("{%s}" % street + (" {%s}" % hnr if hnr else ""))
    town = " ".join("{%s}" % c for c in (zipc, city) if c)
    if town:
        parts.append(town)
    if country:
        parts.append("{%s}" % country)
    return ", ".join(parts)
