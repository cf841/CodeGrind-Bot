import ast
import asyncio
import re
from dataclasses import dataclass
from random import random
from typing import TYPE_CHECKING

import aiohttp
import aiohttp.client_exceptions
import backoff
import markdownify

from constants import Difficulty
from utils.common import convert_to_score

if TYPE_CHECKING:
    # To prevent circular imports
    from bot import DiscordBot


URL = "https://leetcode.com/graphql"
HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://leetcode.com",
    "Referer": "https://leetcode.com",
    "Cookie": "csrftoken=; LEETCODE_SESSION=;",
    "x-csrftoken": "",
    "user-agent": "Mozilla/5.0 LeetCode API",
}


semaphore = asyncio.Semaphore(8)


@dataclass
class QuestionInfo:
    premium: bool
    question_id: int
    difficulty: str
    title: str
    link: str
    total_accepted: int
    total_submission: int
    ac_rate: float
    question_rating: int | None
    description: str
    example_one: str
    follow_up: str | None


@dataclass
class Submissions:
    easy: int
    medium: int
    hard: int
    score: int


@dataclass
class UserStats:
    real_name: str
    submissions: Submissions


class RateLimitReached(Exception):
    def __init__(self) -> None:
        super().__init__("ExceptionWithStatusCode. Error: 429. Rate Limited.")


def parse_content(content: str) -> tuple[str, str, str | None]:
    """
    Parses the content of a LeetCode question to extract description and constraints.

    :param content: The HTML content of the question.

    :return: A tuple containing the description and constraints of the question,
             or None if not found.
    """
    position_example_one = content.find(
        '<p><strong class="example">Example 1:</strong></p>'
    )
    position_example_two = content.find(
        '<p><strong class="example">Example 2:</strong></p>'
    )
    position_follow_up = content.find("<strong>Follow up:</strong> ")

    description = html_to_markdown(content[:position_example_one])
    example_one = html_to_markdown(
        content[
            position_example_one
            + len(
                '<p><strong class="example">Example 1:</strong></p>'
            ) : position_example_two
        ]
    )

    follow_up = None
    if position_follow_up != -1:
        follow_up = html_to_markdown(
            content[position_follow_up + len("<strong>Follow up:</strong> ") :]
        )

    return description, example_one, follow_up


def html_to_markdown(html: str) -> str:
    """
    Converts HTML content to Markdown.

    :param html: The HTML content to convert to Markdown.

    :return: The Markdown content.
    """
    # Remove all bold, italics, and underlines from code blocks as markdowns doesn't
    # support this.
    html = re.sub(
        r"(<code>|<pre>)(.*?)(<[/]code>|<[/]pre>)",
        lambda m: m.group(0)
        .replace("<b>", "")
        .replace("</b>", "")
        .replace("<em>", "")
        .replace("</em>", "")
        .replace("<strong>", "")
        .replace("</strong>", "")
        .replace("<u>", "")
        .replace("</u>", ""),
        html,
        flags=re.DOTALL,
    )

    subsitutions = [
        (r"<sup>", r"^"),
        (r"</sup>", r""),
        # Replace image tag with the url src of that image
        (r'<img.*?src="(.*?)".*?>', r""),
        (r"<style.*?>.*?</style>", r""),
        (r"&nbsp;", r" "),
    ]

    for pattern, replacement in subsitutions:
        html = re.sub(pattern, replacement, html, flags=re.DOTALL)

    markdown = markdownify.markdownify(html, heading_style="ATX")

    # Remove unnecessary extra lines
    markdown = re.sub(r"\n\n", "\n", markdown)

    return markdown


async def fetch_random_question(
    bot: "DiscordBot", client_session: aiohttp.ClientSession, difficulty: Difficulty
) -> str | None:
    """
    Fetches a random LeetCode question title slug based on the given difficulty.

    :param difficulty: The difficulty level of the question ("easy", "medium", or "hard"
    ).

    :return: The title slug of a randomly selected question, or None if an error occurs.
    """
    payload = {
        "operationName": "randomQuestion",
        "query": """
        query randomQuestion($categorySlug: String, $filters: QuestionListFilterInput) {
            randomQuestion(categorySlug: $categorySlug, filters: $filters) {
                titleSlug
            }
        }
        """,
        "variables": {
            "categorySlug": "all-code-essentials",
            "filters": (
                {"difficulty": difficulty.name}
                if difficulty != Difficulty.RANDOM
                else {}
            ),
        },
    }

    async with client_session.post(
        URL, json=payload, headers=HEADERS, timeout=10
    ) as response:
        if response.status != 200:
            bot.logger.exception(
                f"fetch_random_question: An error occurred while trying to get the "
                f"question from LeetCode: {response.status}",
            )
            return

        response_data = await response.json()

    try:
        title_slug = response_data["data"]["randomQuestion"]["titleSlug"]

    except ValueError:
        bot.logger.exception(
            f"fetch_random_question: failed to decode json. Error code "
            f"({response_data})"
        )
        return

    return title_slug


async def fetch_daily_question(
    bot: "DiscordBot", client_session: aiohttp.ClientSession
) -> str | None:
    """
    Fetches the title slug of the active daily coding challenge question.

    :return: The title slug of the daily coding challenge question,
             or None if an error occurs.
    """
    data = {
        "operationName": "daily",
        "query": """
        query daily {
            challenge: activeDailyCodingChallengeQuestion {
                question {
                    titleSlug
                }
            }
        }
    """,
    }

    async with client_session.post(
        URL, json=data, headers=HEADERS, timeout=10
    ) as response:
        if response.status != 200:
            bot.logger.exception(
                f"fetch_daily_question: An error occurred while trying to get the "
                f"question from LeetCode. Error code ({response.status})"
            )
            return

        response_data = await response.json()

    try:
        title_slug = response_data["data"]["challenge"]["question"]["titleSlug"]

    except ValueError:
        bot.logger.exception(
            f"fetch_daily_question: failed to decode json. Error code ({response_data})"
        )
        return

    return title_slug


async def search_question(
    bot: "DiscordBot", client_session: aiohttp.ClientSession, text: str
) -> str | None:
    """
    Searches for a LeetCode question title slug based on the provided text.

    :param text: The text to search for in the question titles.

    :return: The title slug of the matched question, or None if no match is found or an
             error occurs.
    """
    payload = {
        "operationName": "problemsetQuestionList",
        "query": """
        query problemsetQuestionList(
            $categorySlug: String,
            $limit: Int,
            $skip: Int,
            $filters: QuestionListFilterInput
        ) {
            problemsetQuestionList: questionList(
                categorySlug: $categorySlug,
                limit: $limit,
                skip: $skip,
                filters: $filters
            ) {
                questions:
                    data {
                        titleSlug
                    }
                }
            }
        """,
        "variables": {
            "categorySlug": "",
            "skip": 0,
            "limit": 1,
            "filters": {"searchKeywords": text},
        },
    }

    async with client_session.post(
        URL, json=payload, headers=HEADERS, timeout=10
    ) as response:
        if response.status != 200:
            bot.logger.exception(
                f"search_question: An error occurred while trying to get the "
                f"question from LeetCode: {response.status}"
            )
            return

        response_data = await response.json()

    try:
        questions_matched_list = response_data["data"]["problemsetQuestionList"]

        if not questions_matched_list:
            return

        question_title_slug = questions_matched_list["questions"][0]["titleSlug"]

    except ValueError:
        bot.logger.exception(
            f"search_question: failed to decode json. Error code ({response_data})"
        )
        return

    return question_title_slug


async def fetch_question_info(
    bot: "DiscordBot", client_session: aiohttp.ClientSession, question_title_slug: str
) -> QuestionInfo | None:
    """
    Retrieves information about a LeetCode question based on its title slug.

    :param question_title_slug: The title slug of the LeetCode question.

    :return: Information about the LeetCode question, or None if an error occurs or no
             question is found.
    """
    bot.logger.info(f"Fetched question with title: {question_title_slug}")

    payload = {
        "operationName": "questionInfo",
        "query": """
        query questionInfo($titleSlug: String!) {
            question(titleSlug: $titleSlug) {
                questionFrontendId
                title
                difficulty
                content
                likes
                dislikes
                stats
                isPaidOnly
            }
        }
        """,
        "variables": {"titleSlug": question_title_slug},
    }

    async with client_session.post(
        URL, json=payload, headers=HEADERS, timeout=10
    ) as response:
        if response.status != 200:
            bot.logger.exception(
                f"fetch_question_info: An error occurred while trying to get the "
                f"question from LeetCode. Error code ({response.status})"
            )
            return

        response_data = await response.json()

    try:
        question = response_data["data"]["question"]

        question_id = question["questionFrontendId"]
        difficulty = question["difficulty"]
        title = question["title"]
        is_paid_only = question["isPaidOnly"]
        content = question["content"] if not is_paid_only else ""
        link = f"https://leetcode.com/problems/{question_title_slug}"

        stats = ast.literal_eval(question["stats"])
        total_accepted = stats["totalAccepted"]
        total_submission = stats["totalSubmission"]
        ac_rate = stats["acRate"]

    except ValueError:
        bot.logger.exception(
            f"fetch_question_info: failed to decode json. Error code ({response_data})",
        )
        return

    # Get question rating
    question_rating = None
    if not is_paid_only:
        rating = bot.ratings.fetch_rating(title)
        if rating:
            question_rating = int(rating)

    # Parse content for description, example one, and follow up
    description, example_one, follow_up = parse_content(content)

    return QuestionInfo(
        premium=is_paid_only,
        question_id=question_id,
        difficulty=difficulty,
        title=title,
        link=link,
        total_accepted=total_accepted,
        total_submission=total_submission,
        ac_rate=ac_rate,
        question_rating=question_rating,
        description=description,
        example_one=example_one,
        follow_up=follow_up,
    )


@backoff.on_exception(backoff.expo, RateLimitReached, logger=None)
async def fetch_problems_solved_and_rank(
    bot: "DiscordBot", client_session: aiohttp.ClientSession, leetcode_id: str
) -> UserStats | None:
    """
    Retrieves the statistics of problems solved and rank of a LeetCode user.

    :param client_session: The aiohttp ClientSession to use for making requests.
    :param leetcode_id: The LeetCode username.

    :return: Statistics of problems solved and rank of the user, or None if an error
    occurs.
    """
    payload = {
        "operationName": "getProblemsSolvedAndRank",
        "query": """query getProblemsSolvedAndRank($username: String!) {
            matchedUser(username: $username) {
                profile {
                    realName
                }
                submitStatsGlobal {
                    acSubmissionNum {
                        difficulty
                        count
                    }
                }
            }
        }
        """,
        "variables": {"username": leetcode_id},
    }

    async with semaphore:
        async with client_session.post(
            URL, json=payload, headers=HEADERS, timeout=10
        ) as response:
            match response.status:
                case 200:
                    response_data = await response.json()
                case 429:
                    bot.channel_logger.rate_limited()
                    raise RateLimitReached()
                case 403:
                    # Forbidden access
                    bot.logger.exception(
                        "fetch_problems_solved_and_rank: Error code: % s, LeetCode "
                        "username: % s",
                        response.status,
                        leetcode_id,
                    )
                    bot.channel_logger.forbidden()
                    return
                case _:
                    bot.logger.exception(
                        "fetch_problems_solved_and_rank: Error code: % s, LeetCode "
                        "username: % s",
                        response.status,
                        leetcode_id,
                    )
                    return

        # Add a small delay to avoid rate limits, using random to avoid patterns
        await asyncio.sleep(random())

    try:
        matched_user = response_data["data"]["matchedUser"]

        if not matched_user:
            return None

        real_name = matched_user["profile"]["realName"]
        submit_stats_global = matched_user["submitStatsGlobal"]
        ac_submission_num = submit_stats_global["acSubmissionNum"]

        easy_count = next(
            (
                item["count"]
                for item in ac_submission_num
                if item["difficulty"] == "Easy"
            ),
            0,
        )
        medium_count = next(
            (
                item["count"]
                for item in ac_submission_num
                if item["difficulty"] == "Medium"
            ),
            0,
        )
        hard_count = next(
            (
                item["count"]
                for item in ac_submission_num
                if item["difficulty"] == "Hard"
            ),
            0,
        )

    except ValueError:
        bot.logger.exception(
            f"fetch_problems_solved_and_rank: Failed to decode json for user "
            f"({leetcode_id}): {response_data}",
        )
        return

    return UserStats(
        real_name=real_name,
        submissions=Submissions(
            easy=easy_count,
            medium=medium_count,
            hard=hard_count,
            score=convert_to_score(
                easy=easy_count, medium=medium_count, hard=hard_count
            ),
        ),
    )