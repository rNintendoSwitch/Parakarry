import asyncio
import logging
import re
import time
import typing
import uuid
from datetime import datetime, timezone
from sys import exit

import discord
import pymongo
from discord import app_commands
from discord.ext import commands

import cogs.utils as utils


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
        self.READY = False
        self.closeQueue = {}

    @app_commands.command(name='close', description='Closes a modmail thread, optionally with a delay')
    @app_commands.describe(delay='The delay for the modmail to close, in 1w2d3h4m5s format')
    @app_commands.guild_only()
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
                utils._close_thread(self.bot, user, guild, channel, self.modLogs),
            )
            self.closeQueue[doc['_id']] = close_action
            return f'Thread scheduled to be closed <t:{int(delayDate.timestamp())}:R>', False

        await utils._close_thread(self.bot, user, guild, channel, self.modLogs)

    @app_commands.command(name='reply', description='Replys to a modmail, with your username')
    @app_commands.describe(content='The message to send to the user')
    @app_commands.describe(attachment='An image or file to send to the user')
    @app_commands.guild_only()
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
    @app_commands.guild_only()
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

            elif self.leadModRole in interaction.guild.get_member(interaction.user).roles:
                responsibleModerator = f'*(Lead Moderator)* **{interaction.user}**'

            else:
                responsibleModerator = f'*(Moderator)* **{interaction.user}**'

            replyText = f'Reply from {responsibleModerator}: {content if content else ""}'

            if attachment:
                replyMessage = await member.send(replyText, file=await attachment.to_file())

            else:
                replyMessage = await member.send(replyText)

        except:
            return await interaction.response.send_message(
                'There was an issue replying to this user, they may have left the server or disabled DMs'
            )

        embed = discord.Embed(title='Moderator message', description=content, color=0x7ED321)
        if not anonymous:
            embed.set_author(name=f'{interaction.user} ({interaction.user.id})', icon_url=interaction.user.avatar.url)

        else:
            embed.title = '[ANON] Moderator message'
            embed.set_author(
                name=f'{interaction.user} ({interaction.user.id}) as r/NintendoSwitch',
                icon_url='https://cdn.mattbsg.xyz/rns/snoo.png',
            )

        if attachment and re.search(
            r'\.(gif|jpe?g|tiff|png|webp)$', str(attachment), re.IGNORECASE
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
                            'avatar_url': str(interaction.user.avatar.with_static_format('png').with_size(1024)),
                            'mod': True,
                        },
                        'attachments': [replyMessage.attachments[0].url] if replyMessage.attachments else [],
                    }
                }
            },
        )

    @app_commands.command(name='open', description='Open a modmail thread with a user')
    @app_commands.describe(member='The user to start a thread with')
    @app_commands.guild_only()
    @app_commands.default_permissions(view_audit_log=True)
    async def _open_thread(self, interaction: discord.Interaction, member: discord.Member):
        """
        Open a modmail thread with a user
        """

        if mclient.modmail.logs.find_one({'recipient.id': str(member.id), 'open': True}):
            return await interaction.response.send_message(
                ':x: Unable to open modmail to user -- there is already a thread involving them currently open',
                ephemeral=True,
            )

        try:
            await utils._trigger_create_mod_thread(self.bot, interaction.guild, member, interaction.user)

        except discord.Forbidden:
            return

        await interaction.response.send_message(f':white_check_mark: Modmail has been opened with {member}')

    appeal_group = app_commands.Group(
        name='appeal',
        description='Make a decision to accept or deny a ban appeal',
        guild_only=True,
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
        await self.modLogs.send(embed=embed)

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
                self.modLogs,
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
        next_attempt='The amount of time until the user can appeal again, in 1w2d3h4m5s format',
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

        try:
            delayDate = utils.resolve_duration(next_attempt)

        except KeyError:
            return await interaction.response.send_message('Invalid duration')

        user = await self.bot.fetch_user(int(doc['recipient']['id']))
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
                'expiry': int(delayDate.timestamp()),
                'context': 'banappeal',
                'active': True,
            }
        )

        embed = discord.Embed(color=0x4A90E2, timestamp=datetime.now(tz=timezone.utc))
        embed.set_author(name=f'Ban appeal denied | {user}')
        embed.set_footer(text=docID)
        embed.add_field(name='User', value=user.mention, inline=True)
        embed.add_field(name='Moderator', value=f'{interaction.user.mention}', inline=True)
        embed.add_field(name='Next appeal in', value=f'<t:{int(delayDate.timestamp())}:R>')
        embed.add_field(name='Reason', value=reason)
        await self.modLogs.send(embed=embed)

        try:
            await user.send(
                f'The moderators have decided to **uphold your ban** on the {interaction.guild} Discord and your ban appeal thread has been closed. You may appeal again after __<t:{int(delayDate.timestamp())}:f> (approximately <t:{int(delayDate.timestamp())}:R>)__. In the meantime you have been kicked from the Ban Appeals server. When you are able to appeal again you may rejoin with this invite: {config.appealInvite}\n\nReason given by moderators:\n```{reason}```'
            )

        except:
            await self.bot.get_channel(config.adminChannel).send(
                f':warning: The ban appeal for {user} has been denied by {interaction.user} until <t:{int(delayDate.timestamp())}:f>, but I was unable to DM them the decision'
            )

        else:
            await self.bot.get_channel(config.adminChannel).send(
                f':white_check_mark: The ban appeal for {user} has been denied by {interaction.user} until <t:{int(delayDate.timestamp())}:f>'
            )

        finally:
            await utils._close_thread(
                self.bot,
                user,
                None,
                interaction.channel,
                self.modLogs,
                dm=False,
                reason='[Appeal denied] ' + reason,
            )
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

            self.leadModRole = self.bot.get_guild(config.guild).get_role(config.leadModRole)
            self.modRole = self.bot.get_guild(config.guild).get_role(config.modRole)
            self.trialModRole = self.bot.get_guild(config.guild).get_role(config.trialModRole)

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

        except discord.errors.NotFound:
            guildMember = guild.get_member(member.id)
            if guildMember and (
                guild.get_role(config.modRole) in guildMember.roles
                or guild.get_role(config.trialModRole) in guildMember.roles
            ):
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
            channel = self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id']))
            await channel.send(f'**{member}** has left the server')

            if not thread['ban_appeal']:
                msg, _ = await self._close_generic(member, member.guild, channel, '4h')

                if msg:
                    channel.send(msg)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        attachments = [x.url for x in message.attachments]
        ctx = await self.bot.get_context(message)

        if message.content:
            content = message.content
        elif message.stickers:
            content = "\n".join([f'*Sent a sticker: {sticker.name}*' for sticker in message.stickers])
        else:
            content = '*No message content.*'

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

                description = content
                embed = discord.Embed(title='New message', description=description, color=0x32B6CE)
                embed.set_author(name=f'{message.author} ({message.author.id})', icon_url=message.author.avatar.url)
                embed.set_footer(text=f'{message.channel.id}/{message.id}')
                if message.stickers:
                    embed.set_image(url=message.stickers[0].url)

                if len(attachments) > 1:  # More than one attachment, use fields
                    for x in range(len(attachments)):
                        embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])

                elif attachments and re.search(
                    r'\.(gif|jpe?g|tiff|png|webp)$', str(attachments[0]), re.IGNORECASE
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
                                'content': content,
                                'type': 'thread_message',
                                'author': {
                                    'id': str(message.author.id),
                                    'name': message.author.name,
                                    'discriminator': message.author.discriminator,
                                    'avatar_url': str(message.author.avatar.with_static_format('png').with_size(1024)),
                                    'mod': False,
                                },
                                'attachments': [x.url for x in message.stickers] + attachments,
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
                embed = discord.Embed(title='New message', description=content, color=0x32B6CE)
                embed.set_author(
                    name=f'{message.author} ({message.author.id})',
                    icon_url=message.author.avatar.with_static_format('png').with_size(1024),
                )
                embed.set_footer(text=f'{message.channel.id}/{message.id}')

                if len(attachments) > 1:  # More than one attachment, use fields
                    for x in range(len(attachments)):
                        embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])

                elif attachments and re.search(
                    r'\.(gif|jpe?g|tiff|png|webp)$', str(attachments[0]), re.IGNORECASE
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
                                    'content': content,
                                    'type': 'internal',
                                    'author': {
                                        'id': str(message.author.id),
                                        'name': message.author.name,
                                        'discriminator': message.author.discriminator,
                                        'avatar_url': str(
                                            message.author.avatar.with_static_format('png').with_size(1024)
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
