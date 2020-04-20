import asyncio
import logging
import datetime
import time
import typing

import pymongo
import discord
from discord.ext import commands

import config

mclient = pymongo.MongoClient(
	config.mongoHost,
	username=config.mongoUser,
	password=config.mongoPass
)
#activityStatus = discord.Activity(type=discord.ActivityType.playing, name='DM to contact mods')
bot = commands.Bot(['!', ',', '.'], fetch_offline_members=True)#, activity=activityStatus, case_insensitive=True)

LOG_FORMAT = '%(levelname)s [%(asctime)s]: %(message)s'
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)

class Mail(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.READY = False
        self.punNames = {
            'tier1': 'T1 Warn',
            'tier2': 'T2 Warn',
            'tier3': 'T3 Warn',
            'clear': 'Warn Clear',
            'mute': 'Mute',
            'unmute': 'Unmute',
            'kick': 'Kick',
            'ban': 'Ban',
            'unban': 'Unban',
            'blacklist': 'Blacklist ({})',
            'unblacklist': 'Unblacklist ({})',
            'note': 'User note'
        }

    async def _create_thread(self, channel, message, creator, recipient):
        db = mclient.modmail.logs
        _id = str(message.id) + '-' + str(int(time.time()))
        attachments = [x.url for x in message.attachments]

        db.insert_one({
            '_id': _id,
            'key': _id,
            'open': True,
            'created_at': str(message.created_at),
            'closed_at': None,
            'channel_id': str(channel.id),
            'guild_id': str(channel.guild.id),
            'bot_id': self.bot.user.id,
            'recipient': {
                'id': str(recipient.id),
                'name': recipient.name,
                'discriminator': recipient.discriminator,
                'avatar_url': str(recipient.avatar_url_as(static_format='png', size=1024)),
                'mod': False
            },
            'creator': {
                'id': str(creator.id),
                'name': creator.name,
                'discriminator': creator.discriminator,
                'avatar_url': str(creator.avatar_url_as(static_format='png', size=1024)),
                'mod': False
            },
            'closer': None,
            'messages': [
                {
                    'timestamp': str(message.created_at),
                    'message_id': str(message.id),
                    'content': message.content,
                    'type': 'thread_message',
                    'author': {
                        'id': str(message.author.id),
                        'name': message.author.name,
                        'discriminator': message.author.discriminator,
                        'avatar_url': str(message.author.avatar_url_as(static_format='png', size=1024)),
                        'mod': False
                    },
                    'attachments': attachments
                }
            ]
        })

        return _id

    async def _info(self, ctx, user: typing.Union[discord.Member, int]):
        inServer = True
        if type(user) == int:
            # User doesn't share the ctx server, fetch it instead
            dbUser = mclient.bowser.users.find_one({'_id': user})
            inServer = False

            user = await self.bot.fetch_user(user)

            if not dbUser:
                embed = discord.Embed(color=discord.Color(0x18EE1C), description=f'Fetched information about {user.mention} from the API because they are not in this server. There is little information to display as they have not been recorded joining the server before.')
                embed.set_author(name=f'{str(user)} | {user.id}', icon_url=user.avatar_url)
                embed.set_thumbnail(url=user.avatar_url)
                embed.add_field(name='Created', value=user.created_at.strftime('%B %d, %Y %H:%M:%S UTC'))
                return await ctx.send(embed=embed) # TODO: Return DB info if it exists as well

        else:
            dbUser = mclient.bowser.users.find_one({'_id': user.id})

        # Member object, loads of info to work with
        messages = mclient.bowser.messages.find({'author': user.id})
        msgCount = 0 if not messages else messages.count()

        desc = f'Fetched user {user.mention}' if inServer else f'Fetched information about previous member {user.mention} ' \
            'from the API because they are not in this server. ' \
            'Showing last known data from before they left.'

        embed = discord.Embed(color=discord.Color(0x18EE1C), description=desc)
        embed.set_author(name=f'{str(user)} | {user.id}', icon_url=user.avatar_url)
        embed.set_thumbnail(url=user.avatar_url)
        embed.add_field(name='Messages', value=str(msgCount), inline=True)
        if inServer:
            embed.add_field(name='Join date', value=user.joined_at.strftime('%B %d, %Y %H:%M:%S UTC'), inline=True)

        roleList = []
        if inServer:
            for role in reversed(user.roles):
                if role.id == user.guild.id:
                    continue

                roleList.append(role.name)

        else:
            roleList = dbUser['roles']
            
        if not roleList:
            # Empty; no roles
            roles = '*User has no roles*'

        else:
            if not inServer:
                tempList = []
                for x in reversed(roleList):
                    y = ctx.guild.get_role(x)
                    name = '*deleted role*' if not y else y.name
                    tempList.append(name)

                roleList = tempList

            roles = ', '.join(roleList)

        embed.add_field(name='Roles', value=roles, inline=False)

        lastMsg = 'N/a' if msgCount == 0 else datetime.datetime.utcfromtimestamp(messages.sort('timestamp',pymongo.DESCENDING)[0]['timestamp']).strftime('%B %d, %Y %H:%M:%S UTC')
        embed.add_field(name='Last message', value=lastMsg, inline=True)
        embed.add_field(name='Created', value=user.created_at.strftime('%B %d, %Y %H:%M:%S UTC'), inline=True)

        noteDocs = mclient.bowser.puns.find({'user': user.id, 'type': 'note'})
        if noteDocs.count():
            noteCnt = noteDocs.count()
            noteList = []
            for x in noteDocs.sort('timestamp', pymongo.DESCENDING):
                stamp = datetime.datetime.utcfromtimestamp(x['timestamp']).strftime('`[%m/%d/%y]`')
                noteList.append(f'{stamp}: {x["reason"]}')

            embed.add_field(name='User notes', value='View history to get more details on who issued the note.\n\n' + '\n'.join(noteList), inline=False)

        punishments = ''
        punsCol = mclient.bowser.puns.find({'user': user.id, 'type': {'$ne': 'note'}})
        if not punsCol.count():
            punishments = '__*No punishments on record*__'

        else:
            puns = 0
            for pun in punsCol.sort('timestamp', pymongo.DESCENDING):
                if puns >= 5:
                    break

                puns += 1
                stamp = datetime.datetime.utcfromtimestamp(pun['timestamp']).strftime('%m/%d/%y %H:%M:%S UTC')
                punType = self.punNames[pun['type']]
                if pun['type'] in ['clear', 'unmute', 'unban', 'unblacklist']:
                    punishments += f'- [{stamp}] {punType}\n'

                else:
                    punishments += f'+ [{stamp}] {punType}\n'

            punishments = f'Showing {puns}/{punsCol.count()} punishment entries. ' \
                f'For a full history including responsible moderator, active status, and more use `{ctx.prefix}history @{str(user)}` or `{ctx.prefix}history {user.id}`' \
                f'\n```diff\n{punishments}```'
        embed.add_field(name='Punishments', value=punishments, inline=False)
        return await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_ready(self):
        logging.info('[Bot] Ready')
        if not self.READY:
            self.READY = True

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.id not in [125233822760566784]: return
        # Do something to check category, and add message to log
        if message.channel.type == discord.ChannelType.private:
            # User has sent a DM -- check 
            db = mclient.modmail.logs
            thread = db.find_one({'recipient.id': str(message.author.id), 'open': True})
            attachments = [x.url for x in message.attachments]
            if thread:
                description = message.content if message.content else None
                embed = discord.Embed(title='New message', description=description, color=0x32B6CE)
                embed.set_author(name=str(message.author), icon_url=message.author.avatar_url)
                if attachments:
                    for x in range(len(attachments)):
                        embed.add_field(name=f'Attachment {x}', value=attachments[x])

                await self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id'])).send(embed=embed)
                db.update_one({'_id': thread['_id']}, {'$push': {'messages': {
                    'timestamp': str(message.created_at),
                    'message_id': str(message.id),
                    'content': message.content,
                    'type': 'thread_message',
                    'author': {
                        'id': str(message.author.id),
                        'name': message.author.name,
                        'discriminator': message.author.discriminator,
                        'avatar_url': str(message.author.avatar_url_as(static_format='png', size=1024)),
                        'mod': False
                    },
                    'attachments': attachments
                }}})

            else:
                guild = self.bot.get_guild(config.guild)
                category = guild.get_channel(config.category)
                channel = await category.create_text_channel(str(message.author), reason='New modmail opened')

                embed = discord.Embed(title='New modmail opened', color=0xE3CF59)
                embed.set_author(name=str(message.author), icon_url=message.author.avatar_url)

                docID = await self._create_thread(channel, message, message.author, message.author)

                punsDB = mclient.bowser.puns
                puns = punsDB.find({'user': message.author.id, 'active': True})
                description = f"A new modmail needs to be reviewed from {message.author.mention}. There are {db.find({'recipient.id': str(message.author.id)}).count()} previous threads involving this user. Archive link: {config.logUrl}{docID}"

                if puns.count():
                    punNames = {
                        'tier1': 'T1 Warn',
                        'tier2': 'T2 Warn',
                        'tier3': 'T3 Warn',
                        'clear': 'Warn Clear',
                        'mute': 'Mute',
                        'unmute': 'Unmute',
                        'kick': 'Kick',
                        'ban': 'Ban',
                        'unban': 'Unban',
                        'blacklist': 'Blacklist ({})',
                        'unblacklist': 'Unblacklist ({})',
                        'note': 'User note'
                    }
                    description += '\n\n__User has active punishments:__\n'
                    for pun in puns:
                        timestamp = datetime.datetime.utcfromtimestamp(pun['timestamp']).strftime('%b %d, %y at %H:%M UTC')
                        description += f"**{punNames[pun['type']]}** by <@{pun['moderator']}> on {timestamp}\n    ･ {pun['reason']}\n"

                embed.description = description
                mailMsg = await channel.send(embed=embed)
                await self._info(await self.bot.get_context(mailMsg), await guild.fetch_member(message.author.id))

                print('test')
                embed = discord.Embed(title='New message', description=message.content if message.content else None, color=0x32B6CE)
                embed.set_author(name=str(message.author), icon_url=message.author.avatar_url)
                if attachments:
                    for x in range(len(attachments)):
                        embed.add_field(name=f'Attachment {x}', value=attachments[x])

                await channel.send(embed=embed)


        #await commands.process_commands(message)

bot.add_cog(Mail(bot))
bot.load_extension('jishaku')
bot.run(config.token)
