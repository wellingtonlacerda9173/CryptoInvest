"""A trava de bancos descartaveis: testes destrutivos so rodam em bancos `*_test`."""

import pytest
from sqlalchemy import URL

from tests.integration.disposable_db import NotDisposableDatabaseError, assert_disposable_database


@pytest.mark.parametrize("name", ["cryptoinvest", "cryptoinvest_test_old", "test", "postgres", ""])
def test_non_disposable_names_are_refused(name: str) -> None:
    url = URL.create("postgresql+asyncpg", username="u", host="h", database=name or None)

    with pytest.raises(NotDisposableDatabaseError):
        assert_disposable_database(url)


def test_a_disposable_name_is_accepted_from_url_or_string() -> None:
    url = URL.create("postgresql+asyncpg", username="u", host="h", database="cryptoinvest_migtest_ab12_test")

    assert assert_disposable_database(url) == "cryptoinvest_migtest_ab12_test"
    assert assert_disposable_database("postgresql+asyncpg://u:p@h/x_test") == "x_test"
    with pytest.raises(NotDisposableDatabaseError):
        assert_disposable_database("postgresql+asyncpg://u:p@h/cryptoinvest")
