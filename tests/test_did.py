from datetime import date

import did


def test_get_this_period():
    assert did.get_this_period(date(2021, 12, 10), "week") == (
        date(2021, 12, 6),
        date(2021, 12, 10),
    )
    assert did.get_this_period(date(2021, 12, 10), "month") == (
        date(2021, 12, 1),
        date(2021, 12, 10),
    )
    assert did.get_this_period(date(2021, 12, 10), "quarter") == (
        date(2021, 10, 1),
        date(2021, 12, 10),
    )
    assert did.get_this_period(date(2021, 12, 10), "year") == (
        date(2021, 1, 1),
        date(2021, 12, 10),
    )


def test_get_last_period():
    assert did.get_last_period(date(2021, 12, 10), "week") == (
        date(2021, 11, 29),
        date(2021, 12, 5),
    )
    assert did.get_last_period(date(2021, 12, 10), "month") == (
        date(2021, 11, 1),
        date(2021, 11, 30),
    )
    assert did.get_last_period(date(2021, 12, 10), "quarter") == (
        date(2021, 9, 1),
        date(2021, 11, 30),
    )
    assert did.get_last_period(date(2021, 12, 10), "year") == (
        date(2020, 1, 1),
        date(2020, 12, 31),
    )
