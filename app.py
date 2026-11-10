"""
GeoBatch – Weboberfläche (Streamlit).
Start:  streamlit run app.py
"""
from __future__ import annotations

import os
import time

import pandas as pd
import streamlit as st

from geobatch_core import (PROVIDER_INFO, ConfigError, Geocoder, Settings, build_address,
                           estimate_hours, normalize_query, template_fields)
from geobatch_io import guess_template, read_table, to_bytes

GEO_COLS = ["geo_lat", "geo_lon", "geo_address", "geo_provider", "geo_quality",
            "geo_type", "geo_status", "geo_error"]

def cfg(name: str, default: str = "") -> str:
    """Wert aus Umgebungsvariable oder Streamlit-Secrets (Streamlit Cloud)."""
    v = os.getenv(name)
    if v:
        return v
    try:
        return str(st.secrets.get(name, default))
    except Exception:  # keine secrets.toml vorhanden
        return default


st.set_page_config(page_title="GeoBatch – Adressen geocodieren", page_icon="📍", layout="wide")
st.title("📍 GeoBatch – Adressen in Koordinaten umwandeln")
st.caption("Freie Dienste auf OpenStreetMap-Basis · Cache · Drosselung · Fallback-Kette")

# ------------------------------------------------------------------ Sidebar
with st.sidebar:
    st.header("Einstellungen")
    providers = st.multiselect(
        "Anbieter (Reihenfolge = Fallback-Kette)", list(PROVIDER_INFO),
        default=["photon"], format_func=lambda k: PROVIDER_INFO[k])
    contact = st.text_input("Kontakt-E-Mail (für User-Agent)", "",
                            placeholder="aus Secrets geladen" if cfg("GEOBATCH_CONTACT") else "",
                            help="Pflicht bei öffentlichem Nominatim, für alle Anbieter empfohlen. "
                                 "Ein Wert aus den Secrets wird nicht angezeigt.") or cfg("GEOBATCH_CONTACT")
    max_per_hour = st.slider("Max. Anfragen pro Stunde", 100, 3600, 1000, 100,
                             help="Gilt für alle Netzwerkanfragen zusammen. Cache-Treffer zählen nicht.")
    country = st.text_input("Länderfilter (ISO, kommagetrennt, leer = alle)", "de")
    fallback = st.checkbox("Bei 'nicht gefunden' nächsten Anbieter fragen", True)
    retry_nf = st.checkbox("Gecachte 'nicht gefunden' erneut versuchen", False)
    with st.expander("API-Keys (optional)"):
        k_liq = st.text_input("LocationIQ", "", type="password",
                              placeholder="aus Secrets geladen" if cfg("LOCATIONIQ_KEY") else "") or cfg("LOCATIONIQ_KEY")
        k_geo = st.text_input("Geoapify", "", type="password",
                              placeholder="aus Secrets geladen" if cfg("GEOAPIFY_KEY") else "") or cfg("GEOAPIFY_KEY")
        k_oc = st.text_input("OpenCage", "", type="password",
                              placeholder="aus Secrets geladen" if cfg("OPENCAGE_KEY") else "") or cfg("OPENCAGE_KEY")
    with st.expander("Eigene Instanzen (ohne Drosselung)"):
        u_nom = st.text_input("Nominatim-URL", "", placeholder="http://localhost:8080")
        u_pho = st.text_input("Photon-URL", "", placeholder="http://localhost:2322")
    st.markdown("---")
    st.markdown("**Nutzungsbedingungen**  \n"
                "[Nominatim-Richtlinie](https://operations.osmfoundation.org/policies/nominatim/) · "
                "[Photon](https://photon.komoot.io)  \n"
                "Daten © [OpenStreetMap](https://www.openstreetmap.org/copyright)-Mitwirkende (ODbL)")

if "nominatim" in providers and not u_nom:
    st.warning("**Öffentliches Nominatim:** max. 1 Anfrage/s, nur ein Thread, Ergebnisse cachen, "
               "nur *einmalige* Massenläufe – regelmäßige oder >24-h-Läufe sind auf 4/min begrenzt. "
               "Für Daueranwendungen eigene Instanz oder Schlüssel-Anbieter nutzen. "
               "[Richtlinie lesen](https://operations.osmfoundation.org/policies/nominatim/)")


def make_geocoder() -> Geocoder | None:
    base = {k: v for k, v in (("nominatim", u_nom), ("photon", u_pho)) if v}
    s = Settings(providers=providers, contact=contact, max_per_hour=max_per_hour,
                 countrycodes=country.strip(), fallback_on_not_found=fallback,
                 retry_not_found=retry_nf, base_urls=base, shared_throttle=True,
                 keys={"locationiq": k_liq, "geoapify": k_geo, "opencage": k_oc})
    try:
        return Geocoder(s)
    except ConfigError as e:
        st.error(str(e))
        return None


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    return f"{h} h {rem // 60:02d} min" if h else f"{rem // 60} min {rem % 60:02d} s"


tab_one, tab_file = st.tabs(["🔎 Einzeladresse", "📄 Datei (CSV/Excel)"])

# ------------------------------------------------------------------ Einzeladresse
with tab_one:
    q = st.text_input("Adresse", "Römerberg 27, 60311 Frankfurt am Main")
    if st.button("Suchen", type="primary") and q.strip():
        gc = make_geocoder()
        if gc:
            r = gc.geocode(q)
            gc.close()
            if r.status == "ok":
                st.success(f"{r.lat:.6f}, {r.lon:.6f} · {r.provider}"
                           f"{' (Cache)' if r.from_cache else ''}")
                st.write(r.address)
                st.map(pd.DataFrame({"lat": [r.lat], "lon": [r.lon]}), zoom=14)
            elif r.status == "not_found":
                st.warning("Nicht gefunden." + (f" ({r.error})" if r.error else ""))
            else:
                st.error(r.error)

# ------------------------------------------------------------------ Datei
with tab_file:
    up = st.file_uploader("CSV oder Excel hochladen", type=["csv", "txt", "xlsx", "xls"])
    if up:
        df = read_table(up.getvalue(), up.name)
        st.write(f"{len(df)} Zeilen · Spalten: {', '.join(map(str, df.columns))}")
        st.dataframe(df.head(5), width="stretch")

        tpl_default = guess_template(list(df.columns)) or "{" + str(df.columns[0]) + "}"
        template = st.text_input("Adressvorlage (Spaltennamen in {…})", tpl_default,
                                 help='z. B. "{Straße} {Hausnummer}, {PLZ} {Ort}"')
        missing = [c for c in template_fields(template) if c not in df.columns]
        if missing:
            st.error(f"Spalten nicht gefunden: {missing}")
            st.stop()

        records = df.to_dict("records")
        queries = [build_address(r, template=template) for r in records]
        st.caption("Vorschau: " + " | ".join(queries[:3]))

        gc = make_geocoder()
        if not gc:
            st.stop()
        cc = gc.s.countrycodes.lower()
        uniq = {normalize_query(x) for x in queries if x}
        new = sum(1 for x in uniq if not gc.cache.get(x + "|" + cc))
        est = estimate_hours(new, gc.s, gc.providers) * 3600
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Zeilen", len(queries))
        c2.metric("Eindeutige Adressen", len(uniq))
        c3.metric("Neu abzufragen", new)
        c4.metric("Geschätzte Dauer", fmt_dur(est))

        if st.button("▶ Geocodierung starten", type="primary"):
            res_df = df.copy()
            for c in GEO_COLS:
                res_df[c] = None
            bar, info = st.progress(0.0), st.empty()
            t0 = time.monotonic()
            for i, r in gc.geocode_many(queries):
                for k, v in r.to_row().items():
                    res_df.at[i, k] = v
                done = i + 1
                el = time.monotonic() - t0
                bar.progress(done / len(queries))
                info.write(f"{done}/{len(queries)} · {gc.stats['ok']} gefunden · "
                           f"{gc.stats['not_found']} nicht gefunden · {gc.stats['error']} Fehler · "
                           f"Rest ca. {fmt_dur(el / done * (len(queries) - done))}")
                st.session_state["result"] = res_df  # auch Teilergebnis bleibt erhalten
            st.session_state["result_name"] = os.path.splitext(up.name)[0] + "_geo"
            st.success("Fertig. Abgebrochene Läufe einfach neu starten – Bekanntes kommt aus dem Cache.")
        gc.close()

    if "result" in st.session_state:
        res = st.session_state["result"]
        name = st.session_state.get("result_name", "ergebnis_geo")
        st.subheader("Ergebnis")
        st.dataframe(res, width="stretch")
        pts = res[res["geo_status"] == "ok"][["geo_lat", "geo_lon"]].astype(float)
        if len(pts):
            st.map(pts.rename(columns={"geo_lat": "lat", "geo_lon": "lon"}))
        d1, d2 = st.columns(2)
        d1.download_button("⬇ CSV", to_bytes(res, "csv"), f"{name}.csv", "text/csv")
        d2.download_button("⬇ Excel", to_bytes(res, "xlsx"), f"{name}.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
