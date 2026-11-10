# GeoBatch – Adressen in Koordinaten umwandeln

Kleines Python-Tool (Weboberfläche + Kommandozeile), das Adresslisten aus CSV- oder Excel-Dateien über **freie Geocoding-Dienste** in Breiten-/Längengrade umwandelt. Ausgelegt auf **~1.000 Adressen pro Stunde**, mit Cache, Drosselung und automatischem Ausweichen auf einen zweiten Anbieter.

## Schnellstart

```bash
pip install -r requirements.txt
streamlit run app.py                 # Weboberfläche im Browser (http://localhost:8501)
```

Unter Windows reicht ein Doppelklick auf `start_app.bat` (legt beim ersten Start eine virtuelle Umgebung an).

Kommandozeile:

```bash
python geobatch_cli.py --query "Römerberg 27, 60311 Frankfurt am Main"
python geobatch_cli.py beispiel_adressen.csv -o ergebnis.csv
python geobatch_cli.py kunden.xlsx -o kunden_geo.xlsx \
    --template "{Straße} {Hausnr}, {PLZ} {Ort}" \
    --providers photon,nominatim --contact du@example.com
```

## Unterstützte Anbieter

| Anbieter | Key nötig | Kostenloses Limit | Eingestellte Drosselung |
|---|---|---|---|
| **Photon** (komoot, OSM-Daten) – *Standard* | nein | Fair Use („übermäßige Nutzung wird gedrosselt“) | max. 1/s |
| **Nominatim** (OpenStreetMap Foundation) | nein, aber Kontaktangabe | max. 1/s, nur einmalige Massenläufe | max. 1/s |
| **LocationIQ** | ja (kostenlos) | 5.000/Tag, 2/s | 2/s, Tageszähler |
| **Geoapify** | ja (kostenlos) | 3.000/Tag, 5/s | 5/s, Tageszähler |
| **OpenCage** | ja (kostenlos, Test) | 2.500/Tag, 1/s | 1/s, Tageszähler |
| Eigene Nominatim-/Photon-Instanz | nein | unbegrenzt | nur globales Limit |

Die Anbieter werden als **Fallback-Kette** genutzt: Findet der erste nichts (oder ist gesperrt/Kontingent erschöpft), fragt GeoBatch den nächsten. Empfehlung: `photon,nominatim` ohne Keys oder `photon,locationiq` mit Key. Stand der Limits: November 2026 – bitte vor größeren Läufen auf den Anbieterseiten prüfen.

API-Keys als Umgebungsvariablen `LOCATIONIQ_KEY`, `GEOAPIFY_KEY`, `OPENCAGE_KEY` setzen oder in der Oberfläche eintragen. Die Kontakt-E-Mail geht über `--contact` oder `GEOBATCH_CONTACT`.

## ⚠️ Wichtig: Nutzungsbedingungen von Nominatim

Die öffentliche Nominatim-Instanz läuft auf gespendeten Servern. Die [Nominatim Usage Policy](https://operations.osmfoundation.org/policies/nominatim/) verlangt:

- höchstens **1 Anfrage pro Sekunde**, **ein Thread**, **eine Maschine**,
- eine **eindeutige Kennung** der Anwendung (GeoBatch erzwingt dafür `--contact`),
- **Caching** der Ergebnisse (macht GeoBatch automatisch),
- Massen-Geocoding nur als **einmalige** Aufgabe. Läufe über 24 Stunden oder regelmäßig wiederkehrende Läufe sind auf **4 Anfragen pro Minute** begrenzt,
- Quellenangabe „© OpenStreetMap-Mitwirkende“ (ODbL).

**1.000 Adressen pro Stunde als einmaliger Lauf passen in diese Regeln.** Wer das regelmäßig (z. B. täglich) machen will, nimmt einen Anbieter mit Key oder betreibt eine eigene Instanz (siehe unten). Wer gegen die Regeln verstößt, wird automatisch per IP gesperrt.

## So erreicht GeoBatch 1.000 Adressen pro Stunde

- **Globale Obergrenze** `--max-per-hour` (Standard 1000 → eine Anfrage alle 3,6 s). Dazu kommt der Mindestabstand je Anbieter.
- **Cache** (`geocache.sqlite`): Jede Adresse wird nur einmal abgefragt, Duplikate in der Datei und Wiederholungsläufe kosten nichts. Auch „nicht gefunden“ wird gespeichert, damit fehlerhafte Adressen nicht wiederholt angefragt werden (mit `--retry-not-found` erneut versuchen).
- **Fortsetzbar:** Bricht ein Lauf ab (Strg+C, Netzwerk, Browser zu), einfach neu starten – Bekanntes kommt sofort aus dem Cache. Die CLI speichert außerdem alle 25 Zeilen zwischen.
- **Fehlerbehandlung:** Bei 429/403 wird der Anbieter 15 Minuten pausiert und der nächste gefragt. Timeouts und 5xx-Fehler werden bis zu dreimal mit wachsender Wartezeit wiederholt. Fehler werden nicht gecacht.

Mehr als 1.000/h geht ohne Key bis 3.600/h (das Maximum bei 1/s). Mit eigener Instanz gibt es keine Obergrenze.

## Datenschutz

Die Adressen werden zur Umwandlung an den gewählten externen Dienst geschickt (z. B. komoot, OpenStreetMap Foundation, LocationIQ). Enthält die Liste personenbezogene Daten, etwa Kundenadressen, gilt die DSGVO. Prüfe dann vorher, ob die Übermittlung an diesen Anbieter zulässig ist (Rechtsgrundlage, ggf. Auftragsverarbeitung, Serverstandort). Oder nutze eine eigene Instanz (siehe unten), dann verlassen die Daten deine Infrastruktur nicht.

GeoBatch selbst speichert die Ergebnisse nur lokal im Cache (`geocache.sqlite`). Eigene Daten- und Cache-Dateien gehören nicht in ein öffentliches Repository.

## Eingabe

- CSV (Trennzeichen `;` `,` Tab `|` und Encoding UTF-8/Windows-1252 werden erkannt) oder Excel (.xlsx).
- Adressspalten werden erkannt (`Adresse` oder `Straße`/`Hausnummer`/`PLZ`/`Ort`/`Land`), sonst per Vorlage `{Spalte}` angeben.
- Länderfilter Standard `de`; mehrere mit `de,at,ch`, leer = weltweit.

## Ausgabe

Alle Originalspalten plus:

| Spalte | Inhalt |
|---|---|
| `geo_lat`, `geo_lon` | Koordinaten (WGS84) |
| `geo_address` | vom Dienst erkannte Adresse – zum Gegenprüfen |
| `geo_provider` | welcher Dienst geantwortet hat |
| `geo_type` | Trefferart, z. B. `house`, `street`, `city` – **`city` bedeutet nur Ortsmitte!** |
| `geo_quality` | Güte 0–1, soweit der Anbieter etwas liefert |
| `geo_status` | `ok`, `not_found` oder `error` |
| `geo_error` | Fehlermeldung |

Tipp: Nach dem Lauf nach `geo_type` filtern. Treffer vom Typ `city` oder `street` sind ungenau und sollten geprüft werden.

## Online bereitstellen (Streamlit Community Cloud, kostenlos)

1. Dieses Repository forken oder den Inhalt in ein eigenes GitHub-Repo hochladen
   (GitHub → *Add file* → *Upload files*, oder `git push`).
2. Auf [share.streamlit.io](https://share.streamlit.io) mit GitHub anmelden → **Create app** →
   Repo `geobatch`, Branch `main`, Datei `app.py` → **Deploy**.
3. In der App unter **Settings → Secrets** eintragen (Vorlage: `.streamlit/secrets.example.toml`):
   ```toml
   GEOBATCH_CONTACT = "deine@email.de"
   LOCATIONIQ_KEY = "..."   # optional
   ```
   Keys aus den Secrets werden **nicht** im Browser angezeigt.
4. Unter **Settings → Sharing** festlegen, wer die App sehen darf. Empfehlung: nur eingeladene Personen. Eine für alle offene App teilt deine Anbieterkontingente mit Fremden.

Hinweise für den Betrieb online:
- Die Anbieterlimits (z. B. 1/s) gelten **für alle gleichzeitigen Nutzer zusammen** – die App drosselt prozessweit.
- Der Cache (`geocache.sqlite`) geht beim Neustart der App verloren. Ergebnisse also immer herunterladen.
- Streamlit Cloud teilt sich IP-Adressen mit vielen anderen Apps. Nominatim kann solche IPs eher sperren,
  Photon ist dort der robustere Standard. Schlüssel-Anbieter (LocationIQ, Geoapify) sind online am zuverlässigsten.
- Nicht als öffentlichen Geocoding-Dienst für Dritte anbieten: Das schließt die Nominatim-Richtlinie aus.
- Die App schläft nach einigen Tagen ohne Nutzung ein und wacht beim nächsten Aufruf in ca. 1 Minute wieder auf.

## Für regelmäßige oder große Mengen: eigene Instanz

Photon läuft mit einem vorgebauten Deutschland-Index lokal (Java, einige GB Platz) und hat dann keine Grenzen:
siehe [github.com/komoot/photon](https://github.com/komoot/photon). Danach:

```bash
python geobatch_cli.py adressen.csv --photon-url http://localhost:2322 --max-per-hour 36000
```

Analog für Nominatim mit `--nominatim-url` (z. B. per Docker-Image `mediagis/nominatim`).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests      # 17 Tests, ohne Netzwerk (gemockte Anbieter)
```

## Dateien

| Datei | Zweck |
|---|---|
| `app.py` | Streamlit-Oberfläche (Einzeladresse, Datei-Upload, Karte, Download CSV/Excel) |
| `geobatch_cli.py` | Kommandozeile |
| `geobatch_core.py` | Anbieter, Drosselung, Cache, Fallback-Kette |
| `geobatch_io.py` | CSV/Excel lesen und schreiben, Spaltenerkennung |
| `tests/` | Tests (laufen bei jedem Push automatisch über GitHub Actions) |
| `.github/workflows/tests.yml` | GitHub-Actions-Workflow für die Tests |
| `beispiel_adressen.csv` | Beispieldatei |

Daten © OpenStreetMap-Mitwirkende, lizenziert unter ODbL – bei Weitergabe der Ergebnisse bitte Quelle nennen.
