import asyncio
import os
import re

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ---------------------------------------------------------------- config
BOOSTER_ROLE_ID = 1524142751481069580

# Roles that should also never be kicked (mods, admins, etc.)
PROTECTED_ROLE_IDS = {
    BOOSTER_ROLE_ID,
    # 123456789012345678,   # <- add your mod/admin role IDs here
}

SKIP_NEW_MEMBERS = True   # don't kick people who joined after the check was posted
KICK_DELAY = 1.1          # seconds between kicks (keeps you off the rate limit)

# DM sent to every kicked member before they're removed
DM_KICKED = True
DM_DELAY = 0.6
DM_MESSAGE = (
    "You were kicked from **{guild}** during an activity check.\n\n"
    "discord.gg/elitek if you were kicked in the mass kick process."
)

LOG_CHANNEL_ID = 0        # optional: channel ID to post the summary into (0 = off)

LINK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)
# -----------------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user} ({bot.user.id})")


def parse_link(link: str):
    match = LINK_RE.match(link.strip())
    if not match:
        return None
    return (int(x) for x in match.groups())


@tree.command(
    name="activity",
    description="Kick everyone who didn't react to an activity check. Boosters are spared.",
)
@app_commands.guild_only()
@app_commands.describe(
    link="Message link of the activity check (right click the message → Copy Message Link)",
    dry_run="Preview who would be kicked without actually kicking anyone",
)
async def activity(interaction: discord.Interaction, link: str, dry_run: bool = False):
    guild = interaction.guild

    parsed = parse_link(link)
    if not parsed:
        await interaction.response.send_message(
            "That's not a valid message link.", ephemeral=True
        )
        return
    guild_id, channel_id, message_id = parsed

    if guild_id != guild.id:
        await interaction.response.send_message(
            "That message is from a different server.", ephemeral=True
        )
        return

    if not guild.me.guild_permissions.kick_members:
        await interaction.response.send_message(
            "I'm missing the **Kick Members** permission.", ephemeral=True
        )
        return

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            await interaction.response.send_message(
                "Couldn't reach that channel.", ephemeral=True
            )
            return

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        await interaction.response.send_message(
            "Couldn't find that message.", ephemeral=True
        )
        return

    if not message.reactions:
        await interaction.response.send_message(
            "Nobody reacted to that message, so everyone would be kicked. "
            "Aborting — add a reaction first or re-run anyway.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    # Make sure the member cache is complete on large servers.
    try:
        await guild.chunk(cache=True)
    except discord.HTTPException:
        pass

    # Everyone who reacted, from every reaction, bots excluded.
    reacted: set[int] = set()
    for reaction in message.reactions:
        try:
            async for user in reaction.users():
                if not user.bot:
                    reacted.add(user.id)
        except discord.HTTPException:
            continue

    to_kick: list[discord.Member] = []
    skipped = {"bots": 0, "boosters": 0, "protected": 0, "new": 0}

    for member in guild.members:
        if member.bot:
            skipped["bots"] += 1
            continue
        if member.id in reacted or member.id == bot.user.id:
            continue

        role_ids = {role.id for role in member.roles}

        # Booster protection: role ID *or* an active boost.
        if BOOSTER_ROLE_ID in role_ids or member.premium_since is not None:
            skipped["boosters"] += 1
            continue

        if role_ids & PROTECTED_ROLE_IDS:
            skipped["protected"] += 1
            continue

        if (
            SKIP_NEW_MEMBERS
            and member.joined_at
            and message.created_at
            and member.joined_at > message.created_at
        ):
            skipped["new"] += 1
            continue

        to_kick.append(member)

    if not to_kick:
        await interaction.followup.send(
            f"Nothing to do — everyone already reacted. "
            f"({len(reacted)} reactions counted, {skipped['boosters']} boosters protected)"
        )
        return

    if dry_run:
        names = "\n".join(f"• {m} (`{m.id}`)" for m in to_kick[:50])
        more = f"\n…and {len(to_kick) - 50} more" if len(to_kick) > 50 else ""
        await interaction.followup.send(
            f"**Dry run** — would kick and DM **{len(to_kick)}** members:\n{names}{more}"
        )
        return

    kicked, failed = [], []
    dm_failed = 0

    for member in to_kick:
        # DM first, while we still share the guild — otherwise the DM can fail.
        if DM_KICKED:
            try:
                await member.send(DM_MESSAGE.format(guild=guild.name))
            except (discord.Forbidden, discord.HTTPException):
                dm_failed += 1
            await asyncio.sleep(DM_DELAY)

        try:
            await member.kick(reason=f"Missed activity check ({message.jump_url})")
            kicked.append(member)
        except discord.Forbidden:
            failed.append(f"{member} — role too high / missing permission")
        except discord.HTTPException as exc:
            failed.append(f"{member} — {exc.status}")
        await asyncio.sleep(KICK_DELAY)

    summary = (
        f"**Activity check complete**\n"
        f"Kicked: **{len(kicked)}**\n"
        f"DM delivered: **{len(kicked) - dm_failed}**\n"
        f"DM failed (closed DMs): **{dm_failed}**\n"
        f"Reacted / spared: **{len(reacted)}**\n"
        f"Boosters protected: **{skipped['boosters']}**\n"
        f"Joined after the check: {skipped['new']}\n"
        f"Other protected roles: {skipped['protected']}\n"
        f"Bots: {skipped['bots']}"
    )

    if failed:
        summary += f"\n\n**{len(failed)} failed** (fix role hierarchy and re-run):\n"
        summary += "\n".join(failed[:25])
        if len(failed) > 25:
            summary += f"\n…and {len(failed) - 25} more"

    summary = summary[:2000]
    await interaction.followup.send(summary)

    if LOG_CHANNEL_ID:
        log_channel = guild.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            try:
                await log_channel.send(f"{interaction.user.mention} ran an activity check\n\n{summary}")
            except discord.HTTPException:
                pass


if __name__ == "__main__":
    bot.run(TOKEN)