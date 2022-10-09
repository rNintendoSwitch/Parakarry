import asyncio
import logging
from datetime import datetime, timezone
from sys import exit

import discord
import pymongo
from discord import app_commands
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

    async def on_ready(self):
        logging.info(f'Parakarry ModMail Bot - Now Logged in as {self.user} ({self.user.id})')
        logging.info('Chunking guilds members...')
        for g in self.guilds:
            await g.chunk(cache=True)
            logging.info(f'Chunked members for guild: {g.name} ({g.id})')

        logging.info('All guild members have been chunked')
        logging.info('Syncing Guild Commands...')
        for g in self.guildList:
            await self.tree.sync(guild=discord.Object(id=g))

        logging.info('Guild commands synced')


asyncio.run(Parakarry().start(config.token))
