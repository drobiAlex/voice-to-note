import pytest

from voice_to_note.transforms.languages import (
    language_name,
    parse_languages,
    resolve_language,
)


def test_a_language_name_resolves_to_its_code_whatever_the_case():
    assert [resolve_language(w) for w in ("English", "ukrainian", "POLISH")] == ["en", "uk", "pl"]


def test_a_code_resolves_to_itself_whatever_the_case():
    assert [resolve_language(w) for w in ("en", "UK", " pl ")] == ["en", "uk", "pl"]


def test_a_regional_code_keeps_its_region():
    assert resolve_language("pt-BR") == "pt-BR"


def test_a_code_missing_from_the_table_is_still_accepted():
    assert resolve_language("yue") == "yue"


def test_an_unknown_name_is_refused_with_the_closest_names():
    with pytest.raises(ValueError, match="did you mean Ukrainian"):
        resolve_language("ukranian")


def test_a_name_nothing_resembles_is_refused_without_a_guess():
    with pytest.raises(ValueError) as refused:
        resolve_language("zzzzzzzz")

    assert "did you mean" not in str(refused.value)


def test_a_preference_list_keeps_its_order_and_drops_repeats_and_blanks():
    assert parse_languages("english, uk,,Polish, en,") == ["en", "uk", "pl"]


def test_a_language_is_named_from_the_table_before_what_yt_dlp_called_it():
    assert language_name("uk", "Ukrainian (auto-generated)") == "Ukrainian"


def test_a_language_the_table_lacks_takes_the_name_yt_dlp_reported():
    assert language_name("yue-orig", "Cantonese (Original)") == "Cantonese"


def test_a_language_with_no_name_at_all_is_called_by_its_code():
    assert language_name("xx") == "xx"
