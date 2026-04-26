from core.weather import detect_weather_city


def test_recognized_city_returns_tuple():
    result = detect_weather_city("Météo à Montréal")
    assert isinstance(result, tuple)
    assert result[2] == "Montréal"


def test_recognized_city_no_accent():
    result = detect_weather_city("météo montreal")
    assert isinstance(result, tuple)
    assert result[2] == "Montréal"


def test_recognized_city_toronto():
    result = detect_weather_city("weather in Toronto")
    assert isinstance(result, tuple)
    assert result[2] == "Toronto"


def test_no_city_bare_keyword():
    assert detect_weather_city("météo") == "no_city"


def test_no_city_question():
    assert detect_weather_city("quelle météo ?") == "no_city"


def test_no_city_il_fait_combien():
    assert detect_weather_city("il fait combien") == "no_city"


def test_unknown_city_returns_unknown_city():
    assert detect_weather_city("météo à Paris") == "unknown_city"


def test_unknown_city_english():
    assert detect_weather_city("weather in London") == "unknown_city"


def test_unknown_city_il_fait_combien():
    assert detect_weather_city("il fait combien à Lyon") == "unknown_city"


def test_multiple_cities_returns_multiple():
    assert detect_weather_city("météo à Montréal et Toronto") == "multiple"


def test_multiple_cities_vancouver_quebec():
    assert detect_weather_city("météo Vancouver et Québec") == "multiple"


def test_same_city_dual_spelling_is_single():
    # montreal and montréal refer to the same city — should count as one
    result = detect_weather_city("météo montreal montréal")
    assert isinstance(result, tuple)
    assert result[2] == "Montréal"


def test_non_weather_query_returns_none():
    assert detect_weather_city("bonjour comment vas-tu") is None


def test_non_weather_query_with_city_name_returns_none():
    # mentioning a city without a weather keyword is not a weather query
    assert detect_weather_city("je suis à Montréal") is None
