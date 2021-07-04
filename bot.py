import asyncio
import datetime
import logging
import re
import time
import uuid
from sys import exit

import discord
import pymongo
from discord.ext import commands
from discord_slash import SlashCommand, SlashContext, cog_ext
from discord_slash.model import SlashCommandOptionType, SlashCommandPermissionType
from discord_slash.utils.manage_commands import create_option, create_permission

import utils


LOG_FORMAT = '%(levelname)s [%(asctime)s]: %(message)s'
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)

try:
    import config

except ImportError:
    logging.critical('[Bot] config.py does not exist, you should make one from the example config')
    exit(1)

mclient = pymongo.MongoClient(config.mongoHost, username=config.mongoUser, password=config.mongoPass)

intents = discord.Intents(guilds=True, members=True, bans=True, messages=True, typing=True)
activityStatus = discord.Activity(type=discord.ActivityType.playing, name='DM to contact mods')
bot = commands.Bot(
    config.command_prefixes, fetch_offline_members=True, activity=activityStatus, case_insensitive=True, intents=intents
)
slash = SlashCommand(bot)
guildList = [config.guild]


class Mail(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.READY = False
        self.closeQueue = {}
        loop = bot.loop
        loop.create_task(slash.sync_all_commands(delete_from_unused_guilds=True))

    @cog_ext.cog_slash(
        name='close',
        guild_ids=guildList,
        description='Closes a modmail thread, optionally with a delay',
        permissions={config.guild: [create_permission(config.modRole, SlashCommandPermissionType.ROLE, True)]},
        options=[
            create_option(
                name='delay',
                description='The delay for the modmail to close, in 1w2d3h4m5s format',
                option_type=SlashCommandOptionType.STRING,
                required=False,
            )
        ],
    )
    async def _close(self, ctx: SlashContext, delay: str = None):
        db = mclient.modmail.logs
        doc = db.find_one({'channel_id': str(ctx.channel.id), 'open': True})

        if not doc:
            return await ctx.send('This is not a modmail channel!')

        if doc['_id'] in self.closeQueue:
            self.closeQueue[doc['_id']].cancel()

        if doc['ban_appeal']:
            app_info = await self.bot.application_info()
            if ctx.author.id != app_info.owner.id:
                return await ctx.send(
                    ':x: Only the bot owner can forcibly close a ban appeal thread. Use `!appeal accept` or `!appeal deny` instead'
                )

        if delay:
            try:
                delayDate = utils.resolve_duration(delay)
                delayTime = delayDate.timestamp() - datetime.datetime.utcnow().timestamp()

            except KeyError:
                return await ctx.send('Invalid duration')

            event_loop = self.bot.loop
            close_action = event_loop.call_later(
                delayTime, event_loop.create_task, utils._close_thread(self.bot, ctx, self.modLogs)
            )
            self.closeQueue[doc['_id']] = close_action
            return await ctx.send('Thread scheduled to be closed in ' + utils.humanize_duration(delayDate))

        await utils._close_thread(self.bot, ctx, self.modLogs)

    @cog_ext.cog_slash(
        name='reply',
        guild_ids=guildList,
        description='Replys to a modmail, with your username',
        permissions={config.guild: [create_permission(config.modRole, SlashCommandPermissionType.ROLE, True)]},
        options=[
            create_option(
                name='content',
                description='The message to send to the user',
                option_type=SlashCommandOptionType.STRING,
                required=True,
            )
        ],
    )
    async def _reply_user(self, ctx: SlashContext, *, content):
        """
        Reply to an open modmail thread
        """
        await self._reply(ctx, content)

    @cog_ext.cog_slash(
        name='areply',
        guild_ids=guildList,
        description='Replys to a modmail, anonymously',
        permissions={config.guild: [create_permission(config.modRole, SlashCommandPermissionType.ROLE, True)]},
        options=[
            create_option(
                name='content',
                description='The message to send to the user',
                option_type=SlashCommandOptionType.STRING,
                required=True,
            )
        ],
    )
    async def _reply_anon(self, ctx: SlashContext, *, content):
        """
        Reply to an open modmail thread anonymously
        """
        await self._reply(ctx, content, True)

    async def _reply(self, ctx, content, anonymous=False):
        db = mclient.modmail.logs
        doc = db.find_one({'channel_id': str(ctx.channel.id)})
        attachments = [x.url for x in ctx.message.attachments]

        if not content and not attachments:
            return await ctx.send('You must provide reply content, attachments, or both to use this command')

        if ctx.channel.category_id != config.category or not doc:  # No thread in channel, or not in modmail category
            return await ctx.send('Cannot send a reply here, this is not a modmail channel!')

        if content and len(content) > 1800:
            return await ctx.send(
                f'Wow there, thats a big reply. Please reduce it by at least {len(content) - 1800} characters'
            )

        if doc['_id'] in self.closeQueue.keys():  # Thread close was scheduled, cancel due to response
            self.closeQueue[doc['_id']].cancel()
            self.closeQueue.pop(doc['_id'], None)
            await ctx.channel.send('Thread closure has been canceled because a moderator has sent a message')

        recipient = doc['recipient']['id']
        member = ctx.guild.get_member(recipient)
        if not member:
            try:
                member = await ctx.guild.fetch_member(recipient)

            except:
                try:
                    member = await self.bot.get_guild(config.appealGuild).fetch_member(recipient)

                except:
                    return await ctx.send('There was an issue replying to this user, they may have left the server')

        try:
            await member.send(
                f'Reply from **{"Moderator" if anonymous else ctx.author}**: {content if content else ""}'
            )
            if attachments:
                await member.send('\n'.join(attachments))

        except:
            return await ctx.send(
                'There was an issue replying to this user, they may have left the server or disabled DMs'
            )

        db.update_one(
            {'_id': doc['_id']},
            {
                '$push': {
                    'messages': {
                        'timestamp': str(ctx.message.created_at),
                        'message_id': str(ctx.message.id),
                        'content': content if content else '',
                        'type': 'thread_message' if not anonymous else 'anonymous',
                        'author': {
                            'id': str(ctx.author.id),
                            'name': ctx.author.name,
                            'discriminator': ctx.author.discriminator,
                            'avatar_url': str(ctx.author.avatar_url_as(static_format='png', size=1024)),
                            'mod': True,
                        },
                        'attachments': attachments,
                    }
                }
            },
        )

        embed = discord.Embed(title='Moderator message', description=content, color=0x7ED321)
        if not anonymous:
            embed.set_author(name=f'{ctx.author} ({ctx.author.id})', icon_url=ctx.author.avatar_url)

        else:
            embed.title = '[ANON] Moderator message'
            embed.set_author(
                name=f'{ctx.author} ({ctx.author.id}) as r/NintendoSwitch',
                icon_url='https://cdn.mattbsg.xyz/rns/snoo.png',
            )

        if len(attachments) > 1:  # More than one attachment, use fields
            for x in range(len(attachments)):
                embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])

        elif attachments and re.search(
            r'\.(gif|jpe?g|tiff|png|webp|bmp)$', str(attachments[0]), re.IGNORECASE
        ):  # One attachment, image
            embed.set_image(url=attachments[0])

        elif attachments:  # Still have an attachment, but not an image
            embed.add_field(name=f'Attachment', value=attachments[0])

        await ctx.send(embed=embed)

    @cog_ext.cog_slash(
        name='open',
        guild_ids=guildList,
        description='Open a modmail thread with a user, using your username',
        permissions={config.guild: [create_permission(config.modRole, SlashCommandPermissionType.ROLE, True)]},
        options=[
            create_option(
                name='member',
                description='The user to start a thread with',
                option_type=SlashCommandOptionType.USER,
                required=True,
            )
        ],
    )
    async def _open_thread(self, ctx: SlashContext, member):
        """
        Open a modmail thread with a user
        """
        await ctx.defer()
        if mclient.modmail.logs.find_one({'recipient.id': str(member.id), 'open': True}):
            return await ctx.send(
                ':x: Unable to open modmail to user -- there is already a thread involving them currently open'
            )

        try:
            await utils._trigger_create_mod_thread(
                self.bot,
                member,
                ctx.message,
                open_type='moderator',
                moderator=ctx.author,
                anonymous=False,
            )

        except discord.Forbidden:
            return

        await ctx.send(f':white_check_mark: Modmail has been opened with {member}')

    @commands.has_any_role(config.modRole)
    @commands.group(name='s', aliases=['snippet', 'snippets'])
    async def _snippets(self, ctx, *args):
        db = mclient.modlog.snippets
        if not args:
            tagList = []
            for x in db.find({}):
                tagList.append(x['_id'])

            embed = discord.Embed(
                title='Snippet List',
                description='Here is a list of snippets you can repond with:\n\n' + ', '.join(tagList),
            )
            return await ctx.send(embed=embed)

        doc = db.find_one({'_id': args[0]})

    @commands.has_any_role(config.modRole)
    @commands.group(name='appeal', case_insensitive=True, invoke_without_command=True)
    async def _appeal(self, ctx):
        return await ctx.send_help(self._appeal)

    @commands.has_any_role(config.modRole)
    @_appeal.command(name='accept')
    async def _appeal_accept(self, ctx, *, reason):
        db = mclient.modmail.logs
        punsDB = mclient.bowser.puns
        userDB = mclient.bowser.users

        doc = db.find_one({'channel_id': str(ctx.channel.id), 'open': True, 'ban_appeal': True})
        user = await self.bot.fetch_user(int(doc['recipient']['id']))
        if not doc:
            return await ctx.send(':x: This is not a ban appeal channel!')

        punsDB.update_one({'user': user.id, 'type': 'ban', 'active': True}, {'$set': {'active': False}})
        punsDB.update_one({'user': user.id, 'type': 'appealdeny', 'active': True}, {'$set': {'active': False}})
        await ctx.guild.unban(user, reason=f'Ban appeal accepted by {ctx.author}')
        docID = str(uuid.uuid4())
        while punsDB.find_one({'_id': docID}):  # Uh oh, duplicate uuid generated
            docID = str(uuid.uuid4())

        punsDB.insert_one(
            {
                '_id': docID,
                'user': user.id,
                'moderator': ctx.author.id,
                'type': 'unban',
                'timestamp': int(time.time()),
                'reason': '[Ban appeal]' + reason,
                'expiry': None,
                'context': 'banappeal',
                'active': False,
            }
        )

        embed = discord.Embed(color=0x4A90E2, timestamp=datetime.datetime.utcnow())
        embed.set_author(name=f'Ban appeal accepted | {user}')
        embed.set_footer(text=docID)
        embed.add_field(name='User', value=user.mention, inline=True)
        embed.add_field(name='Moderator', value=f'{ctx.author.mention}', inline=True)
        embed.add_field(name='Reason', value=reason)
        await self.modLogs.send(embed=embed)

        try:
            await user.send(
                f'The moderators have decided to **lift your ban** on the {ctx.guild} Discord and your ban appeal thread has been closed. We kindly ask that you look over our server rules again upon your return. You may join back with this invite link: https://discord.gg/switch\nIf you are unable to join try reloading your client. Still can\'t join? You are likely IP banned on another account and you will need to appeal that ban as well.\n\nReason given by moderators:\n```{reason}```'
            )

        except:
            await self.bot.get_channel(config.adminChannel).send(
                f':warning: The ban appeal for {user} has been accepted by {ctx.author}, but I was unable to DM them the decision'
            )

        else:
            await self.bot.get_channel(config.adminChannel).send(
                f':white_check_mark: The ban appeal for {user} has been accepted by {ctx.author}'
            )

        finally:
            await utils._close_thread(self.bot, ctx, self.modLogs, dm=False, reason='[Appeal accepted] ' + reason)
            try:
                member = await self.bot.get_guild(config.appealGuild).fetch_member(user.id)
                await member.kick(reason='Accepted appeal')

            except:
                return

    @commands.has_any_role(config.modRole)
    @_appeal.command(name='deny')
    async def _appeal_deny(self, ctx, next_attempt, *, reason):
        db = mclient.modmail.logs
        punsDB = mclient.bowser.puns
        userDB = mclient.bowser.users

        doc = db.find_one({'channel_id': str(ctx.channel.id), 'open': True, 'ban_appeal': True})
        user = await self.bot.fetch_user(int(doc['recipient']['id']))
        if not doc:
            return await ctx.send(':x: This is not a ban appeal channel!')

        try:
            delayDate = utils.resolve_duration(next_attempt)

        except KeyError:
            return await ctx.send('Invalid duration')

        docID = str(uuid.uuid4())
        while punsDB.find_one({'_id': docID}):  # Uh oh, duplicate uuid generated
            docID = str(uuid.uuid4())

        punsDB.update_one({'user': user.id, 'type': 'appealdeny', 'active': True}, {'$set': {'active': False}})
        punsDB.insert_one(
            {
                '_id': docID,
                'user': user.id,
                'moderator': ctx.author.id,
                'type': 'appealdeny',
                'timestamp': int(time.time()),
                'reason': reason,
                'expiry': int(delayDate.timestamp()),
                'context': 'banappeal',
                'active': True,
            }
        )

        embed = discord.Embed(color=0x4A90E2, timestamp=datetime.datetime.utcnow())
        embed.set_author(name=f'Ban appeal denied | {user}')
        embed.set_footer(text=docID)
        embed.add_field(name='User', value=user.mention, inline=True)
        embed.add_field(name='Moderator', value=f'{ctx.author.mention}', inline=True)
        embed.add_field(name='Next appeal in', value=utils.humanize_duration(delayDate))
        embed.add_field(name='Reason', value=reason)
        await self.modLogs.send(embed=embed)

        try:
            await user.send(
                f'The moderators have decided to **uphold your ban** on the {ctx.guild} Discord and your ban appeal thread has been closed. You may appeal again after __{delayDate.strftime("%B %d, %Y at %I:%M%p UTC")} (approximately {utils.humanize_duration(delayDate)})__. In the meantime you have been kicked from the Ban Appeals server. When you are able to appeal again you may rejoin with this invite: {config.appealInvite}\n\nReason given by moderators:\n```{reason}```'
            )

        except:
            await self.bot.get_channel(config.adminChannel).send(
                f':warning: The ban appeal for {user} has been denied by {ctx.author}, but I was unable to DM them the decision'
            )

        else:
            await self.bot.get_channel(config.adminChannel).send(
                f':white_check_mark: The ban appeal for {user} has been denied by {ctx.author}'
            )

        finally:
            await utils._close_thread(self.bot, ctx, self.modLogs, dm=False, reason='[Appeal denied] ' + reason)
            try:
                member = await self.bot.get_guild(config.appealGuild).fetch_member(user.id)
                await member.kick(reason='Failed appeal')

            except:
                return

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
            pass  # Ignore

        elif isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(':x: Missing one or more aguments', delete_after=15)

        elif isinstance(error, commands.BadArgument):
            return await ctx.send(':x: Invalid argument provided', delete_after=15)

        elif isinstance(error, commands.CheckFailure):
            pass  # Ignore

        else:
            await ctx.send(':x: An unknown error occured, contact the developer if this continues to happen')
            raise error

    @commands.Cog.listener()
    async def on_typing(self, channel, user, when):
        if channel.type == discord.ChannelType.private:
            db = mclient.modmail.logs
            doc = db.find_one({'open': True, 'creator.id': str(user.id)})
            if doc:
                await self.bot.get_channel(int(doc['channel_id'])).trigger_typing()

    @commands.Cog.listener()
    async def on_member_ban(self, guild, member):
        db = mclient.modmail.logs
        thread = db.find_one({'recipient.id': str(member.id), 'open': True})
        if thread:
            message = (
                await self.bot.get_guild(int(thread['guild_id']))
                .get_channel(int(thread['channel_id']))
                .send(f'**{member}** has been banned from the server')
            )
            if not thread['ban_appeal']:
                ctx = await self.bot.get_context(message)
                await self._close.__call__(ctx, None)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        db = mclient.modmail.logs
        thread = db.find_one({'recipient.id': str(member.id), 'open': True})
        if thread:
            await self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id'])).send(
                f'**{member}** has rejoined the server, thread closure has been canceled'
            )
            if not thread['ban_appeal']:
                self.closeQueue[thread['_id']].cancel()
                self.closeQueue.pop(thread['_id'], None)

        if member.guild.id != config.appealGuild:  # Return if guild not the appeal server
            return

        guild = self.bot.get_guild(config.guild)
        try:
            await guild.fetch_ban(member)

        except discord.NotFound:
            guildMember = guild.get_member(member.id)
            if guildMember and guild.get_role(config.modRole) in guildMember.roles:
                return

            await member.send(
                'You have been automatically kicked from the /r/NintendoSwitch ban appeal server because you are not banned'
            )
            await member.kick(reason='Not banned on /r/NintendoSwitch')

        await utils._can_appeal(member)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await asyncio.sleep(10)  # Wait for ban to pass and thread to close in-case
        db = mclient.modmail.logs
        thread = db.find_one({'recipient.id': str(member.id), 'open': True})
        if thread:
            message = (
                await self.bot.get_guild(int(thread['guild_id']))
                .get_channel(int(thread['channel_id']))
                .send(f'**{member}** has left the server')
            )
            if not thread['ban_appeal']:
                ctx = await self.bot.get_context(message)
                await self._close.__call__(ctx, '4h')

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        attachments = [x.url for x in message.attachments]
        ctx = await self.bot.get_context(message)

        # Do something to check category, and add message to log
        if message.channel.type == discord.ChannelType.private:
            # User has sent a DM -- check
            db = mclient.modmail.logs
            thread = db.find_one({'recipient.id': str(message.author.id), 'open': True})
            if thread:
                if thread['_id'] in self.closeQueue.keys():  # Thread close was scheduled, cancel due to response
                    self.closeQueue[thread['_id']].cancel()
                    self.closeQueue.pop(thread['_id'], None)
                    await self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id'])).send(
                        'Thread closure has been canceled because the user has sent a message'
                    )

                description = message.content if message.content else None
                embed = discord.Embed(title='New message', description=description, color=0x32B6CE)
                embed.set_author(name=f'{message.author} ({message.author.id})', icon_url=message.author.avatar_url)
                embed.set_footer(text=f'{message.channel.id}/{message.id}')

                if len(attachments) > 1:  # More than one attachment, use fields
                    for x in range(len(attachments)):
                        embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])

                elif attachments and re.search(
                    r'\.(gif|jpe?g|tiff|png|webp|bmp)$', str(attachments[0]), re.IGNORECASE
                ):  # One attachment, image
                    embed.set_image(url=attachments[0])

                elif attachments:  # Still have an attachment, but not an image
                    embed.add_field(name=f'Attachment', value=attachments[0])

                await self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id'])).send(
                    embed=embed
                )
                db.update_one(
                    {'_id': thread['_id']},
                    {
                        '$push': {
                            'messages': {
                                'timestamp': str(message.created_at),
                                'message_id': str(message.id),
                                'content': message.content,
                                'type': 'thread_message',
                                'author': {
                                    'id': str(message.author.id),
                                    'name': message.author.name,
                                    'discriminator': message.author.discriminator,
                                    'avatar_url': str(message.author.avatar_url_as(static_format='png', size=1024)),
                                    'mod': False,
                                },
                                'attachments': attachments,
                            }
                        }
                    },
                )

            else:
                try:
                    thread = await utils._trigger_create_user_thread(self.bot, message.author, message, 'user')
                except RuntimeError as e:
                    logging.critical(
                        f'Exception thrown when calling utils._trigger_create_user_thread() with user {message.author.id}: %s',
                        e,
                    )
                    return

                # TODO: Don't duplicate message embed code based on new thread or just new message
                embed = discord.Embed(
                    title='New message', description=message.content if message.content else None, color=0x32B6CE
                )
                embed.set_author(name=f'{message.author} ({message.author.id})', icon_url=message.author.avatar_url)
                embed.set_footer(text=f'{message.channel.id}/{message.id}')

                if len(attachments) > 1:  # More than one attachment, use fields
                    for x in range(len(attachments)):
                        embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])

                elif attachments and re.search(
                    r'\.(gif|jpe?g|tiff|png|webp|bmp)$', str(attachments[0]), re.IGNORECASE
                ):  # One attachment, image
                    embed.set_image(url=attachments[0])

                elif attachments:  # Still have an attachment, but not an image
                    embed.add_field(name=f'Attachment', value=attachments[0])

                await thread.send(embed=embed)

            await message.add_reaction('✅')

        elif message.channel.category_id == config.category:
            db = mclient.modmail.logs
            doc = db.find_one({'channel_id': str(message.channel.id)})
            if doc:
                if not ctx.valid:  # Not an invoked command, mark as internal message
                    db.update_one(
                        {'_id': doc['_id']},
                        {
                            '$push': {
                                'messages': {
                                    'timestamp': str(message.created_at),
                                    'message_id': str(message.id),
                                    'content': message.content,
                                    'type': 'internal',
                                    'author': {
                                        'id': str(message.author.id),
                                        'name': message.author.name,
                                        'discriminator': message.author.discriminator,
                                        'avatar_url': str(message.author.avatar_url_as(static_format='png', size=1024)),
                                        'mod': True,
                                    },
                                    'attachments': attachments,
                                }
                            }
                        },
                    )

        elif (
            message.channel.type == discord.ChannelType.text
            and not ctx.guild.get_role(config.modRole) in message.author.roles
        ):
            # Regex to match pings with text content after, and none before. Groups: ping id, text content
            match = re.search(r'^\s*<@!?(\d{15,})>\s+(\S(?:.|\n|\r)+)$', message.content)

            if match and int(match.group(1)) == self.bot.user.id:
                await message.delete()

                db = mclient.modmail.logs
                thread = db.find_one({'recipient.id': str(message.author.id), 'open': True})
                content = match.group(2)

                embed = discord.Embed(title='New modmail mention', description=content, color=0x7289DA)
                embed.set_author(name=f'{message.author} ({message.author.id})', icon_url=message.author.avatar_url)
                embed.set_footer(text=f'{message.channel.id}/{message.id}')
                embed.add_field(
                    name=f'Mentioned in', value=f'<#{message.channel.id}> ([Jump to context]({message.jump_url}))'
                )

                try:
                    dm_embed = discord.Embed(description=content, color=0x7289DA)
                    dm_embed.set_author(name=f'{message.author}', icon_url=message.author.avatar_url)
                    dm_message = await message.author.send(
                        f'You mentioned {self.bot.user.name} in <#{message.channel.id}>', embed=dm_embed
                    )
                    await dm_message.add_reaction('✅')  # We don't need to do this but it matches the design language

                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    await self.bot.get_channel(config.adminChannel).send(
                        f'Cannot create thread for mention from <@{message.author.id}> (failed to send DM)', embed=embed
                    )
                    await message.channel.send(
                        f'<@{message.author.id}> You must open your DMs to use modmail threads. Moderators may still receive your mention.',
                        delete_after=30,
                    )

                else:
                    if thread:
                        channel = self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id']))

                        if (
                            thread['_id'] in self.closeQueue.keys()
                        ):  # Thread close was scheduled, cancel due to response
                            self.closeQueue[thread['_id']].cancel()
                            self.closeQueue.pop(thread['_id'], None)
                            await channel.send('Thread closure has been canceled because the user has sent a message')

                        db.update_one(
                            {'_id': thread['_id']},
                            {
                                '$push': {
                                    'messages': {
                                        'timestamp': str(message.created_at),
                                        'message_id': str(message.id),
                                        'content': message.content,
                                        'type': 'mention',
                                        'author': {
                                            'id': str(message.author.id),
                                            'name': message.author.name,
                                            'discriminator': message.author.discriminator,
                                            'avatar_url': str(
                                                message.author.avatar_url_as(static_format='png', size=1024)
                                            ),
                                            'mod': False,
                                        },
                                        'attachments': attachments,
                                        'channel': {'id': message.channel.id, 'name': message.channel.name},
                                    }
                                }
                            },
                        )

                    else:
                        channel = await utils._trigger_create_thread(
                            self.bot, message.author, message, 'user', is_mention=True
                        )

                    await channel.send(embed=embed)


bot.add_cog(Mail(bot))
bot.load_extension('jishaku')
bot.run(config.token)
