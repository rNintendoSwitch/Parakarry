import asyncio
import logging
import datetime
import time
import typing
import re

import pymongo
import discord
from discord.ext import commands, tasks

import config

mclient = pymongo.MongoClient(
	config.mongoHost,
	username=config.mongoUser,
	password=config.mongoPass
)
activityStatus = discord.Activity(type=discord.ActivityType.playing, name='DM to contact mods')
bot = commands.Bot(['!', ',', 'p'], fetch_offline_members=True, activity=activityStatus, case_insensitive=True)

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
            'blacklist': 'Blacklist',
            'unblacklist': 'Unblacklist',
            'note': 'User note'
        }
        self.closeQueue = {}
        self._close_queue.start() #pylint: disable=no-member

    def cog_unload(self, bot):
        self._close_queue.stop() #pylint: disable=no-member

    def resolve_duration(self, data):
        '''
        Takes a raw input string formatted 1w1d1h1m1s (any order)
        and converts to timedelta
        Credit https://github.com/b1naryth1ef/rowboat via MIT license

        data: str
        '''
        timeUnits = {
            's': lambda v: v,
            'm': lambda v: v * 60,
            'h': lambda v: v * 60 * 60,
            'd': lambda v: v * 60 * 60 * 24,
            'w': lambda v: v * 60 * 60 * 24 * 7,
        }
        value = 0
        digits = ''

        try:
            int(data)
            raise KeyError('No time units provided')

        except ValueError:
            pass

        for char in data:
            if char.isdigit():
                digits += char
                continue

            if char not in timeUnits or not digits:
                raise KeyError('Time format not a valid entry')

            value += timeUnits[char](int(digits))
            digits = ''

        return datetime.datetime.utcnow() + datetime.timedelta(seconds=value + 1)

    def humanize_duration(self, duration):
        '''
        Takes a datetime object and returns a prettified
        weeks, days, hours, minutes, seconds string output
        Credit https://github.com/ThaTiemsz/jetski via MIT license

        duration: datetime.datetime
        '''
        now = datetime.datetime.utcnow()
        if isinstance(duration, datetime.timedelta):
            if duration.total_seconds() > 0:
                duration = datetime.datetime.today() + duration
            else:
                duration = datetime.datetime.utcnow() - datetime.timedelta(seconds=duration.total_seconds())
        diff_delta = duration - now
        diff = int(diff_delta.total_seconds())

        minutes, seconds = divmod(diff, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)
        weeks, days = divmod(days, 7)
        units = [weeks, days, hours, minutes, seconds]

        unit_strs = ['week', 'day', 'hour', 'minute', 'second']

        expires = []
        for x in range(0, 5):
            if units[x] == 0:
                continue

            else:
                if units[x] < -1 or units[x] > 1:
                    expires.append('{} {}s'.format(units[x], unit_strs[x]))

                else:
                    expires.append('{} {}'.format(units[x], unit_strs[x]))

        if not expires: return '0 seconds'
        return ', '.join(expires)

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
            'bot_id': str(self.bot.user.id),
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

    async def _user_trigger_create_thread(self, member, message):
        db = mclient.modmail.logs
        
        if not mclient.bowser.users.find_one({'_id': member.id})['modmail']: # Modmail restricted, deny thread creation
            return await member.send('Sorry, I cannot create a new modmail thread because you are currently blacklisted. ' \
                                            'You may DM a moderator if you still need to contact a Discord staff member. If you have ' \
                                            'questions or concerns about the blacklist please message the moderator that performed the action.')

        guild = self.bot.get_guild(config.guild)
        category = guild.get_channel(config.category)
        channel = await category.create_text_channel(f'{member.name}-{member.discriminator}', reason='New modmail opened')

        embed = discord.Embed(title='New modmail opened', color=0xE3CF59)
        embed.set_author(name=f'{member} ({member.id})', icon_url=member.avatar_url)

        threadCount = db.count_documents({'recipient.id': str(member.id)})
        docID = await self._create_thread(channel, message, member, member)

        punsDB = mclient.bowser.puns
        puns = punsDB.find({'user': member.id, 'active': True})
        punsCnt = punsDB.count_documents({'user': member.id, 'active': True})
        description = f"A new modmail needs to be reviewed from {member.mention}. There are {threadCount} previous threads involving this user. Archive link: {config.logUrl}{docID}"

        if punsCnt:
            description += '\n\n__User has active punishments:__\n'
            for pun in puns:
                timestamp = datetime.datetime.utcfromtimestamp(pun['timestamp']).strftime('%b %d, %y at %H:%M UTC')
                description += f"**{self.punNames[pun['type']]}** by <@{pun['moderator']}> on {timestamp}\n    ･ {pun['reason']}\n"

        embed.description = description
        mailMsg = await channel.send(embed=embed)
        await self._info(await self.bot.get_context(mailMsg), await guild.fetch_member(member.id))

        await member.send(f'Hi there!\nYou have opened a modmail thread with the chat moderators who oversee the **{guild.name}** Discord and they have received your message.\n\nI will send you a message when moderators respond to this thread. Every message you send to me while your thread is open will also be sent to the moderation team -- so you can message me anytime to add information or to reply to a moderator\'s message. You\'ll know your message has been sent when I react to your message with a ✅. \n\nPlease be patient for a response; if this is an urgent issue you may also ping the Chat-Mods with @Chat-Mods in a channel')
        
        return channel

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
        msgCount = 0 if not messages else mclient.bowser.messages.count_documents({'author': user.id})

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
        noteCnt = mclient.bowser.puns.count_documents({'user': user.id, 'type': 'note'})
        if noteCnt:
            noteList = []
            for x in noteDocs.sort('timestamp', pymongo.DESCENDING):
                stamp = datetime.datetime.utcfromtimestamp(x['timestamp']).strftime('`[%m/%d/%y]`')
                noteList.append(f'{stamp}: {x["reason"]}')

            embed.add_field(name='User notes', value='View history to get more details on who issued the note.\n\n' + '\n'.join(noteList), inline=False)

        punishments = ''
        punsCol = mclient.bowser.puns.find({'user': user.id, 'type': {'$ne': 'note'}})
        punsCnt = mclient.bowser.puns.count_documents({'user': user.id, 'type': {'$ne': 'note'}})
        if not punsCnt:
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

            punishments = f'Showing {puns}/{punsCnt} punishment entries. ' \
                f'For a full history including responsible moderator, active status, and more use `{ctx.prefix}history @{str(user)}` or `{ctx.prefix}history {user.id}`' \
                f'\n```diff\n{punishments}```'
        embed.add_field(name='Punishments', value=punishments, inline=False)
        return await ctx.send(embed=embed)

    @tasks.loop(seconds=10)
    async def _close_queue(self):
        queue = self.closeQueue.copy()
        for key, value in queue.items():
            if value['date'] < datetime.datetime.utcnow():
                await self._close.__call__(value['ctx'], delay=None) #pylint: disable=not-callable
                del self.closeQueue[key]

    @commands.has_any_role(config.modRole)
    @commands.command(name='close')
    async def _close(self, ctx, delay: typing.Optional[str]):
        db = mclient.modmail.logs
        doc = db.find_one({'channel_id': str(ctx.channel.id)})

        if not doc:
            return await ctx.send('This is not a modmail channel!')

        if delay:
            try:
                delayDate = self.resolve_duration(delay)

            except KeyError:
                return await ctx.send('Invalid duration')

            self.closeQueue[doc['_id']] = {'ctx': ctx, 'date': delayDate}
            return await ctx.send('Thread scheduled to be closed. Will be closed in ' + self.humanize_duration(delayDate))

        db.update_one({'_id': doc['_id']}, {
            '$set': {
                'open': False,
                'closed_at': str(ctx.message.created_at),
                'closer': {
                    'id': str(ctx.author.id),
                    'name': ctx.author.name,
                    'discriminator': ctx.author.discriminator,
                    'avatar_url': str(ctx.author.avatar_url_as(static_format='png', size=1024)),
                    'mod': True
                }
            }
        })

        await ctx.channel.delete(reason=f'Modmail closed by {ctx.author}')
        try:
            mailer = await ctx.guild.fetch_member(int(doc['recipient']['id']))
            await mailer.send('__Your modmail thread has been closed__. If you need to contact the chat-moderators again you may send me another DM to open a new modmail thread')

        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            await self.bot.get_channel(config.adminChannel).send(f'Failed to send DM to <@{doc["recipient"]["id"]}> for modmail closure. They have not been notified')


        user = doc['recipient']

        embed = discord.Embed(description=config.logUrl + doc['_id'], color=0xB8E986, timestamp=datetime.datetime.utcnow())
        embed.set_author(name=f'Mod mail closed | {user["name"]}#{user["discriminator"]} ({user["id"]})')
        embed.add_field(name='User', value=f'<@{user["id"]}>', inline=True)
        embed.add_field(name='Moderator', value=f'{ctx.author.mention}', inline=True)
        await self.modLogs.send(embed=embed)

    @commands.has_any_role(config.modRole)
    @commands.command(name='reply', aliases=['r'])
    async def _reply_user(self, ctx, *, content: typing.Optional[str]):
        await self._reply(ctx, content)

    @commands.has_any_role(config.modRole)
    @commands.command(name='areply', aliases=['ar'])
    async def _reply_anon(self, ctx, *, content: typing.Optional[str]):
        await self._reply(ctx, content, True)

    async def _reply(self, ctx, content, anonymous=False):
        db = mclient.modmail.logs
        doc = db.find_one({'channel_id': str(ctx.channel.id)})
        attachments = [x.url for x in ctx.message.attachments]

        if not content and not attachments:
            return await ctx.send('You must provide reply content, attachments, or both to use this command')

        if ctx.channel.category_id != config.category or not doc: # No thread in channel, or not in modmail category
            return await ctx.send('Cannot send a reply here, this is not a modmail channel!')

        if content and len(content) > 1800:
            return await ctx.send(f'Wow there, thats a big reply. Please reduce it by at least {len(content) - 1800} characters')

        if doc['_id'] in self.closeQueue.keys(): # Thread close was scheduled, cancel due to response
            del self.closeQueue[doc['_id']]
            await ctx.channel.send('Thread closure canceled due to moderator response')

        recipient = doc['recipient']['id']
        member = ctx.guild.get_member(recipient)
        if not member:
            try:
                member = await ctx.guild.fetch_member(recipient)

            except:
                return await ctx.send('There was an issue replying to this user, they may have left the server')

        try:
            await member.send(f'Reply from **{"Moderator" if anonymous else ctx.author}**: {content if content else ""}')
            if attachments:
                await member.send('\n'.join(attachments))

        except:
            return await ctx.send('There was an issue replying to this user, they may have left the server or disabled DMs')

        db.update_one({'_id': doc['_id']}, {'$push': {'messages': {
            'timestamp': str(ctx.message.created_at),
            'message_id': str(ctx.message.id),
            'content': content if content else '',
            'type': 'thread_message' if not anonymous else 'anonymous',
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
        if not anonymous:
            embed.set_author(name=f'{ctx.author} ({ctx.author.id})', icon_url=ctx.author.avatar_url)

        else:
            embed.title = '[ANON] Moderator message'
            embed.set_author(name=f'{ctx.author} ({ctx.author.id}) as r/NintendoSwitch', icon_url='https://cdn.mattbsg.xyz/rns/snoo.png')

        if len(attachments) > 1: # More than one attachment, use fields
            for x in range(len(attachments)):
                embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])

        elif attachments and re.search(r'\.(gif|jpe?g|tiff|png|webp|bmp)$', str(attachments[0]), re.IGNORECASE): # One attachment, image
            embed.set_image(url=attachments[0])

        elif attachments: # Still have an attachment, but not an image
            embed.add_field(name=f'Attachment', value=attachments[0])

        await ctx.send(embed=embed)

    @commands.has_any_role(config.modRole)
    @commands.group(name='s', aliases=['snippet', 'snippets'])
    async def _snippets(self, ctx, *args):
        db = mclient.modlog.snippets
        if not args:
            tagList = []
            for x in db.find({}):
                tagList.append(x['_id'])

            embed = discord.Embed(title='Snippet List', description='Here is a list of snippets you can repond with:\n\n' + ', '.join(tagList))
            return await ctx.send(embed=embed)

        doc = db.find_one({'_id': args[0]})

    @commands.Cog.listener()
    async def on_ready(self):
        logging.info('[Bot] Ready')
        if not self.READY:
            self.READY = True
            self.modLogs = self.bot.get_channel(config.modLog)
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
        ctx = await self.bot.get_context(message)

        # Do something to check category, and add message to log
        if message.channel.type == discord.ChannelType.private:
            # User has sent a DM -- check 
            db = mclient.modmail.logs
            thread = db.find_one({'recipient.id': str(message.author.id), 'open': True})
            if thread:
                if thread['_id'] in self.closeQueue.keys(): # Thread close was scheduled, cancel due to response
                    del self.closeQueue[thread['_id']]
                    await self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id'])).send('Thread closure canceled due to user response')

                description = message.content if message.content else None
                embed = discord.Embed(title='New message', description=description, color=0x32B6CE)
                embed.set_author(name=f'{message.author} ({message.author.id})', icon_url=message.author.avatar_url)
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
                thread = await self._user_trigger_create_thread(message.author, message)

                embed = discord.Embed(title='New message', description=message.content if message.content else None, color=0x32B6CE)
                embed.set_author(name=f'{message.author} ({message.author.id})', icon_url=message.author.avatar_url)
                if len(attachments) > 1: # More than one attachment, use fields
                    for x in range(len(attachments)):
                        embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])

                elif attachments and re.search(r'\.(gif|jpe?g|tiff|png|webp|bmp)$', str(attachments[0]), re.IGNORECASE): # One attachment, image
                    embed.set_image(url=attachments[0])

                elif attachments: # Still have an attachment, but not an image
                    embed.add_field(name=f'Attachment', value=attachments[0])

                await thread.send(embed=embed)

            await message.add_reaction('✅')

        elif message.channel.category_id == config.category:
            db = mclient.modmail.logs
            doc = db.find_one({'channel_id': str(message.channel.id)})
            if doc:
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

        elif message.content.startswith(f'<@!{self.bot.user.id}>') and message.channel.type == discord.ChannelType.text and not ctx.guild.get_role(config.modRole) in message.author.roles:
            db = mclient.modmail.logs
            thread = db.find_one({'recipient.id': str(message.author.id), 'open': True})

            content = message.content[len(f'<@!{self.bot.user.id}>'):].strip()
            if content:
                await message.delete()

                embed = discord.Embed(title='New modmail mention', description=content, color=0x7289DA)
                embed.set_author(name=f'{message.author} ({message.author.id})', icon_url=message.author.avatar_url)
                embed.add_field(name=f'Mentioned in', value=f'<#{message.channel.id}> ([Jump to context]({message.jump_url}))')

                try:
                    dm_embed = discord.Embed(description=content, color=0x7289DA)
                    dm_embed.set_author(name=f'{message.author}', icon_url=message.author.avatar_url)
                    dm_message = await message.author.send(f'You mentioned {self.bot.user.name} in <#{message.channel.id}>', embed=dm_embed)
                    await dm_message.add_reaction('✅') # We don't need to do this but it matches the design language

                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    await self.bot.get_channel(config.adminChannel).send(f'Cannot create thread for mention from <@{message.author.id}> (failed to send DM)', embed=embed)
                    ack = await message.channel.send(f'<@{message.author.id}> You must open your DMs to use modmail threads. Moderators may still receive your mention.')
                    await ack.delete(delay=30)

                else:
                    if thread: 
                        channel = self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id']))

                        if thread['_id'] in self.closeQueue.keys(): # Thread close was scheduled, cancel due to response
                                del self.closeQueue[thread['_id']]
                                await channel.send('Thread closure canceled due to user response')

                        db.update_one({'_id': thread['_id']}, {'$push': {'messages': { 
                            'timestamp': str(message.created_at),
                            'message_id': str(message.id),
                            'content': message.content,
                            'type': 'thread_message', # TODO: Different message type in logviewer
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
                        channel = await self._user_trigger_create_thread(message.author, message) # TODO: Different message type in logviewer

                    await channel.send(embed=embed)

bot.add_cog(Mail(bot))
bot.load_extension('jishaku')
bot.run(config.token)
