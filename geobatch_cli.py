#!/usr/bin/env python3
"""
GeoBatch – Kommandozeile.

Beispiele
  # Einzeladresse
  python geobatch_cli.py --query "Römerberg 27, 60311 Frankfurt am Main"

  # Datei (Spalten werden erkannt), Ergebnis als CSV
  python geobatch_cli.py adressen.csv -o adressen_geo.csv

  # Mit Vorlage, Fallback-Kette und Kontakt (Pflicht für Nominatim)
  python geobatch_cli.py kunden.xlsx -o kunden_geo.xlsx \\
      --template "{Straße} {Hausnr}, {PLZ} {Ort}" \\
      --providers photon,nominatim --contact du@example.com --max-per-hour 1000

API-Keys (optional) über Umgebungsvariablen: LOCATIONIQ_KEY, GEOAPIFY_KEY, OPENCAGE_KEY.
Kontakt auch über GEOBATCH_CONTACT.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from geobatch_core import (ConfigError, Geocoder, Settings, build_address, estimate_hours,
                           normalize_query, template_fields)
from geobatch_io import guess_template, read_table, write_table

GEO_COLS = ["geo_lat", "geo_lon", "geo_address", "geo_provider", "geo_quality",
            "geo_type", "geo_status", "geo_error"]


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Adressen in Koordinaten umwandeln (freie Dienste).")
    ap.add_argument("input", nargs="?", help="CSV- oder Excel-Datei mit Adressen")
    ap.add_argument("-o", "--output", help="Ergebnisdatei (.csv oder .xlsx)")
    ap.add_argument("--query", help="Nur eine einzelne Adresse abfragen")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--address-col", help="Spalte mit der kompletten Adresse")
    g.add_argument("--cols", nargs="+", help="Spalten, die mit Komma verbunden werden")
    g.add_argument("--template", help='Vorlage, z. B. "{Straße} {Nr}, {PLZ} {Ort}"')
    ap.add_argument("--providers", default="photon",
                    help="Fallback-Kette, z. B. photon,nominatim,locationiq (Standard: photon)")
    ap.add_argument("--contact", default=os.getenv("GEOBATCH_CONTACT", ""),
                    help="E-Mail für den User-Agent (Pflicht bei öffentlichem Nominatim)")
    ap.add_argument("--max-per-hour", type=int, default=1000,
                    help="Globale Obergrenze an Anfragen pro Stunde (Standard 1000)")
    ap.add_argument("--country", default="de", help='Länderfilter, z. B. "de" oder "de,at,ch"; "" = alle')
    ap.add_argument("--no-fallback", action="store_true",
                    help="Bei 'nicht gefunden' keinen weiteren Anbieter fragen")
    ap.add_argument("--retry-not-found", action="store_true",
                    help="Im Cache als 'nicht gefunden' markierte Adressen erneut abfragen")
    ap.add_argument("--nominatim-url", help="Eigene Nominatim-Instanz (ohne Drosselung)")
    ap.add_argument("--photon-url", help="Eigene Photon-Instanz (ohne Drosselung)")
    ap.add_argument("--cache", default="geocache.sqlite", help="Cache-Datei (Standard geocache.sqlite)")
    ap.add_argument("--checkpoint", type=int, default=25, help="Zwischenspeichern alle N Zeilen")
    return ap.parse_args(argv)


def make_settings(a) -> Settings:
    base = {}
    if a.nominatim_url:
        base["nominatim"] = a.nominatim_url
    if a.photon_url:
        base["photon"] = a.photon_url
    return Settings(
        providers=[p for p in a.providers.split(",") if p.strip()],
        contact=a.contact, max_per_hour=a.max_per_hour, countrycodes=a.country,
        fallback_on_not_found=not a.no_fallback, retry_not_found=a.retry_not_found,
        keys={"locationiq": os.getenv("LOCATIONIQ_KEY", ""), "geoapify": os.getenv("GEOAPIFY_KEY", ""),
              "opencage": os.getenv("OPENCAGE_KEY", "")},
        base_urls=base, cache_path=a.cache)


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def main(argv=None) -> int:
    a = parse_args(argv)
    try:
        gc = Geocoder(make_settings(a))
    except ConfigError as e:
        print(f"Konfigurationsfehler: {e}", file=sys.stderr)
        return 2

    try:
        if a.query:
            r = gc.geocode(a.query)
            print(f"{r.status}: {r.lat}, {r.lon}  [{r.provider}{', Cache' if r.from_cache else ''}]")
            print(f"  {r.address or r.error}")
            return 0 if r.status == "ok" else 1

        if not a.input:
            print("Bitte eine Eingabedatei oder --query angeben (siehe --help).", file=sys.stderr)
            return 2
        df = read_table(a.input)
        cols = list(df.columns)
        template = a.template or ("{%s}" % a.address_col if a.address_col else None)
        if not template and not a.cols:
            template = guess_template(cols)
            if not template:
                print(f"Keine Adressspalten erkannt. Vorhanden: {cols}\n"
                      "Bitte --address-col, --cols oder --template angeben.", file=sys.stderr)
                return 2
            print(f"Erkannte Adressvorlage: {template}")
        missing = [c for c in (template_fields(template) if template else a.cols) if c not in cols]
        if missing:
            print(f"Spalten nicht gefunden: {missing}. Vorhanden: {cols}", file=sys.stderr)
            return 2

        out = a.output or os.path.splitext(a.input)[0] + "_geo.csv"
        records = df.to_dict("records")
        queries = [build_address(r, a.cols, template) for r in records]
        for c in GEO_COLS:
            df[c] = None

        cc = gc.s.countrycodes.lower()
        uncached = len({normalize_query(q) for q in queries if q
                        and not gc.cache.get(normalize_query(q) + "|" + cc)})
        hours = estimate_hours(uncached, gc.s, gc.providers)
        print(f"{len(queries)} Zeilen, {len(set(queries))} eindeutige Adressen, davon ~{uncached} neu "
              f"-> geschätzt {fmt_dur(hours * 3600)} bei max. {gc.s.max_per_hour}/h")
        public_nom = any(p.name == "nominatim" for p in gc.providers)
        if public_nom and hours > 24:
            print("WARNUNG: Läufe über 24 h sind bei öffentlichem Nominatim auf 4 Anfragen/min begrenzt. "
                  "Bitte Datei aufteilen oder eine eigene Instanz nutzen (--nominatim-url).",
                  file=sys.stderr)

        t0 = time.monotonic()
        for i, res in gc.geocode_many(queries):
            for k, v in res.to_row().items():
                df.at[i, k] = v
            done = i + 1
            if done % a.checkpoint == 0 or done == len(queries):
                write_table(df, out)
            el = time.monotonic() - t0
            eta = el / done * (len(queries) - done)
            src = "Cache" if res.from_cache else res.provider or "-"
            print(f"[{done}/{len(queries)}] {res.status:<9} {src:<11} {queries[i][:60]}"
                  f"   (ETA {fmt_dur(eta)})", flush=True)
        st = gc.stats
        print(f"\nFertig: {st['ok']} gefunden, {st['not_found']} nicht gefunden, {st['error']} Fehler; "
              f"{st['requests']} Netzwerkanfragen, {st['cache']} aus dem Cache.\nErgebnis: {out}")
        print("Daten © OpenStreetMap-Mitwirkende (ODbL) bzw. jeweiliger Anbieter – bitte Quelle nennen.")
        return 0
    except KeyboardInterrupt:
        print("\nAbgebrochen. Bereits gefundene Adressen liegen im Cache – einfach erneut starten.")
        return 130
    finally:
        gc.close()


if __name__ == "__main__":
    sys.exit(main())
