from __future__ import annotations

import pytest

from visu_gateway import secret_filter as sf


def clean(s, suspicious=False):
    red: list = []
    return sf.clean_string(s, "p", suspicious, red), red


def test_empty_and_plain():
    assert clean("") == ("", [])
    assert clean("Wohnzimmer 21 °C")[0] == "Wohnzimmer 21 °C"


def test_jwt_and_long_token():
    assert clean("eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM")[0] == sf.REDACTED
    assert clean("A" * 32)[0] == sf.REDACTED
    assert clean("A" * 31)[0] != sf.REDACTED


def test_guid_is_not_a_token():
    assert clean("{3565B1F2-8F7B-4311-A4B6-1BF1D868F39E}")[0] == "{3565B1F2-8F7B-4311-A4B6-1BF1D868F39E}"


def test_url_credentials_and_params():
    out, red = clean("https://admin:geheim@host/path?token=abc&x=1&apikey=zzz")
    assert "admin" not in out and "geheim" not in out and "abc" not in out and "zzz" not in out
    assert "x=1" in out and len(red) == 2


def test_truncate():
    out, _ = clean("x " * 400)
    assert out.endswith("…[gekürzt]") and len(out) < 330


@pytest.mark.parametrize("key", ["Password", "api_key", "Benutzer", "refreshToken", "PIN", "Seriennummer", "mailAddress"])
def test_suspicious_keys_redact_strings(key):
    assert sf.clean_recursive({key: "wert"}, "p", []) == {key: sf.REDACTED}


def test_non_strings_untouched_under_suspicious_key():
    assert sf.clean_recursive({"Password": 1234, "ok": True}, "p", []) == {"Password": 1234, "ok": True}


def test_pin_is_word_boundary():
    assert sf.clean_recursive({"Spinner": "x", "pin": "y"}, "p", []) == {"Spinner": "x", "pin": sf.REDACTED}


def test_nested_lists():
    out = sf.clean_recursive({"a": [{"token": "t"}, "http://u:p@h/"]}, "p", [])
    assert out["a"][0] == {"token": sf.REDACTED} and "u:p" not in out["a"][1]


def test_clean_value_uses_name():
    assert sf.clean_value("1234", "WLAN Passwort", "p", []) == sf.REDACTED
    assert sf.clean_value(1234, "WLAN Passwort", "p", []) == 1234  # bekannte Grenze: nur Strings
    assert sf.clean_value("kurz", "Temperatur", "p", []) == "kurz"
