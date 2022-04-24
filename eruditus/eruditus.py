import logging
import io
import os
import re

from typing import Union

import discord
from discord.ext import tasks

from datetime import datetime, timedelta, timezone

import aiohttp

from binascii import hexlify
from buttons.workon import WorkonButton

from slash_commands.help import Help
from slash_commands.syscalls import Syscalls
from slash_commands.encoding import Encoding
from slash_commands.ctftime import CTFTime
from slash_commands.cipher import Cipher
from slash_commands.report import Report
from slash_commands.request import Request
from slash_commands.search import Search
from slash_commands.ctf import CTF

from lib.util import (
    sanitize_channel_name,
    setup_logger,
    truncate,
    get_local_time,
    derive_colour,
)
from lib.ctftime import (
    scrape_event_info,
    ctftime_date_to_datetime,
)
from lib.ctfd import get_scoreboard, pull_challenges, register_to_ctfd
from config import (
    CHALLENGE_COLLECTION,
    CTF_COLLECTION,
    CTFTIME_URL,
    DATE_FORMAT,
    DBNAME,
    GUILD_ID,
    MIN_PLAYERS,
    MONGO,
    USER_AGENT,
    TEAM_NAME,
    TEAM_EMAIL,
)


class Eruditus(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = discord.app_commands.CommandTree(self)

    async def create_ctf(self, name: str, live: bool = True) -> Union[dict, None]:
        """Create a CTF along with its channels and role.

        Args:
            name: CTF name.
            live: True if the CTF is ongoing.

        Returns:
            A dictionary containing information about the created CTF, or None if the
            CTF already exists.
        """
        # Check if the CTF already exists (case insensitive).
        if MONGO[DBNAME][CTF_COLLECTION].find_one(
            {"name": re.compile(f"^{re.escape(name.strip())}$", re.IGNORECASE)}
        ):
            return None

        # The bot is supposed to be part of a single guild.
        guild = self.get_guild(GUILD_ID)

        # Create the role if it didn't exist.
        role = discord.utils.get(guild.roles, name=name)
        if role is None:
            role = await guild.create_role(
                name=name,
                colour=derive_colour(name),
                mentionable=True,
            )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            role: discord.PermissionOverwrite(read_messages=True),
        }

        # Create the category channel if it didn't exist.
        category_channel = discord.utils.get(guild.categories, name=name)
        if category_channel is None:
            category_channel = await guild.create_category(
                name=f"{['⏰', '🔴'][live]} {name}",
                overwrites=overwrites,
            )

        await guild.create_text_channel("general", category=category_channel)
        await guild.create_voice_channel("general", category=category_channel)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
        }

        credentials_channel = await guild.create_text_channel(
            name="🔑-credentials", category=category_channel, overwrites=overwrites
        )
        notes_channel = await guild.create_text_channel(
            name="📝-notes", category=category_channel, overwrites=overwrites
        )
        bot_cmds_channel = await guild.create_text_channel(
            name="🤖-bot-cmds", category=category_channel, overwrites=overwrites
        )
        announcement_channel = await guild.create_text_channel(
            name="📣-announcements", category=category_channel, overwrites=overwrites
        )
        solves_channel = await guild.create_text_channel(
            name="🎉-solves", category=category_channel, overwrites=overwrites
        )
        scoreboard_channel = await guild.create_text_channel(
            name="📈-scoreboard", category=category_channel, overwrites=overwrites
        )

        ctf = {
            "name": name,
            "archived": False,
            "ended": False,
            "credentials": {
                "url": None,
                "username": None,
                "password": None,
            },
            "challenges": [],
            "guild_role": role.id,
            "guild_category": category_channel.id,
            "guild_channels": {
                "announcements": announcement_channel.id,
                "credentials": credentials_channel.id,
                "scoreboard": scoreboard_channel.id,
                "solves": solves_channel.id,
                "notes": notes_channel.id,
                "bot-cmds": bot_cmds_channel.id,
            },
        }
        MONGO[DBNAME][CTF_COLLECTION].insert_one(ctf)
        return ctf

    async def setup_hook(self) -> None:
        self.tree.add_command(Help())
        self.tree.add_command(Syscalls())
        self.tree.add_command(Encoding())
        self.tree.add_command(CTFTime())
        self.tree.add_command(Cipher())
        self.tree.add_command(Report())
        self.tree.add_command(Request())
        self.tree.add_command(Search())
        self.tree.add_command(CTF(), guild=discord.Object(GUILD_ID))

        self.create_upcoming_events.start()
        self.ctf_reminder.start()
        self.scoreboard_updater.start()
        self.challenge_puller.start()

    async def on_ready(self) -> None:
        for guild in self.guilds:
            logger.info(f"{self.user} connected to {guild}")

        # Sync global and guild specific commands.
        await self.tree.sync()
        await self.tree.sync(guild=discord.Object(id=GUILD_ID))

        await self.change_presence(activity=discord.Game(name="/help"))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        logger.info(f"{self.user} joined {guild}!")

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        logger.info(f"{self.user} left {guild}.")

    async def on_scheduled_event_update(
        self, before: discord.ScheduledEvent, after: discord.ScheduledEvent
    ) -> None:
        # The bot is supposed to be part of a single guild.
        guild = self.get_guild(GUILD_ID)

        # If an event started (status changes from scheduled to active).
        if (
            before.status == discord.EventStatus.scheduled
            and after.status == discord.EventStatus.active
        ):
            # Create the CTF if it wasn't already created.
            ctf = await self.create_ctf(after.name, live=True)
            if ctf is None:
                ctf = MONGO[DBNAME][CTF_COLLECTION].find_one(
                    {
                        "name": re.compile(
                            f"^{re.escape(after.name.strip())}$", re.IGNORECASE
                        )
                    }
                )

            # Give the CTF role to the interested people if they didn't get it yet.
            role = discord.utils.get(guild.roles, id=ctf["guild_role"])
            async for user in after.users():
                member = await guild.fetch_member(user.id)
                await member.add_roles(role)

            # Substitue the ⏰ in the category channel name with a 🔴 to say that
            # we're live.
            category_channel = discord.utils.get(
                guild.categories, id=ctf["guild_category"]
            )
            await category_channel.edit(name=category_channel.name.replace("⏰", "🔴"))

            # Ping all participants.
            ctf_general_channel = discord.utils.get(
                guild.text_channels,
                category_id=ctf["guild_category"],
                name="general",
            )
            await ctf_general_channel.send(
                f"{role.mention} has started!\nGet to work now ⚔️ 🔪 😠 🔨 ⚒️"
            )

        # If an event ended (status changes from active to ended/completed).
        elif (
            before.status == discord.EventStatus.active
            and after.status == discord.EventStatus.ended
        ):
            # Ping players that the CTF ended.
            ctf = MONGO[DBNAME][CTF_COLLECTION].find_one(
                {
                    "name": re.compile(
                        f"^{re.escape(after.name.strip())}$", re.IGNORECASE
                    )
                }
            )
            if ctf is None:
                return

            # Substitue the 🔴 in the category channel name with a 🏁 to say that
            # the CTF ended.
            category_channel = discord.utils.get(
                guild.categories, id=ctf["guild_category"]
            )
            await category_channel.edit(name=category_channel.name.replace("🔴", "🏁"))

            # Ping all participants.
            role = discord.utils.get(guild.roles, id=ctf["guild_role"])
            ctf_general_channel = discord.utils.get(
                guild.text_channels,
                category_id=ctf["guild_category"],
                name="general",
            )
            await ctf_general_channel.send(
                f"🏁 {role.mention} time is up! The CTF has ended."
            )

            # Update status of the CTF.
            MONGO[DBNAME][CTF_COLLECTION].update_one(
                {"_id": ctf["_id"]}, {"$set": {"ended": True}}
            )

    @tasks.loop(hours=1, reconnect=True)
    async def ctf_reminder(self) -> None:
        """Create a CTF for events starting soon and send a reminder."""
        # Wait until the bot's internal cache is ready.
        await self.wait_until_ready()

        # Timezone aware local time.
        local_time = get_local_time()

        # The bot is supposed to be part of a single guild.
        guild = self.get_guild(GUILD_ID)

        # Find a public channel where we can send our reminders.
        public_channel = None
        for channel in guild.text_channels:
            if channel.permissions_for(guild.default_role).read_messages:
                public_channel = channel
                if "general" in public_channel.name:
                    break

        for scheduled_event in await guild.fetch_scheduled_events():
            if scheduled_event.status != discord.EventStatus.scheduled:
                continue

            remaining_time = scheduled_event.start_time - local_time
            if remaining_time < timedelta(hours=1):
                # Ignore this event if not too many people are interested in it.
                if scheduled_event.user_count < MIN_PLAYERS:
                    if public_channel:
                        await public_channel.send(
                            f"🔔 CTF `{scheduled_event.name}` starting in "
                            f"`{str(remaining_time).split('.')[0]}`.\n"
                            f"This CTF was not created automatically because less than"
                            f" {MIN_PLAYERS} players were willing to participate.\n"
                            f"You can still create it manually using `/ctf createctf`."
                        )
                    continue

                # If a CTF is starting soon, we create it if it wasn't created yet.
                ctf = await self.create_ctf(scheduled_event.name, live=False)
                if ctf is None:
                    ctf = MONGO[DBNAME][CTF_COLLECTION].find_one(
                        {
                            "name": re.compile(
                                f"^{re.escape(scheduled_event.name.strip())}$",
                                re.IGNORECASE,
                            )
                        }
                    )

                # Register a team account if it's a CTFd platform.
                url = scheduled_event.location.split(" — ")[1]
                password = hexlify(os.urandom(32)).decode()
                result = await register_to_ctfd(
                    ctfd_base_url=url,
                    username=TEAM_NAME,
                    password=password,
                    email=TEAM_EMAIL,
                )
                if "success" in result:
                    # Add credentials.
                    ctf["credentials"]["url"] = url
                    ctf["credentials"]["username"] = TEAM_NAME
                    ctf["credentials"]["password"] = password

                    MONGO[DBNAME][CTF_COLLECTION].update_one(
                        {"_id": ctf["_id"]},
                        {"$set": {"credentials": ctf["credentials"]}},
                    )

                    creds_channel = discord.utils.get(
                        guild.text_channels, id=ctf["guild_channels"]["credentials"]
                    )
                    message = (
                        "```yaml\n"
                        f"CTF platform: {url}\n"
                        f"Username: {TEAM_NAME}\n"
                        f"Password: {password}\n"
                        "```"
                    )

                    await creds_channel.purge()
                    await creds_channel.send(message)

                # Add interested people automatically.
                role = discord.utils.get(guild.roles, id=ctf["guild_role"])
                async for user in scheduled_event.users():
                    member = await guild.fetch_member(user.id)
                    await member.add_roles(role)

                # Send a reminder that the CTF is starting soon.
                if public_channel:
                    await public_channel.send(
                        f"🔔 CTF `{ctf['name']}` starting in "
                        f"`{str(remaining_time).split('.')[0]}`.\n"
                        f"@here you can still use `/ctf join` to participate in case "
                        f"you forgot to hit the `Interested` button of the event."
                    )

    @tasks.loop(hours=3, reconnect=True)
    async def create_upcoming_events(self) -> None:
        """Create a Scheduled Event for each upcoming CTF competition."""
        # Wait until the bot's internal cache is ready.
        await self.wait_until_ready()

        # Timezone aware local time.
        local_time = get_local_time()

        # The bot is supposed to be part of a single guild.
        guild = self.get_guild(GUILD_ID)

        scheduled_events = {
            scheduled_event.name: scheduled_event.id
            for scheduled_event in await guild.fetch_scheduled_events()
        }
        async with aiohttp.request(
            method="get",
            url=f"{CTFTIME_URL}/api/v1/events/",
            params={"limit": 10},
            headers={"User-Agent": USER_AGENT},
        ) as response:
            if response.status == 200:
                for event in await response.json():
                    event_info = await scrape_event_info(event["id"])
                    if event_info is None:
                        continue

                    event_start = ctftime_date_to_datetime(event_info["start"])
                    event_end = ctftime_date_to_datetime(event_info["end"])

                    # If the event starts in more than a week, then it's too soon to
                    # schedule it, we ignore it for now.
                    if event_start > local_time + timedelta(weeks=1):
                        continue

                    async with aiohttp.request(
                        method="get",
                        url=event_info["logo"],
                        headers={"User-Agent": USER_AGENT},
                    ) as image:
                        raw_image = io.BytesIO(await image.read()).read()

                    event_description = (
                        f"{event_info['description']}\n\n"
                        f"👥 **Organizers**\n{', '.join(event_info['organizers'])}\n\n"
                        f"💰 **Prizes**\n{event_info['prizes']}\n\n"
                        f"⚙️ **Format**\n {event_info['location']} "
                        f"{event_info['format']}\n\n"
                        f"🎯 **Weight**\n{event_info['weight']}"
                    )
                    parameters = {
                        "name": event_info["name"],
                        "description": truncate(text=event_description, maxlen=1000),
                        "start_time": event_start,
                        "end_time": event_end,
                        "entity_type": discord.EntityType.external,
                        "image": raw_image,
                        "location": (
                            f"{CTFTIME_URL}/event/{event_info['id']}"
                            " — "
                            f"{event_info['website']}"
                        ),
                    }

                    # In case the event was already scheduled, we update it, otherwise
                    # we create a new event.
                    if event_info["name"] in scheduled_events:
                        scheduled_event = guild.get_scheduled_event(
                            scheduled_events[event_info["name"]]
                        )
                        scheduled_event = await scheduled_event.edit(**parameters)

                    else:
                        scheduled_event = await guild.create_scheduled_event(
                            **parameters
                        )

    @tasks.loop(minutes=2, reconnect=True)
    async def challenge_puller(self) -> None:
        """Periodically pull challenges for all running CTFs."""
        # Wait until the bot's internal cache is ready.
        await self.wait_until_ready()

        # The bot is supposed to be part of a single guild.
        guild = self.get_guild(GUILD_ID)

        for ctf in MONGO[DBNAME][CTF_COLLECTION].find({"ended": False}):
            url = ctf["credentials"]["url"]
            username = ctf["credentials"]["username"]
            password = ctf["credentials"]["password"]

            if url is None:
                return

            async for challenge in pull_challenges(url, username, password):
                # Avoid having duplicate categories when people mix up upper/lower case
                # or add unnecessary spaces at the beginning or the end.
                challenge["category"] = challenge["category"].title().strip()

                # Check if challenge was already created.
                if MONGO[DBNAME][CHALLENGE_COLLECTION].find_one(
                    {
                        "id": challenge["id"],
                        "name": re.compile(
                            f"^{re.escape(challenge['name'])}$", re.IGNORECASE
                        ),
                        "category": re.compile(
                            f"^{re.escape(challenge['category'])}$", re.IGNORECASE
                        ),
                    }
                ):
                    continue

                # Create a channel for the challenge and set its permissions.
                category_channel = discord.utils.get(
                    guild.categories, id=ctf["guild_category"]
                )
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False)
                }
                channel_name = sanitize_channel_name(
                    f"{challenge['category']}-{challenge['name']}"
                )
                challenge_channel = await guild.create_text_channel(
                    name=f"❌-{channel_name}",
                    category=category_channel,
                    overwrites=overwrites,
                )

                # Send challenge information in its respective channel.
                description = challenge["description"] or "No description."
                tags = ", ".join(challenge["tags"]) or "No tags."
                files = [
                    f"{ctf['credentials']['url'].strip('/')}{file}"
                    for file in challenge["files"]
                ]
                files = "\n- " + "\n- ".join(files) if files else "No files."
                embed = discord.Embed(
                    title=f"{challenge['name']} - {challenge['value']} points",
                    description=(
                        f"**Category:** {challenge['category']}\n"
                        f"**Description:** {description}\n"
                        f"**Files:** {files}\n"
                        f"**Tags:** {tags}"
                    ),
                    colour=discord.Colour.blue(),
                ).set_footer(
                    text=datetime.strftime(datetime.now(tz=timezone.utc), DATE_FORMAT)
                )
                message = await challenge_channel.send(embed=embed)
                await message.pin()

                # Announce that the challenge was added.
                announcements_channel = discord.utils.get(
                    guild.text_channels,
                    id=ctf["guild_channels"]["announcements"],
                )
                role = discord.utils.get(guild.roles, id=ctf["guild_role"])

                embed = discord.Embed(
                    title="🔔 New challenge created!",
                    description=(
                        f"**Challenge name:** {challenge['name']}\n"
                        f"**Category:** {challenge['category']}\n\n"
                        f"Use `/ctf workon {challenge['name']}` or the button to join."
                        f"\n{role.mention}"
                    ),
                    colour=discord.Colour.dark_gold(),
                ).set_footer(
                    text=datetime.strftime(datetime.now(tz=timezone.utc), DATE_FORMAT)
                )
                announcement = await announcements_channel.send(
                    embed=embed, view=WorkonButton(name=challenge["name"])
                )

                # Add challenge to the database.
                challenge_object_id = (
                    MONGO[DBNAME][CHALLENGE_COLLECTION]
                    .insert_one(
                        {
                            "id": challenge["id"],
                            "name": challenge["name"],
                            "category": challenge["category"],
                            "channel": challenge_channel.id,
                            "solved": False,
                            "blooded": False,
                            "players": [],
                            "announcement": announcement.id,
                            "solve_time": None,
                            "solve_announcement": None,
                        }
                    )
                    .inserted_id
                )

                # Add reference to the newly created challenge.
                ctf["challenges"].append(challenge_object_id)
                MONGO[DBNAME][CTF_COLLECTION].update_one(
                    {"_id": ctf["_id"]}, {"$set": {"challenges": ctf["challenges"]}}
                )

    @tasks.loop(minutes=1, reconnect=True)
    async def scoreboard_updater(self) -> None:
        """Periodically update scoreboard for all running CTFs."""
        # Wait until the bot's internal cache is ready.
        await self.wait_until_ready()

        # The bot is supposed to be part of a single guild.
        guild = self.get_guild(GUILD_ID)

        for ctf in MONGO[DBNAME][CTF_COLLECTION].find({"ended": False}):
            ctfd_url = ctf["credentials"]["url"]
            username = ctf["credentials"]["username"]
            password = ctf["credentials"]["password"]

            if ctfd_url is None:
                continue

            try:
                teams = await get_scoreboard(ctfd_url, username, password)
            except aiohttp.client_exceptions.InvalidURL:
                continue

            if not teams:
                continue

            name_field_width = max(len(team["name"]) for team in teams) + 10
            scoreboard = ""
            for rank, team in enumerate(teams, start=1):
                scoreboard += (
                    f"{['-', '+'][team['name'] == username]} "
                    f"{rank:<10}{team['name']:<{name_field_width}}"
                    f"{round(team['score'], 4)}\n"
                )

            if scoreboard:
                message = (
                    f"**Scoreboard as of "
                    f"{datetime.strftime(datetime.now(tz=timezone.utc), DATE_FORMAT)}"
                    "**"
                    "```diff\n"
                    f"  {'Rank':<10}{'Team':<{name_field_width}}{'Score'}\n"
                    f"{scoreboard}"
                    "```"
                )
            else:
                message = "No solves yet, or platform isn't CTFd."

            # Update scoreboard in the scoreboard channel.
            scoreboard_channel = discord.utils.get(
                guild.text_channels, id=ctf["guild_channels"]["scoreboard"]
            )
            async for last_message in scoreboard_channel.history(limit=1):
                await last_message.edit(content=message)
                break
            else:
                await scoreboard_channel.send(message)


if __name__ == "__main__":
    logger = setup_logger(logging.INFO)
    client = Eruditus()
    client.run(os.getenv("DISCORD_TOKEN"))
