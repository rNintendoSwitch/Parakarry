import asyncio
import logging
import datetime
import time
import typing
import re

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
bot = commands.Bot(['!', ',', '.', 'p'], fetch_offline_members=True)#, activity=activityStatus, case_insensitive=True)

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

    @commands.has_any_role(config.modRole)
    @commands.command(name='reply', aliases=['r'])
    async def _reply(self, ctx, *, content):
        db = mclient.modmail.logs
        doc = db.find_one({'channel_id': str(ctx.channel.id)})

        if ctx.channel.category_id != config.category or not doc: # No thread in channel, or not in modmail category
            return await ctx.send('Cannot send a reply here, this is not a modmail channel!')

        if len(content) > 1800:
            return await ctx.send(f'Wow there, thats a big reply. Please reduce it by at least {len(content) - 1800} characters')

        recipient = doc['recipient']['id']
        attachments = [x.url for x in ctx.message.attachments]
        member = ctx.guild.get_member(recipient)
        if not member:
            try:
                member = await ctx.guild.fetch_member(recipient)

            except:
                return await ctx.send('There was an getting that member')

        try:
            await member.send(f'Reply from **{ctx.author}**. You can respond by replying to this message\n--------\n{content}')
            if attachments:
                await member.send('\n'.join(attachments))

        except:
            return await ctx.send('There was an issue sending that message to the user')

        db.update_one({'_id': doc['_id']}, {'$push': {'messages': {
            'timestamp': str(ctx.message.created_at),
            'message_id': str(ctx.message.id),
            'content': content,
            'type': 'thread_message',
            'author': {
                'id': str(ctx.author.id),
                'name': ctx.author.name,
                'discriminator': ctx.author.discriminator,
                'avatar_url': str(ctx.author.avatar_url_as(static_format='png', size=1024)),
                'mod': True
            },
            'attachments': attachments
        }}})

        embed = discord.Embed(title='Moderator message', description=content, color=0x7ED321)
        embed.set_author(name=str(ctx.author), icon_url=ctx.author.avatar_url)
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_ready(self):
        logging.info('[Bot] Ready')
        if not self.READY:
            self.READY = True
            self.bot.remove_command('help')

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.errors.CommandNotFound):
            pass # Ignore

        else:
            raise error

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot: return
        attachments = [x.url for x in message.attachments]

        # Do something to check category, and add message to log
        if message.channel.type == discord.ChannelType.private:
            # User has sent a DM -- check 
            db = mclient.modmail.logs
            thread = db.find_one({'recipient.id': str(message.author.id), 'open': True})
            if thread:
                description = message.content if message.content else None
                embed = discord.Embed(title='New message', description=description, color=0x32B6CE)
                embed.set_author(name=str(message.author), icon_url=message.author.avatar_url)
                if len(attachments) > 1: # More than one attachment, use fields
                    for x in range(len(attachments)):
                        embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])

                elif attachments and re.search(r'\.(gif|jpe?g|tiff|png|webp|bmp)$', str(attachments[0]), re.IGNORECASE): # One attachment, image
                    embed.set_image(url=attachments[0])

                elif attachments: # Still have an attachment, but not an image
                    embed.add_field(name=f'Attachment', value=attachments[0])

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

                threadCount = db.find({'recipient.id': str(message.author.id)}).count()
                docID = await self._create_thread(channel, message, message.author, message.author)

                punsDB = mclient.bowser.puns
                puns = punsDB.find({'user': message.author.id, 'active': True})
                description = f"A new modmail needs to be reviewed from {message.author.mention}. There are {threadCount} previous threads involving this user. Archive link: {config.logUrl}{docID}"

                if puns.count():
                    description += '\n\n__User has active punishments:__\n'
                    for pun in puns:
                        timestamp = datetime.datetime.utcfromtimestamp(pun['timestamp']).strftime('%b %d, %y at %H:%M UTC')
                        description += f"**{self.punNames[pun['type']]}** by <@{pun['moderator']}> on {timestamp}\n    ･ {pun['reason']}\n"

                embed.description = description
                mailMsg = await channel.send(embed=embed)
                await self._info(await self.bot.get_context(mailMsg), await guild.fetch_member(message.author.id))

                embed = discord.Embed(title='New message', description=message.content if message.content else None, color=0x32B6CE)
                embed.set_author(name=str(message.author), icon_url=message.author.avatar_url)
                if len(attachments) > 1: # More than one attachment, use fields
                    for x in range(len(attachments)):
                        embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])

                elif attachments and re.search(r'\.(gif|jpe?g|tiff|png|webp|bmp)$', str(attachments[0]), re.IGNORECASE): # One attachment, image
                    embed.set_image(url=attachments[0])

                elif attachments: # Still have an attachment, but not an image
                    embed.add_field(name=f'Attachment', value=attachments[0])

                await channel.send(embed=embed)
                await message.channel.send(f'Hi there!\nYou have opened a modmail thread with the chat moderators who oversee the **{guild.name}** Discord and they have received your message.\n\nI will send you a message when moderators respond to this thread. Every message you send to me while your thread is open will also be sent to the moderation team -- so you can message me anytime to add information or to reply to a moderator\'s message. You\'ll know your message has been sent when I react to your message with a ✅. \n\nPlease be patient for a response; if this is an urgent issue you may also ping the Chat-Mods with @Chat-Mods in a channel')

            await message.add_reaction('✅')

        elif message.channel.category_id == config.category:
            db = mclient.modmail.logs
            doc = db.find_one({'channel_id': str(message.channel.id)})
            if doc:
                ctx = await self.bot.get_context(message)
                if not ctx.valid: # Not an invoked command, mark as internal message
                    db.update_one({'_id': doc['_id']}, {'$push': {'messages': {
                        'timestamp': str(message.created_at),
                        'message_id': str(message.id),
                        'content': message.content,
                        'type': 'internal',
                        'author': {
                            'id': str(message.author.id),
                            'name': message.author.name,
                            'discriminator': message.author.discriminator,
                            'avatar_url': str(message.author.avatar_url_as(static_format='png', size=1024)),
                            'mod': True
                        },
                        'attachments': attachments
                    }}})

        if await self.bot.get_context(message).valid:
            await self.bot.process_commands(message)

bot.add_cog(Mail(bot))
bot.load_extension('jishaku')
bot.run(config.token)
