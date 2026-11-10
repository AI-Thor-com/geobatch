import json
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobatch_core import (ConfigError, Geocoder, Settings, build_address, normalize_query)  # noqa: E402
from geobatch_io import guess_template, read_table  # noqa: E402


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class FakeSession:
    """Antwortet je nach Host; protokolliert Aufrufe."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        for host, fn in self.routes.items():
            if host in url:
                return fn(params)
        raise AssertionError(url)


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def photon_hit(lat=50.11, lon=8.68, cc="DE"):
    return lambda p: FakeResp(200, {"features": [{"geometry": {"coordinates": [lon, lat]},
                                                  "properties": {"street": "Römerberg", "housenumber": "27",
                                                                 "postcode": "60311", "city": "Frankfurt",
                                                                 "country": "Deutschland", "countrycode": cc,
                                                                 "type": "house"}}]})


def photon_empty(p):
    return FakeResp(200, {"features": []})


def nominatim_hit(p):
    return FakeResp(200, [{"lat": "48.1", "lon": "11.5", "display_name": "München", "importance": 0.7,
                           "addresstype": "city"}])


def make(tmp_path, routes, **kw):
    fc = FakeClock()
    s = Settings(cache_path=str(tmp_path / "c.sqlite"), **kw)
    sess = FakeSession(routes)
    return Geocoder(s, session=sess, clock=fc.clock, sleep=fc.sleep), sess, fc


def test_photon_ok_and_cache(tmp_path):
    gc, sess, _ = make(tmp_path, {"photon": photon_hit()})
    r = gc.geocode("Römerberg 27, 60311 Frankfurt")
    assert r.status == "ok" and r.lat == 50.11 and r.lon == 8.68 and r.provider == "photon"
    assert "Römerberg 27" in r.address
    r2 = gc.geocode("  römerberg 27 ,60311  frankfurt ")   # gleiche Adresse, anders geschrieben
    assert r2.from_cache and len(sess.calls) == 1


def test_throttle_1000_per_hour(tmp_path):
    gc, sess, fc = make(tmp_path, {"photon": photon_hit()}, max_per_hour=1000)
    t_start = fc.t
    for i in range(11):
        gc.geocode(f"Straße {i}, Frankfurt")
    # 11 Anfragen -> 10 Abstände à 3,6 s
    assert fc.t - t_start == pytest.approx(36.0)
    assert len(sess.calls) == 11


def test_country_filter_photon(tmp_path):
    gc, _, _ = make(tmp_path, {"photon": photon_hit(cc="FR")}, countrycodes="de",
                    fallback_on_not_found=False)
    assert gc.geocode("Rue X, Paris").status == "not_found"


def test_fallback_to_nominatim(tmp_path):
    gc, sess, _ = make(tmp_path, {"photon": photon_empty, "nominatim": nominatim_hit},
                       providers=["photon", "nominatim"], contact="a@b.de")
    r = gc.geocode("Marienplatz 1, München")
    assert r.status == "ok" and r.provider == "nominatim"
    ua = sess.calls[-1][2]["User-Agent"]
    assert "GeoBatch" in ua and "a@b.de" in ua
    assert sess.calls[-1][1]["countrycodes"] == "de"


def test_nominatim_requires_contact(tmp_path):
    with pytest.raises(ConfigError):
        make(tmp_path, {}, providers=["nominatim"])


def test_nominatim_self_hosted_no_contact_no_throttle(tmp_path):
    gc, sess, fc = make(tmp_path, {"localhost": nominatim_hit}, providers=["nominatim"],
                        base_urls={"nominatim": "http://localhost:8080"}, max_per_hour=3600)
    assert gc.geocode("X").provider == "nominatim-self"


def test_429_pauses_provider_and_is_not_cached(tmp_path):
    gc, sess, fc = make(tmp_path, {"photon": lambda p: FakeResp(429, {})})
    r = gc.geocode("A-Straße 1")
    assert r.status == "error" and "429" in r.error
    n = len(sess.calls)
    r = gc.geocode("B-Straße 2")            # Anbieter pausiert -> keine weitere Anfrage
    assert r.status == "error" and len(sess.calls) == n
    assert gc.cache.get(normalize_query("A-Straße 1") + "|de") is None


def test_server_error_retries_then_error(tmp_path):
    gc, sess, fc = make(tmp_path, {"photon": lambda p: FakeResp(503, {})}, max_retries=3)
    r = gc.geocode("C-Straße 3")
    assert r.status == "error" and len(sess.calls) == 3


def test_not_found_cached_unless_retry(tmp_path):
    gc, sess, _ = make(tmp_path, {"photon": photon_empty})
    gc.geocode("Nirgendwo 1")
    gc.geocode("Nirgendwo 1")
    assert len(sess.calls) == 1
    gc.s.retry_not_found = True
    gc.geocode("Nirgendwo 1")
    assert len(sess.calls) == 2


def test_daily_quota(tmp_path):
    routes = {"geoapify": lambda p: FakeResp(200, {"results": [{"lat": 1, "lon": 2, "formatted": "x",
                                                                 "rank": {"confidence": 0.9}}]})}
    gc, sess, _ = make(tmp_path, routes, providers=["geoapify"], keys={"geoapify": "k"})
    for _ in range(3000):
        gc.cache.count("geoapify")
    r = gc.geocode("Q 1")
    assert r.status == "error" and "kontingent" in r.error.lower() and not sess.calls


def test_keyed_providers_parse(tmp_path):
    routes = {
        "locationiq": lambda p: FakeResp(200, [{"lat": "1.5", "lon": "2.5", "display_name": "L",
                                                "importance": 0.5, "type": "house"}]),
        "opencagedata": lambda p: FakeResp(200, {"results": [{"geometry": {"lat": 3, "lng": 4},
                                                              "formatted": "O", "confidence": 9,
                                                              "components": {"_type": "building"}}]}),
    }
    gc, _, _ = make(tmp_path, routes, providers=["locationiq"], keys={"locationiq": "k"})
    assert gc.geocode("a").lat == 1.5
    (tmp_path / "oc").mkdir()
    gc2, _, _ = make(tmp_path / "oc", routes, providers=["opencage"], keys={"opencage": "k"})
    r = gc2.geocode("b")
    assert (r.lat, r.lon, r.quality, r.match_type) == (3.0, 4.0, 0.9, "building")


def test_locationiq_404_is_not_found(tmp_path):
    gc, _, _ = make(tmp_path, {"locationiq": lambda p: FakeResp(404, {"error": "Unable to geocode"})},
                    providers=["locationiq"], keys={"locationiq": "k"})
    assert gc.geocode("zzz").status == "not_found"


def test_missing_key(tmp_path):
    with pytest.raises(ConfigError):
        make(tmp_path, {}, providers=["geoapify"])


def test_build_address_template_and_nan():
    row = {"Straße": "Zeil", "Nr": "1", "PLZ": 60313.0, "Ort": "Frankfurt", "Leer": float("nan")}
    assert build_address(row, template="{Straße} {Nr}, {PLZ} {Ort}") == "Zeil 1, 60313 Frankfurt"
    assert build_address(row, columns=["Straße", "Leer", "Ort"]) == "Zeil, Frankfurt"
    assert build_address({"Straße": "", "Ort": "Köln"}, template="{Straße}, {Ort}") == "Köln"


def test_guess_template():
    assert guess_template(["ID", "Straße", "Hausnummer", "PLZ", "Ort"]) == "{Straße} {Hausnummer}, {PLZ} {Ort}"
    assert guess_template(["Adresse", "Name"]) == "{Adresse}"
    assert guess_template(["foo", "bar"]) is None


def test_read_csv_semicolon_cp1252(tmp_path):
    p = tmp_path / "a.csv"
    p.write_bytes("Straße;PLZ;Ort\nMünchner Str. 1;80331;München\n".encode("cp1252"))
    df = read_table(str(p))
    assert list(df.columns) == ["Straße", "PLZ", "Ort"] and df.iloc[0]["Ort"] == "München"
    assert df.iloc[0]["PLZ"] == "80331"   # führende Nullen/Text bleiben erhalten


def test_shared_throttle_is_process_wide():
    from geobatch_core import shared_throttle
    a = shared_throttle("photon-test", 1.0)
    b = shared_throttle("photon-test", 0.5)
    assert a is b and a.min_interval == 1.0
