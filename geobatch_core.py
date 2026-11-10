"""
GeoBatch – Kern: Adressen -> Koordinaten über freie Geocoding-Dienste.

Eigenschaften
- Mehrere Anbieter in einer Fallback-Kette (Photon, Nominatim, LocationIQ,
  Geoapify, OpenCage, plus selbst gehostete Nominatim/Photon-Instanzen).
- Strikte Drosselung: globales Limit pro Stunde + Mindestabstand je Anbieter.
- Tageskontingente je Anbieter (werden in SQLite mitgezählt).
- SQLite-Cache: jede Adresse wird nur einmal abgefragt (auch "nicht gefunden"),
  dadurch sind abgebrochene Läufe ohne Mehrkosten fortsetzbar.
- Ein Thread, eine Maschine – so verlangt es die Nominatim-Nutzungsrichtlinie.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, asdict, field
from datetime import date
from typing import Callable, Iterable, Iterator, Optional

import requests

APP_NAME = "GeoBatch"
APP_VERSION = "1.0"

NOMINATIM_PUBLIC = "https://nominatim.openstreetmap.org"
PHOTON_PUBLIC = "https://photon.komoot.io"


class ConfigError(ValueError):
    """Fehlende oder unzulässige Konfiguration (z. B. API-Key, Kontakt)."""


class ProviderBlocked(RuntimeError):
    """Anbieter hat gedrosselt/gesperrt (429/403) oder Kontingent erschöpft."""


# --------------------------------------------------------------------------- #
# Ergebnis
# --------------------------------------------------------------------------- #
@dataclass
class GeoResult:
    query: str
    status: str = "not_found"          # ok | not_found | error
    lat: Optional[float] = None
    lon: Optional[float] = None
    address: str = ""
    provider: str = ""
    quality: Optional[float] = None    # 0..1, soweit der Anbieter etwas liefert
    match_type: str = ""               # z. B. house, street, city
    error: str = ""
    from_cache: bool = False

    def to_row(self) -> dict:
        return {
            "geo_lat": round(self.lat, 7) if self.lat is not None else None,
            "geo_lon": round(self.lon, 7) if self.lon is not None else None,
            "geo_address": self.address,
            "geo_provider": self.provider,
            "geo_quality": self.quality,
            "geo_type": self.match_type,
            "geo_status": self.status,
            "geo_error": self.error,
        }


def normalize_query(q: str) -> str:
    """Schlüssel für den Cache: Kleinschreibung, Unicode-NFC, Leerraum vereinheitlicht."""
    q = unicodedata.normalize("NFC", str(q or "")).strip().lower()
    q = re.sub(r"\s+", " ", q)
    q = re.sub(r"\s*,\s*", ", ", q)
    return q.strip(", ")


# --------------------------------------------------------------------------- #
# Drosselung
# --------------------------------------------------------------------------- #
class Throttle:
    """Mindestabstand zwischen Netzwerkanfragen (monotone Uhr)."""

    def __init__(self, min_interval: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.min_interval = max(0.0, float(min_interval))
        self._clock, self._sleep = clock, sleep
        self._last: Optional[float] = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        # Lock: mehrere gleichzeitige Nutzer (z. B. gehostete Web-App) teilen sich das Limit
        with self._lock:
            if self._last is not None and self.min_interval > 0:
                remaining = self.min_interval - (self._clock() - self._last)
                if remaining > 0:
                    self._sleep(remaining)
            self._last = self._clock()


#: Prozessweit geteilte Drosseln je Anbieter (für gehostete Mehrbenutzer-Apps)
_SHARED: dict[str, Throttle] = {}
_SHARED_LOCK = threading.Lock()


def shared_throttle(name: str, min_interval: float) -> Throttle:
    with _SHARED_LOCK:
        t = _SHARED.get(name)
        if t is None:
            t = _SHARED[name] = Throttle(min_interval)
        else:
            t.min_interval = max(t.min_interval, min_interval)
        return t


# --------------------------------------------------------------------------- #
# Anbieter
# --------------------------------------------------------------------------- #
class Provider:
    name = "base"
    #: Mindestabstand in Sekunden laut Anbieterbedingungen (öffentliche Instanz)
    min_interval = 1.0
    #: Tageskontingent (None = keins bekannt)
    daily_quota: Optional[int] = None
    requires_key = False

    def __init__(self, session: requests.Session, user_agent: str, timeout: float = 20.0):
        self.session, self.user_agent, self.timeout = session, user_agent, timeout

    # Unterklassen: Anfrage bauen + Antwort parsen
    def request(self, query: str, countrycodes: str) -> tuple[str, dict]:
        raise NotImplementedError

    def parse(self, query: str, data, countrycodes: str = "") -> GeoResult:
        raise NotImplementedError

    def geocode(self, query: str, countrycodes: str = "") -> GeoResult:
        url, params = self.request(query, countrycodes)
        resp = self.session.get(url, params=params, timeout=self.timeout,
                                headers={"User-Agent": self.user_agent,
                                         "Accept-Language": "de"})
        if resp.status_code in (401, 403, 429, 402):
            raise ProviderBlocked(f"{self.name}: HTTP {resp.status_code}")
        resp.raise_for_status()
        return self.parse(query, resp.json(), countrycodes)


class NominatimProvider(Provider):
    name = "nominatim"
    min_interval = 1.0          # absolutes Maximum laut OSMF-Richtlinie

    def __init__(self, session, user_agent, base_url: str = NOMINATIM_PUBLIC, **kw):
        super().__init__(session, user_agent, **kw)
        self.base_url = base_url.rstrip("/")
        if self.base_url != NOMINATIM_PUBLIC:
            self.name = "nominatim-self"
            self.min_interval = 0.0

    def request(self, query, countrycodes):
        p = {"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 0}
        if countrycodes:
            p["countrycodes"] = countrycodes
        return f"{self.base_url}/search", p

    def parse(self, query, data, countrycodes=""):
        if not data:
            return GeoResult(query, provider=self.name)
        r = data[0]
        imp = r.get("importance")
        return GeoResult(query, "ok", float(r["lat"]), float(r["lon"]),
                         r.get("display_name", ""), self.name,
                         round(float(imp), 3) if imp is not None else None,
                         r.get("addresstype") or r.get("type", ""))


class PhotonProvider(Provider):
    name = "photon"
    min_interval = 1.0          # "fair use" – wir bleiben bewusst bei max. 1/s

    def __init__(self, session, user_agent, base_url: str = PHOTON_PUBLIC, **kw):
        super().__init__(session, user_agent, **kw)
        self.base_url = base_url.rstrip("/")
        if self.base_url != PHOTON_PUBLIC:
            self.name = "photon-self"
            self.min_interval = 0.0

    def request(self, query, countrycodes):
        return f"{self.base_url}/api", {"q": query, "limit": 5, "lang": "de"}

    def parse(self, query, data, countrycodes=""):
        feats = (data or {}).get("features") or []
        wanted = {c.strip().upper() for c in countrycodes.split(",") if c.strip()}
        if wanted:  # Photon hat keinen Länderfilter in allen Versionen -> clientseitig filtern
            feats = [f for f in feats if (f.get("properties", {}).get("countrycode") or "").upper() in wanted]
        if not feats:
            return GeoResult(query, provider=self.name)
        f = feats[0]
        lon, lat = f["geometry"]["coordinates"][:2]
        p = f.get("properties", {})
        street = " ".join(x for x in [p.get("street") or p.get("name"), p.get("housenumber")] if x)
        town = " ".join(x for x in [p.get("postcode"), p.get("city") or p.get("district")] if x)
        addr = ", ".join(x for x in [street, town, p.get("country")] if x)
        return GeoResult(query, "ok", float(lat), float(lon), addr, self.name, None,
                         p.get("type", ""))

class LocationIQProvider(Provider):
    name = "locationiq"
    min_interval = 0.5          # Free: 2 Anfragen/s, 60/min
    daily_quota = 5000
    requires_key = True

    def __init__(self, session, user_agent, api_key: str, **kw):
        super().__init__(session, user_agent, **kw)
        if not api_key:
            raise ConfigError("LocationIQ braucht einen API-Key (LOCATIONIQ_KEY).")
        self.api_key = api_key

    def request(self, query, countrycodes):
        p = {"key": self.api_key, "q": query, "format": "json", "limit": 1}
        if countrycodes:
            p["countrycodes"] = countrycodes
        return "https://eu1.locationiq.com/v1/search", p

    def parse(self, query, data, countrycodes=""):
        if isinstance(data, dict) and data.get("error"):
            return GeoResult(query, provider=self.name)
        if not data:
            return GeoResult(query, provider=self.name)
        r = data[0]
        imp = r.get("importance")
        return GeoResult(query, "ok", float(r["lat"]), float(r["lon"]),
                         r.get("display_name", ""), self.name,
                         round(float(imp), 3) if imp is not None else None, r.get("type", ""))

    def geocode(self, query, countrycodes=""):
        try:
            return super().geocode(query, countrycodes)
        except requests.HTTPError as e:  # LocationIQ meldet "nicht gefunden" als 404
            if e.response is not None and e.response.status_code == 404:
                return GeoResult(query, provider=self.name)
            raise


class GeoapifyProvider(Provider):
    name = "geoapify"
    min_interval = 0.2          # Free: bis 5 Anfragen/s
    daily_quota = 3000
    requires_key = True

    def __init__(self, session, user_agent, api_key: str, **kw):
        super().__init__(session, user_agent, **kw)
        if not api_key:
            raise ConfigError("Geoapify braucht einen API-Key (GEOAPIFY_KEY).")
        self.api_key = api_key

    def request(self, query, countrycodes):
        p = {"text": query, "apiKey": self.api_key, "format": "json", "limit": 1, "lang": "de"}
        if countrycodes:
            p["filter"] = "countrycode:" + countrycodes.lower()
        return "https://api.geoapify.com/v1/geocode/search", p

    def parse(self, query, data, countrycodes=""):
        res = (data or {}).get("results") or []
        if not res:
            return GeoResult(query, provider=self.name)
        r = res[0]
        conf = (r.get("rank") or {}).get("confidence")
        return GeoResult(query, "ok", float(r["lat"]), float(r["lon"]), r.get("formatted", ""),
                         self.name, round(float(conf), 3) if conf is not None else None,
                         r.get("result_type", ""))


class OpenCageProvider(Provider):
    name = "opencage"
    min_interval = 1.0          # Free (Test): 1 Anfrage/s
    daily_quota = 2500
    requires_key = True

    def __init__(self, session, user_agent, api_key: str, **kw):
        super().__init__(session, user_agent, **kw)
        if not api_key:
            raise ConfigError("OpenCage braucht einen API-Key (OPENCAGE_KEY).")
        self.api_key = api_key

    def request(self, query, countrycodes):
        p = {"q": query, "key": self.api_key, "limit": 1, "no_annotations": 1, "language": "de"}
        if countrycodes:
            p["countrycode"] = countrycodes.lower()
        return "https://api.opencagedata.com/geocode/v1/json", p

    def parse(self, query, data, countrycodes=""):
        res = (data or {}).get("results") or []
        if not res:
            return GeoResult(query, provider=self.name)
        r = res[0]
        conf = r.get("confidence")   # 1..10
        return GeoResult(query, "ok", float(r["geometry"]["lat"]), float(r["geometry"]["lng"]),
                         r.get("formatted", ""), self.name,
                         round(conf / 10, 2) if conf is not None else None,
                         (r.get("components") or {}).get("_type", ""))


PROVIDER_INFO = {
    "photon":     "Photon (komoot) – ohne Key, Fair Use, max. 1/s",
    "nominatim":  "Nominatim (OSMF) – ohne Key, max. 1/s, nur einmalige Läufe, Kontakt nötig",
    "locationiq": "LocationIQ – Free-Key, 5.000/Tag, 2/s",
    "geoapify":   "Geoapify – Free-Key, 3.000/Tag, 5/s",
    "opencage":   "OpenCage – Free-Key (Test), 2.500/Tag, 1/s",
}


# --------------------------------------------------------------------------- #
# Cache + Nutzungszähler
# --------------------------------------------------------------------------- #
class Cache:
    def __init__(self, path: str = "geocache.sqlite"):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("""CREATE TABLE IF NOT EXISTS cache(
            key TEXT PRIMARY KEY, result TEXT NOT NULL, ts REAL NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS usage(
            provider TEXT, day TEXT, n INTEGER, PRIMARY KEY(provider, day))""")
        self.db.commit()

    def get(self, key: str) -> Optional[GeoResult]:
        row = self.db.execute("SELECT result FROM cache WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        r = GeoResult(**json.loads(row[0]))
        r.from_cache = True
        return r

    def put(self, key: str, res: GeoResult) -> None:
        d = asdict(res)
        d["from_cache"] = False
        self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                        (key, json.dumps(d, ensure_ascii=False), time.time()))
        self.db.commit()

    def used_today(self, provider: str) -> int:
        row = self.db.execute("SELECT n FROM usage WHERE provider=? AND day=?",
                              (provider, date.today().isoformat())).fetchone()
        return row[0] if row else 0

    def count(self, provider: str) -> None:
        self.db.execute("""INSERT INTO usage VALUES(?,?,1)
            ON CONFLICT(provider, day) DO UPDATE SET n=n+1""",
                        (provider, date.today().isoformat()))
        self.db.commit()

    def close(self):
        self.db.close()


# --------------------------------------------------------------------------- #
# Orchestrierung
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    providers: list[str] = field(default_factory=lambda: ["photon"])
    contact: str = ""                  # E-Mail o. ä. für den User-Agent (Pflicht bei Nominatim)
    max_per_hour: int = 1000           # globales Limit über alle Netzwerkanfragen
    countrycodes: str = "de"           # z. B. "de" oder "de,at,ch"; leer = weltweit
    fallback_on_not_found: bool = True # nächsten Anbieter probieren, wenn nichts gefunden
    retry_not_found: bool = False      # gecachte "nicht gefunden" erneut versuchen
    keys: dict = field(default_factory=dict)       # {"locationiq": "...", ...}
    base_urls: dict = field(default_factory=dict)  # {"nominatim": "http://localhost:8080"}
    max_retries: int = 3
    cache_path: str = "geocache.sqlite"
    shared_throttle: bool = False      # True in Web-Apps: Anbieterlimits gelten über alle Sitzungen


def build_providers(s: Settings, session: requests.Session) -> list[Provider]:
    ua = f"{APP_NAME}/{APP_VERSION}" + (f" ({s.contact})" if s.contact else "")
    out: list[Provider] = []
    for name in s.providers:
        name = name.strip().lower()
        if name == "nominatim":
            base = s.base_urls.get("nominatim", NOMINATIM_PUBLIC)
            if base.rstrip("/") == NOMINATIM_PUBLIC and not s.contact:
                raise ConfigError("Für die öffentliche Nominatim-Instanz ist eine Kontaktangabe "
                                  "(E-Mail) für den User-Agent Pflicht – siehe "
                                  "https://operations.osmfoundation.org/policies/nominatim/")
            out.append(NominatimProvider(session, ua, base))
        elif name == "photon":
            out.append(PhotonProvider(session, ua, s.base_urls.get("photon", PHOTON_PUBLIC)))
        elif name == "locationiq":
            out.append(LocationIQProvider(session, ua, s.keys.get("locationiq", "")))
        elif name == "geoapify":
            out.append(GeoapifyProvider(session, ua, s.keys.get("geoapify", "")))
        elif name == "opencage":
            out.append(OpenCageProvider(session, ua, s.keys.get("opencage", "")))
        else:
            raise ConfigError(f"Unbekannter Anbieter: {name}")
    if not out:
        raise ConfigError("Mindestens ein Anbieter muss gewählt sein.")
    return out


class Geocoder:
    def __init__(self, settings: Settings, session: Optional[requests.Session] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if settings.max_per_hour <= 0:
            raise ConfigError("max_per_hour muss > 0 sein.")
        self.s = settings
        self.session = session or requests.Session()
        self.providers = build_providers(settings, self.session)
        self.cache = Cache(settings.cache_path)
        self._sleep = sleep
        self.global_throttle = Throttle(3600.0 / settings.max_per_hour, clock, sleep)
        if settings.shared_throttle:
            # Anbieterlimit prozessweit; das Stundenlimit bleibt je Sitzung
            self.throttles = {p.name: shared_throttle(p.name, p.min_interval) for p in self.providers}
        else:
            self.throttles = {p.name: Throttle(p.min_interval, clock, sleep) for p in self.providers}
        self.blocked_until: dict[str, float] = {}
        self._clock = clock
        self.stats = {"cache": 0, "requests": 0, "ok": 0, "not_found": 0, "error": 0}

    # -- eine Adresse -------------------------------------------------------
    def geocode(self, query: str) -> GeoResult:
        query = str(query or "").strip()
        if not query:
            return GeoResult(query, "error", error="leere Adresse")
        key = normalize_query(query) + "|" + self.s.countrycodes.lower()
        cached = self.cache.get(key)
        if cached and (cached.status == "ok" or (cached.status == "not_found" and not self.s.retry_not_found)):
            cached.query = query
            self.stats["cache"] += 1
            self.stats[cached.status] += 1
            return cached

        result: Optional[GeoResult] = None
        errors: list[str] = []
        for p in self.providers:
            if self._clock() < self.blocked_until.get(p.name, 0):
                errors.append(f"{p.name}: pausiert")
                continue
            if p.daily_quota and self.cache.used_today(p.name) >= p.daily_quota:
                errors.append(f"{p.name}: Tageskontingent erschöpft")
                continue
            r = self._call(p, query, errors)
            if r is None:
                continue
            result = r
            if r.status == "ok" or not self.s.fallback_on_not_found:
                break

        if result is None:
            # Kein Anbieter hat verwertbar geantwortet -> Fehler, nicht cachen (später erneut)
            result = GeoResult(query, "error", error="; ".join(errors) or "kein Anbieter verfügbar")
        # "nicht gefunden" nur cachen, wenn alle Anbieter sauber geantwortet haben
        if result.status == "ok" or (result.status == "not_found" and not errors):
            self.cache.put(key, result)
        elif result.status == "not_found":
            result.error = "; ".join(errors)
        self.stats[result.status] += 1
        return result

    def _call(self, p: Provider, query: str, errors: list) -> Optional[GeoResult]:
        delay = 2.0
        for attempt in range(1, self.s.max_retries + 1):
            self.global_throttle.wait()
            self.throttles[p.name].wait()
            self.stats["requests"] += 1
            self.cache.count(p.name)
            try:
                return p.geocode(query, self.s.countrycodes)
            except ProviderBlocked as e:
                # Bei Sperre/Drosselung: Anbieter 15 Minuten pausieren, nicht weiter hämmern
                self.blocked_until[p.name] = self._clock() + 900
                errors.append(str(e))
                return None
            except (requests.Timeout, requests.ConnectionError) as e:
                errors.append(f"{p.name}: {type(e).__name__}")
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else "?"
                errors.append(f"{p.name}: HTTP {code}")
                if isinstance(code, int) and code < 500:
                    return None
            except (ValueError, KeyError) as e:
                errors.append(f"{p.name}: unlesbare Antwort ({e})")
                return None
            if attempt < self.s.max_retries:
                self._sleep(delay)
                delay *= 2
        return None

    # -- viele Adressen ------------------------------------------------------
    def geocode_many(self, queries: Iterable[str]) -> Iterator[tuple[int, GeoResult]]:
        for i, q in enumerate(queries):
            yield i, self.geocode(q)

    def close(self):
        self.cache.close()


# --------------------------------------------------------------------------- #
# Hilfen für Tabellen
# --------------------------------------------------------------------------- #
_FIELD = re.compile(r"\{([^{}]+)\}")


def build_address(row: dict, columns: list[str] | None = None, template: str | None = None) -> str:
    """Adresse aus Tabellenzeile: Vorlage wie "{Straße} {Nr}, {PLZ} {Ort}" oder Spaltenliste."""
    def val(c):
        v = row.get(c, "")
        if v is None or (isinstance(v, float) and v != v):  # NaN
            return ""
        if isinstance(v, float) and v.is_integer():          # PLZ 60311.0 -> 60311
            v = int(v)
        return str(v).strip()

    if template:
        text = _FIELD.sub(lambda m: val(m.group(1)), template)
    else:
        text = ", ".join(x for x in (val(c) for c in (columns or [])) if x)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(\s*,\s*)+", ", ", text)
    return text.strip(" ,")


def template_fields(template: str) -> list[str]:
    return _FIELD.findall(template or "")


def estimate_hours(n_uncached: int, settings: Settings, providers: list[Provider]) -> float:
    """Grobe Laufzeit: das engere von globalem Limit und schnellstem erstem Anbieter."""
    per_req = max(3600.0 / settings.max_per_hour, providers[0].min_interval if providers else 0)
    return n_uncached * per_req / 3600.0
