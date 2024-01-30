"""The individual lookups for each of the various platforms.

This is where logic lives to load the configuration file and perform the lookups
for "what was done between this period".
"""

import json
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import rich
import tomli
from gidgethub import BadRequest, RedirectionException
from gidgethub.httpx import GitHubAPI
from httpx_cache import AsyncClient, Client, FileCache
from pydantic import BaseModel, DirectoryPath, Field, HttpUrl
from rich.markup import escape

CACHE = FileCache()


class GitHubSearchConfiguration(BaseModel):
    heading: str
    kind: Literal["issues", "commits"]
    term: str


class GitHubEventsConfiguration(BaseModel):
    user: str


class GitHubConfiguration(BaseModel):
    api: HttpUrl
    token: str

    search: list[GitHubSearchConfiguration] = []
    events: list[GitHubEventsConfiguration] = []


class LocalGitConfiguration(BaseModel):
    directory: DirectoryPath


class DiscourseConfiguration(BaseModel):
    instance: HttpUrl
    username: str


class Configuration(BaseModel):
    local_git: list[LocalGitConfiguration] = Field(alias="local-git", default=[])
    github: list[GitHubConfiguration] = []
    discourse: list[DiscourseConfiguration] = []


# -- Configuration parsing and execution -----------------------------------------------
class Stop(Exception):
    """For stopping the program, but not presenting a traceback."""


def load_configuration() -> Configuration:
    with Path("~/.did/config.ignore.toml").expanduser().open("rb") as f:
        data = tomli.load(f)

    return Configuration.model_validate(data)


async def run_configuration(config: Configuration, *, since: date, until: date) -> None:
    try:
        for github in config.github:
            await lookup_github(github, since=since, until=until)
        for discourse in config.discourse:
            await lookup_discourse(discourse, since=since, until=until)
        for local_git in config.local_git:
            await lookup_local_git(local_git, since=since, until=until)
    except Stop:
        sys.exit(1)


# -- GitHub ----------------------------------------------------------------------------
GITHUB_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
GITHUB_DATE_FORMAT = "%Y-%m-%d"


def repository_from_repo_url(url: str) -> str:
    return "/".join(url.rsplit("/", 2)[-2:])


def _log_entry(repo: str, text: str, *, url: str) -> None:
    rich.print(f"- [blue bold link={url}]{escape(repo)}[/] -- {escape(text)}")


async def _github_search(
    gh: GitHubAPI, config: GitHubSearchConfiguration, *, since: date, until: date
) -> None:
    print(f"### {config.heading}")
    print()

    search_term = config.term.format(
        time=(
            f"{since.strftime(GITHUB_DATE_FORMAT)}"
            f"..{until.strftime(GITHUB_DATE_FORMAT)}"
        )
    )
    url = f"search/{config.kind}?q={{q}}"

    item = None
    async for item in gh.getiter(url, dict(q=search_term)):
        if "commit" in item:
            _log_entry(
                repo=f"{item['repository']['full_name']}@{item['sha'][:8]}",
                text=item["commit"]["message"].partition("\n")[0],
                url=item["html_url"],
            )
        else:
            assert "title" in item
            repo = repository_from_repo_url(item["repository_url"])
            if "number" in item:
                repo += f"#{item['number']}"
            _log_entry(
                repo=repo,
                text=item["title"],
                url=item["html_url"],
            )
    if item is None:
        print("Nothing.")
    print()


async def _github_events(
    gh: GitHubAPI, config: GitHubEventsConfiguration, *, since: date, until: date
) -> None:
    print("### All events (reverse chronological)")
    print()

    if (date.today() - until) > timedelta(days=90):
        print("GitHub's event history does not go this far back.")
        print()
        return

    async for event in gh.getiter("users/{username}/events", {"username": config.user}):
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

        event_date = datetime.strptime(date_string, GITHUB_DATETIME_FORMAT).date()

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


async def lookup_github(
    config: GitHubConfiguration, *, since: date, until: date
) -> None:
    async with AsyncClient(cache=CACHE) as client:
        gh = GitHubAPI(
            client,
            "pradyunsg-did",
            base_url=str(config.api),
            oauth_token=config.token,
        )
        try:
            await gh.getitem("rate_limit")
        except BadRequest as e:
            rich.print(f"[bold][red]GitHub token issue[/]:[/] {e}", file=sys.stderr)
            raise Stop()
        except RedirectionException as e:
            rich.print(f"[bold][red]GitHub URL issue[/]:[/] {e}", file=sys.stderr)
            raise Stop()

        print(f"## {config.api.host} (GitHub)")

        for search in config.search:
            await _github_search(gh, search, since=since, until=until)
        for event in config.events:
            await _github_events(gh, event, since=since, until=until)


# -- Local Git repos -------------------------------------------------------------------
async def lookup_local_git(
    config: LocalGitConfiguration, *, since: date, until: date
) -> None:
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
    for item in config.directory.iterdir():  # noqa: F821
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


# -- Discourse -------------------------------------------------------------------------
async def lookup_discourse(
    config: DiscourseConfiguration, *, since: date, until: date
) -> None:
    """Get user activity from Discourse.

    This is using the documented JSON blob that is backing the activity page:
    https://discuss.python.org/u/pradyunsg/activity

    See https://github.com/discourse/discourse/blob/5e534e5/app/models/user_action.rb
    for the filter codes, and what they mean.
    """
    DISCOURSE_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

    _one_hour_in_seconds = 60 * 60
    headers = {"cache-control": f"max-age={_one_hour_in_seconds}"}

    def get_bounded_user_actions():
        offset = 0
        client = Client(cache=CACHE, headers=headers)
        while True:
            print(f"<!-- offset: {offset} -->")
            response = client.request(
                "GET",
                f"{config.instance}/user_actions.json",
                params={
                    "offset": offset,
                    "username": config.username,
                    "filter": "1,4,5",
                },
            )
            actions = response.json()["user_actions"]

            for item in actions:
                created_at = datetime.strptime(
                    item["created_at"], DISCOURSE_DATETIME_FORMAT
                ).date()
                offset += 1
                if created_at > until:
                    # Need older events, still.
                    continue
                if created_at < since:
                    # We're now past the boundary that we needed to look at.
                    return
                yield item

    print(f"## {config.instance} (Discourse)")
    print()

    total_liked = 0
    new_topics = []
    replies_by_topic: defaultdict[tuple[str, str], int] = defaultdict(int)

    for item in get_bounded_user_actions():
        action_type = item["action_type"]
        key = (item["title"], f"{config.instance}/t/{item['topic_id']}")
        if action_type == 1:  # LIKE
            total_liked += 1
        elif action_type == 4:  # NEW_TOPIC
            new_topics.append(key)
        elif action_type == 5:  # REPLY
            replies_by_topic[key] += 1

    if new_topics:
        if len(new_topics) == 1:
            print("Created 1 new topic:")
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
        print("Liked 1 post.")
    else:
        print(f"Liked {total_liked} posts.")

    print()
