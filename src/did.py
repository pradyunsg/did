#!/usr/bin/env python
"""Generate a report of what I've done in the recent past.

This is useful for me to figure out what all I've done, which I can then utilize
to both (a) monitor what I'm doing things and (b) reduce the energy spent in
OSS update blog posts.
"""

__version__ = "0.1.0"

import asyncio
import calendar
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import click
import gidgethub
import gidgethub.httpx
import httpx
import httpx_cache
import rich.traceback

Period = Literal["week", "month", "quarter", "year"]
_DATE_FORMAT = "%Y-%m-%d"
_DISCOURSE_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_GH_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TOKEN_FILE = Path(os.environ["HOME"]) / ".did-gh-token"

rich.traceback.install(suppress=[asyncio, httpx, httpx_cache, click], show_locals=True)


def gh_token_issue(reason: str) -> None:
    def out(*args: str) -> None:
        rich.print(*args, file=sys.stderr)

    out("[bold][red]GitHub token issue[/]:[/]", reason)
    out()
    out("To create a new token:")
    out("1. Go and create a token at:")
    out("   https://github.com/settings/tokens/new?description=did-{date.today()}")
    out("2. Write the token in {TOKEN_FILE} and make sure it's not world-readable.")


try:
    GH_TOKEN = _TOKEN_FILE.read_text().strip()
except OSError:
    gh_token_issue(f"could not read token file ({_TOKEN_FILE})")
    sys.exit(2)


CACHE = httpx_cache.FileCache()
MONTHS_TO_NUMBER = {
    month.lower(): index for index, month in enumerate(calendar.month_abbr) if month
}


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
    month_s, _, year_s = period.partition("-")
    assert (
        month_s in MONTHS_TO_NUMBER
    ), f"expected a month name from {list(MONTHS_TO_NUMBER)}"
    assert year_s.isnumeric(), "expected a number"

    month = MONTHS_TO_NUMBER[month_s]
    year = int(year_s)
    _, month_days_count = calendar.monthrange(year=year, month=month)

    return date(year, month, 1), date(year, month, month_days_count)


def discourse(since: date, until: date, *, host: str) -> None:
    """Get user activity from Discourse.

    This is using the documented JSON blob that is backing the activity page:
    https://discuss.python.org/u/pradyunsg/activity

    See https://github.com/discourse/discourse/blob/5e534e5/app/models/user_action.rb
    for the filter codes, and what they mean.
    """

    def get_bounded_user_actions():
        offset = 0
        client = httpx_cache.Client(
            cache=CACHE, headers={"cache-control": "max-age=604800"}
        )
        while True:
            print(f"<!-- offset: {offset} -->")
            response = client.request(
                "GET",
                f"https://{host}/user_actions.json",
                params={
                    "offset": offset,
                    "username": "pradyunsg",
                    "filter": "1,4,5",
                },
            )
            actions = response.json()["user_actions"]

            for item in actions:
                created_at = datetime.strptime(
                    item["created_at"], _DISCOURSE_DATETIME_FORMAT
                ).date()
                offset += 1
                if created_at > until:
                    # Need older events, still.
                    continue
                if created_at < since:
                    # We're now past the boundary that we needed to look at.
                    return
                yield item

    print(f"## {host} (Discourse)")
    print()

    total_liked = 0
    new_topics = []
    replies_by_topic = defaultdict(int)

    for item in get_bounded_user_actions():
        action_type = item["action_type"]
        key = (item["title"], f"https://{host}/t/{item['topic_id']}")
        if action_type == 1:  # LIKE
            total_liked += 1
        elif action_type == 4:  # NEW_TOPIC
            new_topics.append(key)
        elif action_type == 5:  # REPLY
            replies_by_topic[key] += 1

    if new_topics:
        if len(new_topics) == 1:
            print(f"Created 1 new topic:")
        else:
            print(f"Created {len(new_topics)} new topics:")
        print()
        for topic, url in new_topics:
            print(f"- {topic} ({url})")
        print()
    if replies_by_topic:
        total_replies = sum(replies_by_topic.values())
        topic_word = "topic" if len(replies_by_topic) == 1 else "topics"
        reply_word = "reply" if total_replies == 1 else "replies"
        print(
            f"Wrote {total_replies} {reply_word} in "
            f"{len(replies_by_topic)} {topic_word}:"
        )
        print()
        for (topic, url), count in replies_by_topic.items():
            word = "reply" if count == 1 else "replies"
            print(f"- {count} {word} in {topic} ({url})")
        print()

    if total_liked == 1:
        print(f"Liked 1 post.")
    else:
        print(f"Liked {total_liked} posts.")


def local_git_projects(since: date, until: date, *, directory: str) -> None:
    command = [
        "git",
        "log",
        "--all",
        "--format=format:- %h%Cblue%d%Creset %s",
        "--author=Pradyun",
        "--author=Pradyun Gedam",
        "--author=mail@pradyunsg.me",
        "--author=oss@pradyunsg.me",
        "--author=pgedam@bloomberg.net",
        "--author=pradyunsg@users.noreply.github.com",
        "--author=pradyunsg@gmail.com",
        f"--since={since} 00:00:00",
        f"--until={until} 23:59:59",
    ]

    print("## Local Repositories")
    print()

    did_something = False
    for item in Path(directory).iterdir():
        if not (item / ".git").exists():
            continue

        process = subprocess.run(
            command, cwd=item, capture_output=True, encoding="utf-8"
        )
        if process.stderr or process.returncode:
            print(f"<!-- Encountered error: {item}")
            print(process.stderr)
            print(f"exited with code: {process.returncode}")
            print("-->")
            print()
        if process.stdout:
            did_something = True
            print(f"### {item.name}")
            print()
            print(process.stdout)
            print()

    if not did_something:
        print("Nothing.")
        print()


async def github(since: date, until: date):
    time_term = f"{since.strftime(_DATE_FORMAT)}..{until.strftime(_DATE_FORMAT)}"

    print("## GitHub")
    print()

    searches = {
        "Issues created": f"author:pradyunsg type:issue created:{time_term}",
        "Assigned issues closed": f"assignee:pradyunsg type:issue closed:{time_term}",
        "PRs created": f"author:pradyunsg type:pr created:{time_term}",
        "PRs reviewed": f"reviewed-by:pradyunsg type:pr reviewed:{time_term}",
        "Assigned PRs closed": f"assignee:pradyunsg type:pr closed:{time_term}",
    }
    # event_types_to_track = {
    #     "CommitCommentEvent",
    #     "CreateEvent",
    #     "DeleteEvent",
    #     "ForkEvent",
    #     "IssueCommentEvent",
    #     "IssuesEvent",
    #     "PublicEvent",
    #     "PullRequestEvent",
    #     "PullRequestReviewEvent",
    #     "PullRequestReviewCommentEvent",
    #     "PushEvent",
    #     "ReleaseEvent",
    #     "SponsorshipEvent",
    # }

    async with httpx_cache.AsyncClient(cache=CACHE) as client:
        gh = gidgethub.httpx.GitHubAPI(
            client,
            "pradyunsg",
            oauth_token=GH_TOKEN,
        )

        # Various Searches
        for heading, term in searches.items():
            print(f"### {heading}")
            print()
            item = None
            async for item in gh.getiter("/search/issues?q={q}", dict(q=term)):
                print("-", item["title"], "--", item["html_url"])
            if item is None:
                print("Nothing.")
            print()
        print()

        print("### All events (reverse chronological)")
        print()
        if (date.today() - until) > timedelta(days=90):
            print("GitHub's event history does not go this far back.")
            print()
            return

        async for event in gh.getiter(
            "/users/{username}/events", {"username": "pradyunsg"}
        ):
            event_type = event["type"]
            # Determine date for the event
            if event_type == "CommitCommentEvent":
                date_string = event["payload"]["comment"]["updated_at"]
            elif event_type == "CreateEvent":
                date_string = event["created_at"]
            elif event_type == "DeleteEvent":
                date_string = event["created_at"]
            elif event_type == "ForkEvent":
                date_string = event["created_at"]
            elif event_type == "IssueCommentEvent":
                date_string = event["payload"]["issue"]["updated_at"]
            elif event_type == "IssuesEvent":
                date_string = event["payload"]["issue"]["updated_at"]
            elif event_type == "PublicEvent":
                raise NotImplementedError(json.dumps(event, indent=2))
            elif event_type == "PullRequestEvent":
                date_string = event["created_at"]
            elif event_type == "PullRequestReviewEvent":
                date_string = event["payload"]["review"]["submitted_at"]
            elif event_type == "PullRequestReviewCommentEvent":
                date_string = event["payload"]["comment"]["updated_at"]
            elif event_type == "PushEvent":
                date_string = event["created_at"]
            elif event_type == "ReleaseEvent":
                date_string = event["created_at"]
            elif event_type == "SponsorshipEvent":
                raise NotImplementedError(json.dumps(event, indent=2))
            else:
                print(f"<!-- ignoring {event_type} -->")
                continue

            event_date = datetime.strptime(date_string, _GH_DATETIME_FORMAT).date()

            # Timeline check
            if event_date > until:
                continue
            if event_date < since:
                break

            # Present the event
            repository = event["repo"]["name"]
            prefix = f"- {event_date} {repository}: "
            if event_type == "CommitCommentEvent":
                comment_url = event["payload"]["comment"]["html_url"]
                print(f"{prefix}Commented on a commit ({comment_url})")
            elif event_type == "CreateEvent":
                ref_type = event["payload"]["ref_type"]
                ref_name = event["payload"]["ref"]
                print(f"{prefix}Created {ref_type} named {ref_name}")
            elif event_type == "DeleteEvent":
                ref_type = event["payload"]["ref_type"]
                ref_name = event["payload"]["ref"]
                print(f"{prefix}Deleted {ref_type} named {ref_name}")
            elif event_type == "ForkEvent":
                destination = event["payload"]["forkee"]["name"]
                print(f"{prefix}Forked to {destination}")
            elif event_type == "IssueCommentEvent":
                issue_url = event["payload"]["issue"]["html_url"]
                issue_title = event["payload"]["issue"]["title"]
                print(f"{prefix}Commented on {issue_title} ({issue_url})")
            elif event_type == "IssuesEvent":
                action = event["payload"]["action"]
                issue_url = event["payload"]["issue"]["html_url"]
                issue_title = event["payload"]["issue"]["title"]
                print(f"{prefix}{action.capitalize()} {issue_title} ({issue_url})")
            elif event_type == "PublicEvent":
                raise NotImplementedError(json.dumps(event, indent=2))
            elif event_type == "PullRequestEvent":
                action = event["payload"]["action"]
                pr_url = event["payload"]["pull_request"]["html_url"]
                pr_title = event["payload"]["pull_request"]["title"]
                print(f"{prefix}{action.capitalize()} {pr_title} ({pr_url})")
            elif event_type == "PullRequestReviewEvent":
                pr_url = event["payload"]["pull_request"]["html_url"]
                pr_title = event["payload"]["pull_request"]["title"]
                print(f"{prefix}Reviewed {pr_title} ({pr_url})")
            elif event_type == "PullRequestReviewCommentEvent":
                pr_url = event["payload"]["pull_request"]["html_url"]
                pr_title = event["payload"]["pull_request"]["title"]
                print(f"{prefix}Posted review comment on {pr_title} ({pr_url})")
            elif event_type == "PushEvent":
                ref = event["payload"]["ref"]
                n = event["payload"]["distinct_size"]
                print(f"{prefix}Pushed {n} size to {ref}")
            elif event_type == "ReleaseEvent":
                action = event["payload"]["action"]
                tag_name = event["payload"]["release"]["tag_name"]
                print(f"{prefix}Release {action}: {tag_name}")
            elif event_type == "SponsorshipEvent":
                raise NotImplementedError(json.dumps(event, indent=2))
            else:
                raise NotImplementedError(json.dumps(event, indent=2))
        else:
            print("Oh no! GitHub's event history ran out. :(")
            print()


def main(*, since: date, until: date) -> None:
    print()
    try:
        asyncio.run(github(since, until))
    except gidgethub.BadRequest:
        rich.print(rich.traceback.Traceback(suppress=[asyncio]), file=sys.stderr)
        gh_token_issue("Maybe the token expired? https://github.com/settings/tokens/")
        sys.exit(1)

    local_git_projects(since, until, directory=os.path.expanduser("~/Developer"))
    local_git_projects(since, until, directory=os.path.expanduser("~/Developer/github"))
    discourse(since, until, host="discuss.python.org")


@click.group()
def did():
    """Present statistics about what I did."""


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
@click.argument("period", metavar="date", type=str)
def on_(period: str):
    """stats for given date"""
    since, until = convert_to_range(period)

    print(f"# Log items for {period.capitalize()} ({since} to {until})")
    main(since=since, until=until)


if __name__ == "__main__":
    did()
