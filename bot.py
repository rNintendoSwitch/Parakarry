import asyncio
import logging
from sys import exit

import discord
import pymongo
from discord.ext import commands


LOG_FORMAT = '[Parakarry] %(levelname)s [%(asctime)s]: %(message)s'
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)

try:
    import config

except ImportError:
    logging.critical('[Bot] config.py does not exist, you should make one from the example config')
    exit(1)

mclient = pymongo.MongoClient(config.mongoHost, username=config.mongoUser, password=config.mongoPass)


class Parakarry(commands.Bot):
    def __init__(self):
        super().__init__(
            activity=discord.Activity(type=discord.ActivityType.playing, name='DM to contact mods'),
            case_insensitive=True,
            command_prefix=commands.when_mentioned,
            intents=discord.Intents(guilds=True, members=True, bans=True, messages=True, typing=True),
        )
        self.guildList = [config.guild]

    async def setup_hook(self):
        await self.load_extension('jishaku')
        await self.load_extension('cogs.modmail')

    async def on_ready(self):
        logging.info(f'Parakarry ModMail Bot - Now Logged in as {self.user} ({self.user.id})')
        logging.info('Chunking guilds members...')
        for g in self.guilds:
            await g.chunk(cache=True)
            logging.info(f'Chunked members for guild: {g.name} ({g.id})')

        logging.info('All guild members have been chunked')
        logging.info('Syncing Guild Commands...')
        for g in self.guilds:
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)

        logging.info('Guild commands synced')


async def _team_check(ctx):
    app_info = await ctx.bot.application_info()
    if app_info.team:
        devs = app_info.team.members

    else:
        devs = [app_info.owner]

    return ctx.author in devs


@Parakarry.command(name='sync')
@Parakarry.check(_team_check)
async def sync_commands(ctx):
    await Parakarry.tree.sync(guild=ctx.guild)


asyncio.run(Parakarry().start(config.token))
