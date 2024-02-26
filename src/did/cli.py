import asyncio
import calendar
from datetime import date, datetime, timedelta
from typing import Literal

import click
import httpx
import httpx_cache
import rich.traceback

from .lookups import load_configuration, run_configuration

Period = Literal["week", "month", "quarter", "year"]
_DATE_FORMAT = "%Y-%m-%d"
MONTHS_TO_NUMBER = {
    month.lower(): index for index, month in enumerate(calendar.month_abbr) if month
}


# -- Logic for time description handling -----------------------------------------------
def days(n: int) -> timedelta:
    return timedelta(days=n)


def get_this_period(today: date, period: Period) -> tuple[str, date, date]:
    if period == "week":
        start_date = today - days(today.weekday())
        period_ref = start_date.strftime("week %W of %Y")
    elif period == "month":
        start_date = today - days(today.day - 1)
        period_ref = start_date.strftime("%B %Y")
    elif period == "quarter":
        quarter_number = (today.month - 1) // 3
        quarter_start_month = quarter_number * 3 + 1
        start_date = date(today.year, quarter_start_month, 1)
        period_ref = start_date.strftime(f"Q{quarter_number+1} %Y")
    elif period == "year":
        start_date = date(today.year, 1, 1)
        period_ref = start_date.strftime("%Y")
    else:
        assert False

    return period_ref, start_date, today


def previous_quarter(ref: date) -> date:
    if ref.month < 4:
        return date(ref.year - 1, 12, 31)
    elif ref.month < 7:
        return date(ref.year, 3, 31)
    elif ref.month < 10:
        return date(ref.year, 6, 30)
    return date(ref.year, 9, 30)


def get_last_period(today: date, period: Period) -> tuple[str, date, date]:
    if period == "week":
        end_date = today - days(today.weekday() + 1)
        start_date = end_date - days(6)
        period_ref = start_date.strftime("week %W of %Y")
    elif period == "month":
        end_date = today - days(today.day)
        _, month_days_count = calendar.monthrange(
            year=end_date.year, month=end_date.month
        )
        start_date = end_date - days(month_days_count - 1)
        period_ref = start_date.strftime("%B %Y")
    elif period == "quarter":
        end_date = previous_quarter(today)
        start_date = date(end_date.year, end_date.month - 3, 1)
        period_ref = start_date.strftime(f"Q{start_date.month // 3 + 1} %Y")
    elif period == "year":
        start_date = date(today.year - 1, 1, 1)
        end_date = date(today.year, 1, 1) - days(1)
        period_ref = start_date.strftime("%Y")
    else:
        assert False

    return period_ref, start_date, end_date


def convert_to_range(period: str) -> tuple[date, date]:
    if len(period) == 4 and period.isnumeric():
        year = int(period)
        assert year > 2000

        _, last_year_day = calendar.monthrange(year=year, month=12)
        return date(year, 1, 1), date(year, 12, last_year_day)

    if len(period) == 7 and period.lower().startswith("q") and "-" in period:
        quarter, _, year = period.partition("-")
        quarter = quarter.lower()
        assert quarter in {"q1", "q2", "q3", "q4"}, "expected quarter number"
        assert year.isnumeric(), "expected a number"

        quarter_n = int(quarter[1])
        year_n = int(year)

        first_qmonth = (3 * (quarter_n - 1)) + 1
        last_qmonth = 3 * quarter_n
        _, last_qday = calendar.monthrange(year=year_n, month=last_qmonth)
        return (
            date(year=year_n, month=first_qmonth, day=1),
            date(year=year_n, month=last_qmonth, day=last_qday),
        )

    if len(period) == 8 and "-" in period:
        month, _, year = period.partition("-")
        assert (
            month in MONTHS_TO_NUMBER
        ), f"expected a month name from {list(MONTHS_TO_NUMBER)}"
        assert year.isnumeric(), "expected a number"

        month_n = MONTHS_TO_NUMBER[month]
        year_n = int(year)
        _, month_days_count = calendar.monthrange(year=year_n, month=month_n)

        return date(year_n, month_n, 1), date(year_n, month_n, month_days_count)

    assert False, "Expected period to be YYYY or MMM-YYYY or Q{N}-YYYY"


# -- Logic for dispatching lookups -----------------------------------------------------
@click.group()
def did():
    """Present statistics about what I did."""
    rich.traceback.install(
        suppress=[asyncio, httpx, httpx_cache, click], show_locals=True
    )


@did.command()
@click.argument("since", metavar="since", type=click.DateTime([_DATE_FORMAT]))
@click.argument("until", metavar="until", type=click.DateTime([_DATE_FORMAT]))
def between(since: datetime, until: datetime):
    """stats between two provided dates"""
    if since.date() >= until.date():
        raise click.UsageError(
            "'since' must be a date before 'until'.\n"
            f"{since.date()!r} >= {until.date()!r}"
        )

    main(since=since.date(), until=until.date())


@did.command()
@click.argument("period", type=click.Choice(["week", "month", "quarter", "year"]))
def last(period: Period):
    """stats for previous [period header time]"""
    period_ref, since, until = get_last_period(today=date.today(), period=period)
    print(f"# Status update for {period_ref} ({since} to {until})")
    main(since=since, until=until)


@did.command()
@click.argument("period", type=click.Choice(["week", "month", "quarter", "year"]))
def this(period: Period):
    """stats for current [period header time]"""
    period_ref, since, until = get_this_period(today=date.today(), period=period)
    print(f"# Status update for {period_ref} ({since} to {until}*)")
    main(since=since, until=until)


@did.command("yesterday")
def yesterday_():
    """stats for previous day"""
    yesterday = date.today() - days(1)

    print(f"# Log items for {yesterday}")
    main(since=yesterday, until=yesterday)


@did.command("today")
def today_():
    """stats for current day"""
    today = date.today()

    print(f"# Log items for {today}*")
    main(since=today, until=today)


@did.command("month")
@click.argument("month", type=click.DateTime(["%Y-%m", "%B", "%b"]))
def month_(month: datetime):
    """stats for given month"""
    if month.year == 1900:
        start = date(year=date.today().year, month=month.month, day=month.day)
    else:
        start = month.date()

    _, month_days_count = calendar.monthrange(year=start.year, month=start.month)
    end = start + days(month_days_count - 1)

    print(f"# Log items for {start.strftime('%B %Y')} ({start} to {end})")
    main(since=start, until=end)


@did.command("on")
@click.argument("on", metavar="date", type=click.DateTime([_DATE_FORMAT]))
def on_(on: datetime):
    """stats for given date"""
    date_ = on.date()

    print(f"# Log items for {date_.strftime('%-d %B %Y')}")
    main(since=date_, until=date_)


@did.command("in")
@click.argument("period", type=str)
def in_(period: str):
    """stats for given MMM-YYYY"""
    since, until = convert_to_range(period)

    print(f"# Log items for {period.capitalize()} ({since} to {until})")
    main(since=since, until=until)


@did.command("since")
@click.argument("since", metavar="date", type=click.DateTime([_DATE_FORMAT]))
def since_(since: datetime):
    """stats starting from given date"""
    since = since.date()
    until = date.today()

    print(f"# Log items since {since} (until {until}*)")
    main(since=since, until=until)


# -- Dispatch logic --------------------------------------------------------------------
def main(*, since: date, until: date) -> None:
    configuration = load_configuration()
    asyncio.run(run_configuration(configuration, since=since, until=until))
