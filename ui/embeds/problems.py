from typing import TYPE_CHECKING

import discord

from constants import Difficulty
from ui.embeds.common import failure_embed
from ui.embeds.neetcode import neetcode_embed
from utils.problems import (
    fetch_daily_question,
    fetch_question_info,
    fetch_random_question,
    search_question,
    search_question_id
)

if TYPE_CHECKING:
    # To prevent circular imports
    from bot import DiscordBot


async def daily_question_embed(bot: "DiscordBot") -> discord.Embed:
    question_title = await fetch_daily_question(bot)

    if not question_title:
        return question_error_embed()

    embed = await question_embed(bot, question_title)
    return embed


async def search_question_embed(bot: "DiscordBot", search_text: str) -> discord.Embed:
    question_title = await search_question(bot, search_text)

    if not question_title:
        return question_error_embed()

    embed = await question_embed(bot, question_title)
    return embed

# Neetcode Search by id
async def search_question_embed_id(bot: "DiscordBot", search_text: str) -> discord.Embed:
    question_id, question_title = await search_question_id(bot, search_text)

    if not question_id:
        return question_error_embed()

    embed = await neetcode_embed(bot, question_id, question_title)
    return embed

async def random_question_embed(
    bot: "DiscordBot", difficulty: Difficulty
) -> discord.Embed:
    question_title = await fetch_random_question(bot, difficulty)

    if not question_title:
        return question_error_embed()

    embed = await question_embed(bot, question_title)
    return embed


async def question_embed(bot: "DiscordBot", question_title: str) -> discord.Embed:
    info = await fetch_question_info(bot, question_title)

    if not info:
        return question_error_embed()

    colour_dict = {
        "Easy": discord.Colour.green(),
        "Medium": discord.Colour.orange(),
        "Hard": discord.Colour.red(),
    }
    colour = (
        colour_dict[info.difficulty]
        if info.difficulty in colour_dict
        else discord.Colour.blue()
    )

    if info.premium:
        return premium_question_embed(info.question_id, info.title, info.link, colour)

    embed = discord.Embed(
        title=f"{info.question_id}. {info.title}",
        url=info.link,
        description=info.description,
        colour=colour,
    )

    # TODO: handle descriptions/examples larger than the limit
    # TODO: https://www.pythondiscord.com/pages/guides/python-guides/discord-embed-limits/
    embed.add_field(name="Example 1: ", value=info.example_one, inline=False)

    if info.follow_up:
        embed.add_field(name="Follow up: ", value=info.follow_up, inline=False)

    embed.add_field(name="Difficulty: ", value=info.difficulty, inline=True)

    if info.question_rating is not None:
        embed.add_field(
            name="Zerotrac Rating: ", value=f"||{info.question_rating}||", inline=True
        )

    embed.set_footer(
        text=f"Accepted: {info.total_accepted}  |  Submissions: "
        f"{info.total_submission}  |  Acceptance Rate: {info.ac_rate}"
    )

    return embed


def premium_question_embed(
    question_id, title, link, colour: discord.Colour
) -> discord.Embed:
    return discord.Embed(
        title=f"{question_id}. {title}",
        description="Premium Question",
        url=link,
        colour=colour,
    )


def question_error_embed() -> discord.Embed:
    return failure_embed(
        title="There was a problem retrieving the question. Please try again later."
    )
