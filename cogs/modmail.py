import asyncio
import logging
import re
import time
import typing
import uuid
from code import interact
from datetime import datetime, timezone
from sys import exit

import discord
import pymongo
from discord import app_commands
from discord.ext import commands

import cogs.utils as utils
import exceptions


try:
    import config

except ImportError:
    logging.critical('[Bot] config.py does not exist, you should make one from the example config')
    exit(1)

mclient = pymongo.MongoClient(config.mongoHost, username=config.mongoUser, password=config.mongoPass)
guildList = [config.guild]


class Mail(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.closeQueue = {}

        self.openContextMenu = app_commands.ContextMenu(name='Open a Modmail', callback=self._open_context)
        self.reportContextMenu = app_commands.ContextMenu(name='Report this Message', callback=self._message_report)
        self.bot.tree.add_command(self.openContextMenu, guild=discord.Object(id=config.guild))
        self.bot.tree.add_command(self.reportContextMenu, guild=discord.Object(id=config.guild))

    @app_commands.command(name='close', description='Closes a modmail thread, optionally with a delay')
    @app_commands.describe(delay='The delay for the modmail to close, in 1w2d3h4m5s format')
    @app_commands.guilds(discord.Object(id=config.guild))
    @app_commands.default_permissions(view_audit_log=True)
    async def _close(self, interaction: discord.Interaction, delay: typing.Optional[str]):
        message, ephemeral = await self._close_generic(interaction.user, interaction.guild, interaction.channel, delay)

        if message:
            await interaction.response.send_message(message, ephemeral=ephemeral)

    async def _close_generic(self, user, guild, channel, delay):
        db = mclient.modmail.logs
        doc = db.find_one({'channel_id': str(channel.id), 'open': True})

        if not doc:
            return 'This is not a modmail channel!', True

        if doc['_id'] in self.closeQueue:
            self.closeQueue[doc['_id']].cancel()

        if doc['ban_appeal']:
            return (
                ':x: Ban appeals cannot be closed with the `/close` command. Use `/appeal accept` or `/appeal deny` instead',
                False,
            )

        if delay:
            try:
                delayDate = utils.resolve_duration(delay)
                delayTime = delayDate.timestamp() - datetime.now(tz=timezone.utc).timestamp()

            except KeyError:
                return 'Invalid duration', False

            event_loop = self.bot.loop
            close_action = event_loop.call_later(
                delayTime,
                event_loop.create_task,
                utils._close_thread(self.bot, user, guild, channel, self.bot.get_channel(config.modLog)),
            )
            self.closeQueue[doc['_id']] = close_action
            return f'Thread scheduled to be closed <t:{int(delayDate.timestamp())}:R>', False

        await utils._close_thread(self.bot, user, guild, channel, self.bot.get_channel(config.modLog))
        return None, False

    @app_commands.command(name='reply', description='Replys to a modmail, with your username')
    @app_commands.describe(content='The message to send to the user')
    @app_commands.describe(attachment='An image or file to send to the user')
    @app_commands.guilds(discord.Object(id=config.guild))
    @app_commands.default_permissions(view_audit_log=True)
    async def _reply_user(
        self,
        interaction: discord.Interaction,
        content: app_commands.Range[str, None, 1800],
        attachment: typing.Optional[discord.Attachment],
    ):
        await self._reply(interaction, content, attachment)

    @app_commands.command(name='areply', description='Replys to a modmail, anonymously')
    @app_commands.describe(content='The message to send to the user')
    @app_commands.describe(attachment='An image or file to send to the user')
    @app_commands.guilds(discord.Object(id=config.guild))
    @app_commands.default_permissions(view_audit_log=True)
    async def _reply_anon(
        self,
        interaction: discord.Interaction,
        content: app_commands.Range[str, None, 1800],
        attachment: typing.Optional[discord.Attachment],
    ):
        await self._reply(interaction, content, attachment, True)

    async def _reply(self, interaction: discord.Interaction, content, attachment, anonymous=False):
        db = mclient.modmail.logs
        doc = db.find_one({'channel_id': str(interaction.channel.id)})

        if (
            interaction.channel.category_id != config.category or not doc
        ):  # No thread in channel, or not in modmail category
            return await interaction.response.send_message(
                'Cannot send a reply here, this is not a modmail channel!', ephemeral=True
            )

        if doc['_id'] in self.closeQueue.keys():  # Thread close was scheduled, cancel due to response
            self.closeQueue[doc['_id']].cancel()
            self.closeQueue.pop(doc['_id'], None)
            await interaction.channel.send('Thread closure has been canceled because a moderator has sent a message')

        recipient = doc['recipient']['id']
        member = interaction.guild.get_member(recipient)
        if not member:
            try:
                member = await interaction.guild.fetch_member(recipient)

            except:
                try:
                    member = await self.bot.get_guild(config.appealGuild).fetch_member(recipient)

                except:
                    return await interaction.response.send_message(
                        'There was an issue replying to this user, they may have left the server'
                    )

        try:
            if anonymous:
                responsibleModerator = 'a **Moderator**'

            elif interaction.guild.owner == interaction.user:
                responsibleModerator = f'*(Server Owner)* **{interaction.user}**'

            elif self.bot.get_guild(config.guild).get_role(config.leadModRole) in interaction.user.roles:
                responsibleModerator = f'*(Lead Moderator)* **{interaction.user}**'

            else:
                responsibleModerator = f'*(Moderator)* **{interaction.user}**'

            replyText = f'Reply from {responsibleModerator}: {content if content else ""}'

            if attachment:
                replyMessage = await member.send(replyText, file=await attachment.to_file())

            else:
                replyMessage = await member.send(replyText)

        except discord.errors.Forbidden:
            return await interaction.response.send_message(
                'There was an issue replying to this user, they may have left the server or disabled DMs'
            )

        embed = discord.Embed(title='Moderator message', description=content, color=0x7ED321)
        if not anonymous:
            embed.set_author(
                name=f'{interaction.user} ({interaction.user.id})', icon_url=interaction.user.display_avatar.url
            )

        else:
            embed.title = '[ANON] Moderator message'
            embed.set_author(
                name=f'{interaction.user} ({interaction.user.id}) as r/NintendoSwitch',
                icon_url='https://cdn.mattbsg.xyz/rns/snoo.png',
            )

        if attachment and re.search(
            r'\.(gif|jpe?g|tiff|png|webp)(\?[a-zA-Z0-9#-_]*)?$', str(attachment), re.IGNORECASE
        ):  # One attachment, image
            embed.set_image(url=replyMessage.attachments[0].url)

        elif attachment:  # Still have an attachment, but not an image
            embed.add_field(name=f'Attachment', value=replyMessage.attachments[0].url)

        await interaction.response.send_message(embed=embed)
        mailMsg = await interaction.original_response()

        db.update_one(
            {'_id': doc['_id']},
            {
                '$push': {
                    'messages': {
                        'timestamp': str(datetime.now(tz=timezone.utc).isoformat(sep=' ')),
                        'message_id': str(mailMsg.id),
                        'content': content if content else '',
                        'type': 'thread_message' if not anonymous else 'anonymous',
                        'author': {
                            'id': str(interaction.user.id),
                            'name': interaction.user.name,
                            'discriminator': interaction.user.discriminator,
                            'avatar_url': str(
                                interaction.user.display_avatar.with_static_format('png').with_size(1024)
                            ),
                            'mod': True,
                        },
                        'attachments': [replyMessage.attachments[0].url] if replyMessage.attachments else [],
                    }
                }
            },
        )

    @app_commands.command(name='open', description='Open a modmail thread with a user')
    @app_commands.describe(member='The user to start a thread with')
    @app_commands.guilds(discord.Object(id=config.guild))
    @app_commands.default_permissions(view_audit_log=True)
    async def _open_slash(self, interaction: discord.Interaction, member: discord.Member):
        """
        Open a modmail thread with a user
        """

        await interaction.response.defer()
        await self._open_thread(interaction, member)

    async def _open_context(self, interaction: discord.Interaction, member: discord.Member):
        """
        Open a modmail thread with a user
        """

        await self._open_thread(interaction, member)

    async def _open_thread(self, interaction: discord.Interaction, member: discord.Member):
        """
        Open a modmail thread with a user
        """

        if member.bot:
            return await interaction.followup.send(':x: Modmail threads cannot be opened with bot accounts')

        if mclient.modmail.logs.find_one({'recipient.id': str(member.id), 'open': True}):
            return await interaction.followup.send(
                ':x: Unable to open modmail to user -- there is already a thread involving them currently open'
            )

        try:
            await utils._trigger_create_mod_thread(self.bot, interaction.guild, member, interaction.user)

        except discord.Forbidden:
            return await interaction.followup.send(
                f':x: Failed to DM {member.mention}, this could be because their DMs are disabled or they have blocked me. Thread open action canceled'
            )

        await interaction.followup.send(f':white_check_mark: Modmail has been opened with {member}')

    async def _message_report(self, interaction: discord.Interaction, message: discord.Message):
        await interaction.response.defer(ephemeral=True)
        if message.author.bot:
            return await interaction.followup.send(':x: You cannot report messages sent by bots', ephemeral=True)

        try:
            dmOpened = await self._user_create_thread(message, interaction)

        except exceptions.ModmailBlacklisted:
            return await interaction.followup.send(
                'Sorry, I cannot create a message report because you are currently blacklisted. '
                'You may DM a moderator if you still need to contact a Discord staff member.',
                ephemeral=True,
            )

        if dmOpened:
            return await interaction.followup.send(
                'Thank you for the report, a moderator will review it soon. If you have any additional information to add you can send me a direct message and I will forward it to the moderators.',
                ephemeral=True,
            )

        else:
            return await interaction.followup.send(
                'Thank you for the report. Since your direct messages are disabled, you will not be able to receive any followup messages from moderators. If you do wish to be notified about updates to this report, please turn them on.',
                ephemeral=True,
            )

    @app_commands.guilds(discord.Object(id=config.guild))
    class GuildGroupCommand(app_commands.Group):
        pass

    appeal_group = GuildGroupCommand(
        name='appeal',
        description='Make a decision to accept or deny a ban appeal',
        default_permissions=discord.Permissions(view_audit_log=True),
    )

    @appeal_group.command(name='accept', description='Accept a user\'s ban appeal')
    @app_commands.describe(reason='Why are you accepting this appeal?')
    async def _appeal_accept(self, interaction: discord.Interaction, reason: app_commands.Range[str, None, 990]):
        db = mclient.modmail.logs
        punsDB = mclient.bowser.puns
        userDB = mclient.bowser.users

        doc = db.find_one({'channel_id': str(interaction.channel.id), 'open': True, 'ban_appeal': True})
        if not doc:
            return await interaction.response.send_message(':x: This is not a ban appeal channel!', ephemeral=True)

        user = await self.bot.fetch_user(int(doc['recipient']['id']))
        punsDB.update_one({'user': user.id, 'type': 'ban', 'active': True}, {'$set': {'active': False}})
        punsDB.update_one({'user': user.id, 'type': 'appealdeny', 'active': True}, {'$set': {'active': False}})
        await interaction.guild.unban(user, reason=f'Ban appeal accepted by {interaction.user}')
        docID = str(uuid.uuid4())
        while punsDB.find_one({'_id': docID}):  # Uh oh, duplicate uuid generated
            docID = str(uuid.uuid4())

        punsDB.insert_one(
            {
                '_id': docID,
                'user': user.id,
                'moderator': interaction.user.id,
                'type': 'unban',
                'timestamp': int(time.time()),
                'reason': '[Ban appeal]' + reason,
                'expiry': None,
                'context': 'banappeal',
                'active': False,
            }
        )

        embed = discord.Embed(color=0x4A90E2, timestamp=datetime.now(tz=timezone.utc))
        embed.set_author(name=f'Ban appeal accepted | {user}')
        embed.set_footer(text=docID)
        embed.add_field(name='User', value=user.mention, inline=True)
        embed.add_field(name='Moderator', value=f'{interaction.user.mention}', inline=True)
        embed.add_field(name='Reason', value=reason)
        await self.bot.get_channel(config.modLog).send(embed=embed)

        try:
            await user.send(
                f'The moderators have decided to **lift your ban** on the {interaction.guild} Discord and your ban appeal thread has been closed. We kindly ask that you look over our server rules again upon your return. You may join back with this invite link: https://discord.gg/switch\nIf you are unable to join please try reloading your  Discord client. Still can\'t join? You are likely IP banned on another account and you will need to appeal that ban as well.\n\nReason given by moderators:\n```{reason}```'
            )

        except:
            await self.bot.get_channel(config.adminChannel).send(
                f':warning: The ban appeal for {user} has been accepted by {interaction.user}, but I was unable to DM them the decision'
            )

        else:
            await self.bot.get_channel(config.adminChannel).send(
                f':white_check_mark: The ban appeal for {user} has been accepted by {interaction.user}'
            )

        finally:
            await utils._close_thread(
                self.bot,
                user,
                None,
                interaction.channel,
                self.bot.get_channel(config.modLog),
                dm=False,
                reason='[Appeal accepted] ' + reason,
            )
            try:
                member = await self.bot.get_guild(config.appealGuild).fetch_member(user.id)
                await member.kick(reason='Accepted appeal')

            except:
                return

    @appeal_group.command(name='deny', description='Deny a user\'s ban appeal')
    @app_commands.describe(
        next_attempt='The amount of time until the user can appeal again, in 1w2d3h4m5s format. You can also pass \'permanent\'.',
        reason='Why are you denying this appeal?',
    )
    async def _appeal_deny(
        self, interaction: discord.Interaction, next_attempt: str, reason: app_commands.Range[str, None, 990]
    ):
        db = mclient.modmail.logs
        punsDB = mclient.bowser.puns
        userDB = mclient.bowser.users

        doc = db.find_one({'channel_id': str(interaction.channel.id), 'open': True, 'ban_appeal': True})
        if not doc:
            return await interaction.response.send_message(':x: This is not a ban appeal channel!', ephemeral=True)

        user = await self.bot.fetch_user(int(doc['recipient']['id']))
        try:
            delayDate = utils.resolve_duration(next_attempt)
            if delayDate != None:
                # We need a timestamp if this will expire
                delayDate = int(delayDate.timestamp())
                humanizedTimestamp = f'until <t:{delayDate}:f>'
                durationUserStr = f'You may appeal again after __<t:{delayDate}:f> (approximately <t:{delayDate}:R>)__. In the meantime you have been kicked from the Ban Appeals server. When you are able to appeal again you may rejoin with this invite: {config.appealInvite}\n\nReason given by moderators:\n```{reason}```'

            else:
                if punsDB.count_documents({'user': user.id, 'type': 'appealdeny'}) < 2:
                    # User has not met the minimum appeal denials to be permanently denied
                    return await interaction.response.send_message(
                        ':x: To permanently deny a ban appeal, the user must have been denied at least 2 times previously'
                    )

                humanizedTimestamp = 'permanently'
                durationUserStr = f'You are not eligible to submit any further appeals for your ban; this decision is final. Please note, it is a [violation of the Discord Community Guidelines](https://discord.com/guidelines/) to use another account to evade this ban and doing so may result in Discord taking action against your account(s), including account termination.'

        except KeyError:
            return await interaction.response.send_message('Invalid duration')

        docID = str(uuid.uuid4())
        while punsDB.find_one({'_id': docID}):  # Uh oh, duplicate uuid generated
            docID = str(uuid.uuid4())

        punsDB.update_one({'user': user.id, 'type': 'appealdeny', 'active': True}, {'$set': {'active': False}})
        punsDB.insert_one(
            {
                '_id': docID,
                'user': user.id,
                'moderator': interaction.user.id,
                'type': 'appealdeny',
                'timestamp': int(time.time()),
                'reason': reason,
                'expiry': delayDate,
                'context': 'banappeal',
                'active': True,
            }
        )

        embed = discord.Embed(color=0x4A90E2, timestamp=datetime.now(tz=timezone.utc))
        embed.set_author(name=f'Ban appeal denied | {user} ({user.id})')
        embed.set_footer(text=docID)
        embed.add_field(name='User', value=user.mention, inline=True)
        embed.add_field(name='Moderator', value=f'{interaction.user.mention}', inline=True)
        embed.add_field(name='Next appeal in', value='Never' if delayDate == None else f'<t:{delayDate}:R>')
        embed.add_field(name='Reason', value=reason)
        await self.bot.get_channel(config.modLog).send(embed=embed)

        try:
            await user.send(
                f'The moderators have decided to **uphold your ban** on the {interaction.guild} Discord and your ban appeal thread has been closed. {durationUserStr}'
            )

        except:
            await self.bot.get_channel(config.adminChannel).send(
                f':warning: The ban appeal for {user} has been denied by {interaction.user} {humanizedTimestamp}, but I was unable to DM them the decision'
            )

        else:
            await self.bot.get_channel(config.adminChannel).send(
                f':white_check_mark: The ban appeal for {user} has been denied by {interaction.user} {humanizedTimestamp}'
            )

        finally:
            await utils._close_thread(
                self.bot,
                user,
                None,
                interaction.channel,
                self.bot.get_channel(config.modLog),
                dm=False,
                reason='[Appeal denied] ' + reason,
            )
            try:
                member = await self.bot.get_guild(config.appealGuild).fetch_member(user.id)
                if delayDate:
                    await member.kick(reason='Failed appeal')

                else:
                    await member.ban(reason='Failed appeal, permanent denial')

            except:
                return

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
                await self.bot.get_channel(int(doc['channel_id'])).typing()

    @commands.Cog.listener()
    async def on_member_ban(self, guild, member):
        db = mclient.modmail.logs
        thread = db.find_one({'recipient.id': str(member.id), 'open': True})
        if thread:
            channel = self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id']))
            await channel.send(f'**{member}** has been banned from the server')

            if not thread['ban_appeal']:
                await self._close_generic(member, member.guild, channel, None)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        db = mclient.modmail.logs
        thread = db.find_one({'recipient.id': str(member.id), 'open': True})
        if thread:  # Check if a thread is open
            if (
                member.guild.id == config.guild and thread['_id'] in self.closeQueue.keys()
            ):  # Standard thread and pending closure
                await self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id'])).send(
                    f'**{member}** has joined the server, thread closure has been canceled'
                )

                self.closeQueue[thread['_id']].cancel()
                self.closeQueue.pop(thread['_id'], None)

            elif thread['ban_appeal']:  # Appeals don't have close delays
                await self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id'])).send(
                    f'**{member}** has rejoined the appeal server'
                )

        if member.guild.id != config.appealGuild:  # Return if guild not the appeal server
            return

        guild = self.bot.get_guild(config.guild)
        try:
            await guild.fetch_ban(member)

        except discord.errors.NotFound:
            guildMember = guild.get_member(member.id)
            if guildMember and (
                guild.get_role(config.modRole) in guildMember.roles
                or guild.get_role(config.trialModRole) in guildMember.roles
            ):
                return

            try:
                await member.send(
                    'You have been automatically kicked from the /r/NintendoSwitch ban appeal server because you are not currently banned.\n\nDiscord also prevents you from joining if another account was banned from our server with the same IP or phone number as you. If you still can\'t join, you will need to submit an appeal from the other account that was banned.'
                )

            except:
                pass

            finally:
                await member.kick(reason='Not banned on /r/NintendoSwitch')

        await utils._can_appeal(member)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await asyncio.sleep(10)  # Wait for ban to pass and thread to close in-case
        db = mclient.modmail.logs
        thread = db.find_one({'recipient.id': str(member.id), 'open': True})
        if thread:
            channel = self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id']))

            if member.guild.id == config.guild:
                msg, _ = await self._close_generic(member, member.guild, channel, '4h')

                if msg:
                    await channel.send(f'**{member}** has left the server. {msg}')

            elif (
                thread['ban_appeal'] and member.guild.id == config.appealGuild
            ):  # We only care about appeal leaves if they had an appeal thread
                await channel.send(f'**{member}** has left the server')

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if message.channel.type not in [discord.ChannelType.private, discord.ChannelType.text]:
            return

        try:
            await self._user_create_thread(message)

        except exceptions.InvalidType:
            logging.error(
                f'Got an invalid MessageType via DM: {message.type} by {message.author} ({message.author.id})'
            )

        except exceptions.ModmailBlacklisted:
            return await message.author.send(
                'Sorry, I cannot create a new modmail thread because you are currently blacklisted. '
                'You may DM a moderator if you still need to contact a Discord staff member.'
            )

    def _format_message_embed(
        self, message: discord.Message, attachments: list, interaction: discord.Interaction = None
    ):
        if message.content:
            content = message.content

        elif message.stickers:
            content = "\n".join([f'*Sent a sticker: {sticker.name}*' for sticker in message.stickers])

        else:
            content = '*No message content.*'

        embed = discord.Embed(
            title='Message reported' if interaction else 'New message',
            description=content,
            color=0xF381FD if interaction else 0x32B6CE,
        )
        embed.set_author(name=f'{message.author} ({message.author.id})', icon_url=message.author.display_avatar.url)

        if not interaction:
            embed.set_footer(text=f'{message.channel.id}/{message.id}')

        else:
            embed.add_field(name='Message link', value=message.jump_url)

        if message.stickers:
            embed.set_image(url=message.stickers[0].url)

        if len(attachments) > 1:  # More than one attachment, use fields
            for x in range(len(attachments)):
                embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])

        elif attachments and re.search(
            r'\.(gif|jpe?g|tiff|png|webp)(\?[a-zA-Z0-9#-_]*)?$', str(attachments[0]), re.IGNORECASE
        ):  # One attachment, image
            embed.set_image(url=attachments[0])

        elif attachments:  # Still have an attachment, but not an image
            embed.add_field(name=f'Attachment', value=attachments[0])

        return content, embed

    async def _user_create_thread(self, message: discord.Message, interaction: discord.Interaction = None):
        successfulDM = False
        attachments = [x.url for x in message.attachments]
        ctx = await self.bot.get_context(message)

        if message.type not in [discord.MessageType.default, discord.MessageType.reply]:
            raise exceptions.InvalidType

        # Do something to check category, and add message to log
        if message.channel.type == discord.ChannelType.private or interaction:
            # User has sent a message -- check
            db = mclient.modmail.logs
            reporter = message.author if not interaction else interaction.user
            thread = db.find_one({'recipient.id': str(reporter.id), 'open': True})
            if thread:
                successfulDM = True
                if thread['_id'] in self.closeQueue.keys():  # Thread close was scheduled, cancel due to response
                    self.closeQueue[thread['_id']].cancel()
                    self.closeQueue.pop(thread['_id'], None)
                    await self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id'])).send(
                        'Thread closure has been canceled because the user has sent a message'
                    )

                content, embed = self._format_message_embed(message, attachments, interaction=interaction)
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
                                'content': content,
                                'type': 'report' if interaction else 'thread_message',
                                'author': {
                                    'id': str(message.author.id),
                                    'name': message.author.name,
                                    'discriminator': message.author.discriminator,
                                    'avatar_url': str(
                                        message.author.display_avatar.with_static_format('png').with_size(1024)
                                    ),
                                    'mod': False,
                                },
                                'attachments': [x.url for x in message.stickers] + attachments,
                            }
                        }
                    },
                )

            else:
                thread, successfulDM = await utils._trigger_create_user_thread(
                    self.bot,
                    message.author if not interaction else interaction.user,
                    message,
                    'user',
                    interaction=interaction,
                )

                content, embed = self._format_message_embed(message, attachments, interaction=interaction)
                await thread.send(embed=embed)

            if not interaction:
                await message.add_reaction('✅')

            if successfulDM and interaction:
                try:
                    reportConfirm = await interaction.user.send(
                        f'*You reported a message from {message.author}: <{message.jump_url}>*'
                    )
                    await reportConfirm.add_reaction('✅')
                except discord.Forbidden:
                    pass

            return successfulDM

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
                                        'avatar_url': str(
                                            message.author.display_avatar.with_static_format('png').with_size(1024)
                                        ),
                                        'mod': True,
                                    },
                                    'attachments': attachments,
                                }
                            }
                        },
                    )

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if ctx.command:
            cmd_str = ctx.command.full_parent_name + ' ' + ctx.command.name if ctx.command.parent else ctx.command.name
        else:
            cmd_str = None

        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(
                f'{config.redTick} Missing one or more required arguments. See `{ctx.prefix}help {cmd_str}`',
                delete_after=15,
            )

        elif isinstance(error, commands.BadArgument):
            return await ctx.send(
                f'{config.redTick} One or more provided arguments are invalid. See `{ctx.prefix}help {cmd_str}`',
                delete_after=15,
            )

        elif isinstance(error, commands.CheckFailure):
            return await ctx.send(f'{config.redTick} You do not have permission to run this command', delete_after=15)

        elif isinstance(error, commands.CommandNotFound):
            return await ctx.send(
                f'{config.redTick} Invalid command. If you are trying to open a modmail, please send me a DM to begin',
                delete_after=15,
            )

        else:
            await ctx.send(
                f'{config.redTick} An unknown exception has occured, if this continues to happen contact the developer.',
                delete_after=15,
            )

            raise error


async def setup(bot):
    await bot.add_cog(Mail(bot))
