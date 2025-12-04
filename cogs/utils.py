import time
import typing
from datetime import datetime, timedelta, timezone

import config
import discord
import pymongo

import exceptions


mclient = pymongo.MongoClient(config.mongoURI)
punNames = {
    'strike': '{} Strike{}',
    'destrike': 'Removed {} Strike{}',
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
    'note': 'User note',
    'appealdeny': 'Denied ban appeal',
}
tagIDS = {
    'user': config.userThreadTag,
    'moderator': config.modThreadTag,
    'ban_appeal': config.banAppealTag,
    'message_report': config.messageReportTag,
}


def resolve_duration(data):
    """
    Takes a raw input string formatted 1w1d1h1m1s (any order)
    and converts to timedelta
    Credit https://github.com/b1naryth1ef/rowboat via MIT license

    data: str
    """
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
        if data.lower() in ['perm', 'permanent', 'forever']:
            # Never expires, so we aren't going to grab a timestamp
            return None

        pass

    for char in data:
        if char.isdigit():
            digits += char
            continue

        if char not in timeUnits or not digits:
            raise KeyError('Time format not a valid entry')

        value += timeUnits[char](int(digits))
        digits = ''

    return datetime.now(tz=timezone.utc) + timedelta(seconds=value + 1)


def humanize_duration(duration):
    """
    Takes a datetime object and returns a prettified
    weeks, days, hours, minutes, seconds string output
    Credit https://github.com/ThaTiemsz/jetski via MIT license

    duration: datetime.datetime
    """
    now = datetime.now(tz=timezone.utc)
    if isinstance(duration, timedelta):
        if duration.total_seconds() > 0:
            duration = datetime.today() + duration
        else:
            duration = datetime.now(tz=timezone.utc) - timedelta(seconds=duration.total_seconds())
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

    if not expires:
        return '0 seconds'
    return ', '.join(expires)


async def _can_appeal(member):
    db = mclient.bowser.puns
    pun = db.find_one({'user': member.id, 'type': 'appealdeny', 'active': True})
    if pun:
        try:
            if pun['expiry'] == None:
                await member.send(
                    f'You have been automatically kicked from the /r/NintendoSwitch ban appeal server because you cannot make a new appeal. \n\nReason given by moderators:\n```{pun["reason"]}```'
                )

            elif pun['expiry'] > datetime.now(tz=timezone.utc).timestamp():
                expiry = datetime.fromtimestamp(pun['expiry'], tz=timezone.utc)
                await member.send(
                    f'You have been automatically kicked from the /r/NintendoSwitch ban appeal server because you cannot make a new appeal yet. You can join back after __<t:{int(expiry.timestamp())}:f> (approximately <t:{int(expiry.timestamp())}:R>)__ to submit a new appeal with the following invite link: {config.appealInvite}\n\nReason given by moderators:\n```{pun["reason"]}```'
                )

        finally:
            if pun['expiry'] > datetime.now(tz=timezone.utc).timestamp():
                await member.kick(reason='Not ready to appeal again')
                return False

    return True


async def _create_thread(
    bot,
    channel,
    creator,
    recipient,
    is_mention=False,
    content=None,
    is_mod=False,
    ban_appeal=False,
    message=None,
    created_at=None,
    report=None,
):
    db = mclient.modmail.logs
    initial_message = None
    if message:
        attachments = [x.url for x in message.attachments]
        initial_message = {
            'timestamp': str(message.created_at),
            'message_id': str(message.id),
            'content': message.content if not content else content,
            'type': 'report' if report else 'thread_message',
            'author': {
                'id': str(message.author.id),
                'name': message.author.name,
                'discriminator': message.author.discriminator,
                'avatar_url': str(message.author.display_avatar.with_static_format('png').with_size(1024)),
                'mod': is_mod,
            },
            'attachments': attachments,
            'channel': {'id': str(message.channel.id), 'name': message.channel.name} if is_mention else {},
        }

        _id = str(message.id) + '-' + str(int(time.time()))
        if report:
            created_at = datetime.now(tz=timezone.utc).isoformat(sep=' ')

        else:
            created_at = str(message.created_at)

    else:
        _id = str(channel.id) + '-' + str(int(time.time()))

    db.insert_one(
        {
            '_id': _id,
            'key': _id,
            'open': True,
            'created_at': created_at,
            'closed_at': None,
            'channel_id': str(channel.id),
            'guild_id': str(channel.guild.id),
            'bot_id': str(bot.user.id),
            'ban_appeal': ban_appeal,
            'recipient': {
                'id': str(recipient.id),
                'name': recipient.name,
                'discriminator': recipient.discriminator,
                'avatar_url': str(recipient.display_avatar.with_static_format('png').with_size(1024)),
                'mod': False,
            },
            'creator': {
                'id': str(creator.id),
                'name': creator.name,
                'discriminator': creator.discriminator,
                'avatar_url': str(creator.display_avatar.with_static_format('png').with_size(1024)),
                'mod': False,
            },
            'closer': None,
            'messages': [] if not initial_message else [initial_message],
        }
    )

    return _id


async def _close_thread(
    bot,
    mod_user: discord.User,
    guild: discord.Guild,
    thread_channel: discord.TextChannel,
    target_channel: discord.TextChannel,
    dm: bool = True,
    reason: str = None,
):
    db = mclient.modmail.logs
    doc = db.find_one({'channel_id': str(thread_channel.id)})

    closeInfo = {
        '$set': {
            'open': False,
            'closed_at': datetime.now(tz=timezone.utc).isoformat(sep=' '),
            'closer': {
                'id': str(mod_user.id),
                'name': mod_user.name,
                'discriminator': mod_user.discriminator,
                'avatar_url': str(mod_user.display_avatar.with_static_format('png').with_size(1024)),
                'mod': True,
            },
        }
    }

    if reason:
        closeInfo['$set']['close_message'] = reason
    db.update_one({'_id': doc['_id']}, closeInfo)

    try:
        channel = bot.get_channel(thread_channel.id)
        await channel.edit(locked=True, archived=True, reason=f'Modmail closed by {mod_user}')

    except discord.NotFound:
        pass

    if dm:
        try:
            mailer = await guild.fetch_member(int(doc['recipient']['id']))
            await mailer.send(
                '__Your modmail thread has been closed__. If you need to contact the chat-moderators you may send me another DM to open a new modmail thread'
            )

        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            await bot.get_channel(config.adminChannel).send(
                f'Failed to send DM to <@{doc["recipient"]["id"]}> for modmail closure. They have not been notified'
            )

    user = doc['recipient']

    embed = discord.Embed(description=thread_channel.jump_url, color=0xB8E986, timestamp=datetime.now(tz=timezone.utc))

    embed.set_author(name=f'Modmail closed | {user["name"]} ({user["id"]})')

    embed.add_field(name='User', value=f'<@{user["id"]}>', inline=True)
    embed.add_field(name='Moderator', value=f'{mod_user.mention}', inline=True)
    await target_channel.send(embed=embed)


async def _trigger_create_user_thread(
    bot,
    member,
    message,
    open_type,
    is_mention=False,
    moderator=None,
    content=None,
    anonymous=True,
    interaction=None,
):
    db = mclient.modmail.logs
    punsDB = mclient.bowser.puns
    successfulDM = False

    guild = bot.get_guild(config.guild)
    appealGuild = bot.get_guild(config.appealGuild)
    try:
        await guild.fetch_member(member.id)

    except discord.NotFound:
        # If the user is not in the primary guild. Failsafe check in-case on_member_join didn't catch them
        open_type = 'ban_appeal'
        try:
            await guild.fetch_ban(member)

        except discord.NotFound:
            await member.send(
                'You are not banned from /r/NintendoSwitch and have been kicked from the ban appeal server.'
            )
            appealMember = await appealGuild.fetch_member(member.id)
            await appealMember.kick(reason='Member is not banned on /r/NintendoSwitch')

            raise RuntimeError('User is not banned from server')

        else:
            if not await _can_appeal(member):
                raise RuntimeError('User cannot appeal')

    # Deny thread creation if modmail restricted
    if open_type == 'user':
        if not mclient.bowser.users.find_one({'_id': member.id})['modmail']:
            raise exceptions.ModmailBlacklisted

    forum = guild.get_channel(config.forumChannel)
    postName = member.name + ' - '
    if open_type == 'ban_appeal':
        postName += 'Ban Appeal'
        embed = discord.Embed(title='New ban appeal submitted', color=0xEE5F5F)

    else:
        embed = discord.Embed(title='New modmail opened', color=0xE3CF59)

    embed.set_author(
        name=f'{member} ({member.id})',
        icon_url=member.display_avatar.with_static_format('png').with_size(1024),
    )

    threadCount = db.count_documents({'recipient.id': str(member.id)})

    punsDB = mclient.bowser.puns
    puns = punsDB.find({'user': member.id, 'active': True})
    punsCnt = punsDB.count_documents({'user': member.id, 'active': True})
    if open_type == 'ban_appeal':
        description = f'A new ban appeal has been submitted by {member} ({member.mention}) and needs to be reviewed.'

    elif open_type == 'moderator':
        postName += 'Mod Opened'
        description = f'A modmail thread has been opened with {member} ({member.mention}) by {moderator} ({moderator.mention}). There are {threadCount} previous threads involving this user.'

    elif open_type == 'message_report':
        postName += 'Message Reported'
        description = f"A new message report needs to be reviewed from {member} ({member.mention}). There are {threadCount} previous threads involving this user."

    else:
        postName += 'Modmail'
        description = f"A new modmail needs to be reviewed from {member} ({member.mention}). There are {threadCount} previous threads involving this user."

    if punsCnt:
        description += '\n\n__User has active punishments:__\n'
        for pun in puns:
            timestamp = f'<t:{int(pun["timestamp"])}:f>'
            if pun['type'] == 'strike':
                description += f"**{punNames[pun['type']].format(pun['active_strike_count'], 's' if pun['active_strike_count'] > 1 else '')}** by <@{pun['moderator']}> on {timestamp}\n    ･ {pun['reason']}\n"

            else:
                description += (
                    f"**{punNames[pun['type']]}** by <@{pun['moderator']}> on {timestamp}\n    ･ {pun['reason']}\n"
                )

    embed.description = description
    tag = forum.get_tag(tagIDS[open_type])
    thread, threadMessage = await forum.create_thread(
        name=postName, auto_archive_duration=10080, embed=embed, applied_tags=[tag], reason='New modmail opened'
    )
    await _create_thread(
        bot,
        thread,
        member if not moderator else moderator,
        member,
        is_mention,
        content=content,
        is_mod=True if moderator else False,
        ban_appeal=open_type == 'ban_appeal',
        message=message,
        report=interaction,
    )
    await _info(
        await bot.get_context(threadMessage),
        bot,
        member.id if open_type == 'ban_appeal' else await guild.fetch_member(member.id),
    )

    if open_type == 'ban_appeal':
        await member.send(
            f'Hi there!\nYou have submitted a ban appeal to the chat moderators who oversee the **{guild.name}** Discord.\n\nI will send you a message when a moderator responds to this thread. Every message you send to me while your thread is open will also be sent to the moderation team -- so you can message me anytime to add information or to reply to a moderator\'s message. You\'ll know your message has been sent when I react to your message with a ✅.\n\nPlease be patient for a response; the moderation team will have active discussions about the appeal and may take some time to reply. We ask that you be civil and respectful during this process so constructive conversation can be had in both directions. At the end of this process, moderators will either lift or uphold your ban -- you will receive an official message stating the final decision.'
        )
        successfulDM = True

    else:
        try:
            await member.send(
                f'Hi there!\nYou have opened a modmail thread with the chat moderators who oversee the **{guild.name}** Discord and they have received your message.\n\nI will send you a message when moderators respond to this thread. Every message you send to me while your thread is open will also be sent to the moderation team -- so you can message me anytime to add information or to reply to a moderator\'s message. You\'ll know your message has been sent when I react to your message with a ✅. \n\nPlease be patient for a response; if this is an urgent issue you may also ping the Chat-Mods with @Chat-Mods in a channel'
            )
            successfulDM = True

        except discord.Forbidden:
            pass

    return thread, successfulDM


async def _trigger_create_mod_thread(bot, guild, member, moderator):
    db = mclient.modmail.logs
    punsDB = mclient.bowser.puns

    guild = bot.get_guild(config.guild)
    appealGuild = bot.get_guild(config.appealGuild)
    try:
        await guild.fetch_member(member.id)

    except discord.NotFound:
        raise RuntimeError('Invalid user')  # TODO: We need custom exceptions

    forum = guild.get_channel(config.forumChannel)
    postName = member.name + ' - Mod Opened'
    tag = forum.get_tag(tagIDS['moderator'])

    embed = discord.Embed(title='New modmail opened', color=0xE3CF59)

    embed.set_author(
        name=f'{member} ({member.id})',
        icon_url=member.display_avatar.with_static_format('png').with_size(1024),
    )

    threadCount = db.count_documents({'recipient.id': str(member.id)})

    punsDB = mclient.bowser.puns
    puns = punsDB.find({'user': member.id, 'active': True})
    punsCnt = punsDB.count_documents({'user': member.id, 'active': True})

    description = f'A modmail thread has been opened with {member} ({member.mention}) by {moderator} ({moderator.mention}). There are {threadCount} previous threads involving this user.'

    if punsCnt:
        description += '\n\n__User has active punishments:__\n'
        for pun in puns:
            timestamp = f'<t:{int(pun["timestamp"])}:f>'
            if pun['type'] == 'strike':
                description += f"**{punNames[pun['type']].format(pun['active_strike_count'], 's' if pun['active_strike_count'] > 1 else '')}** by <@{pun['moderator']}> on {timestamp}\n    ･ {pun['reason']}\n"

            else:
                description += (
                    f"**{punNames[pun['type']]}** by <@{pun['moderator']}> on {timestamp}\n    ･ {pun['reason']}\n"
                )

    embed.description = description
    thread, threadMessage = await forum.create_thread(
        name=postName,
        auto_archive_duration=10080,
        content=moderator.mention,
        embed=embed,
        applied_tags=[tag],
        reason='New modmail opened',
    )
    docID = await _create_thread(
        bot, thread, moderator, member, created_at=datetime.now(tz=timezone.utc).isoformat(sep=' ')
    )  # Since we don't have a reference with slash commands, pull current iso datetime in UTC
    await _info(await bot.get_context(threadMessage), bot, await guild.fetch_member(member.id))
    try:
        await member.send(
            f'Hi there!\nThe chat moderators who oversee the **{guild.name}** Discord have opened a modmail with you!\n\nI will send you a message when a moderator responds to this thread. Every message you send to me while your thread is open will also be sent to the moderation team -- so you can message me anytime to add information or to reply to a moderator\'s message. You\'ll know your message has been sent when I react to your message with a ✅.'
        )

    except discord.Forbidden:
        # Cleanup if there really was an issue messaging the user, i.e. bot blocked
        db.delete_one({'_id': docID})
        await thread.delete()
        raise

    embed = discord.Embed(
        title='Thread is open',
        description='This thread is now open to moderator and user replies. Start the conversation by using `/reply` or `/areply`',
        color=0x58B9FF,
    )
    await thread.send(content=f'<@&{config.modRole}>', embed=embed, silent=True)


async def _info(ctx, bot, user: typing.Union[discord.Member, int]):
    inServer = True
    if type(user) == int:
        # User doesn't share the ctx server, fetch it instead
        dbUser = mclient.bowser.users.find_one({'_id': user})
        inServer = False

        user = await bot.fetch_user(user)

        if not dbUser:
            embed = discord.Embed(
                color=discord.Color(0x18EE1C),
                description=f'Fetched information about {user.mention} from the API because they are not in this server. There is little information to display as they have not been recorded joining the server before.',
            )
            embed.set_author(
                name=f'{str(user)} | {user.id}',
                icon_url=user.display_avatar.with_static_format('png').with_size(1024),
            )
            embed.set_thumbnail(url=user.display_avatar.with_static_format('png').with_size(1024))
            embed.add_field(name='Created', value=f'<t:{int(user.created_at.timestamp())}:f>')
            return await ctx.send(embed=embed)  # TODO: Return DB info if it exists as well

    else:
        dbUser = mclient.bowser.users.find_one({'_id': user.id})

    # Member object, loads of info to work with
    messages = mclient.bowser.messages.find({'author': user.id})
    msgCount = 0 if not messages else mclient.bowser.messages.count_documents({'author': user.id})

    desc = (
        f'Fetched user {user.mention}.'
        if inServer
        else f'Fetched information about previous member {user.mention} '
        'from the API because they are not in this server. '
        'Showing last known data from before they left.'
    )

    embed = discord.Embed(color=discord.Color(0x18EE1C), description=desc)
    embed.set_author(
        name=f'{str(user)} | {user.id}',
        icon_url=user.display_avatar.with_static_format('png').with_size(1024),
    )
    embed.set_thumbnail(url=user.display_avatar.with_static_format('png').with_size(1024))
    embed.add_field(name='Messages', value=str(msgCount), inline=True)
    if inServer:
        embed.add_field(name='Join date', value=f'<t:{int(user.joined_at.timestamp())}:f>', inline=True)

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

        # concat roles into comma delimitered string
        roles = str(roleList[0])
        for i, role in enumerate(roleList[1:]):
            if len(f"{roles}, {role}") > 1000:  # too big?
                roles += f", and {len(roleList) - i} more..."
                break

            roles += f", {role}"

    embed.add_field(name='Roles', value=roles, inline=False)

    lastMsg = 'N/a' if msgCount == 0 else f'<t:{int(messages.sort("timestamp", pymongo.DESCENDING)[0]["timestamp"])}:f>'
    embed.add_field(name='Last message', value=lastMsg, inline=True)
    embed.add_field(name='Created', value=f'<t:{int(user.created_at.timestamp())}:f>', inline=True)

    noteDocs = mclient.bowser.puns.find({'user': user.id, 'type': 'note'})
    noteCnt = mclient.bowser.puns.count_documents({'user': user.id, 'type': 'note'})
    fieldValue = 'View history to get full details on all notes.\n\n'
    if noteCnt:
        noteList = []
        for x in noteDocs.sort('timestamp', pymongo.DESCENDING):
            stamp = f'[<t:{int(x["timestamp"])}:d>]'
            noteContent = f'{stamp}: {x["reason"]}'

            fieldLength = 0
            for value in noteList:
                fieldLength += len(value)
            if len(noteContent) + fieldLength > 924:
                fieldValue = f'Only showing {len(noteList)}/{noteCnt} notes. ' + fieldValue
                break

            noteList.append(noteContent)

        embed.add_field(name='User notes', value=fieldValue + '\n'.join(noteList), inline=False)

    punishments = ''
    punsCol = mclient.bowser.puns.find({'user': user.id, 'type': {'$ne': 'note'}})
    punsCnt = mclient.bowser.puns.count_documents({'user': user.id, 'type': {'$ne': 'note'}})
    if not punsCnt:
        punishments = '__*No punishments on record*__'

    else:
        puns = 0
        activeStrikes = 0
        totalStrikes = 0
        activeMute = None
        for pun in punsCol.sort('timestamp', pymongo.DESCENDING):
            if pun['type'] == 'strike':
                totalStrikes += pun['strike_count']
                activeStrikes += pun['active_strike_count']

            elif pun['type'] == 'destrike':
                totalStrikes -= pun['strike_count']

            elif pun['type'] == 'mute':
                if pun['active']:
                    activeMute = pun['expiry']

            if puns >= 5:
                continue

            puns += 1
            stamp = f'<t:{int(pun["timestamp"])}:f>'
            punType = punNames[pun['type']]
            if pun['type'] in ['clear', 'unmute', 'unban', 'unblacklist', 'destrike']:
                if pun['type'] == 'destrike':
                    punType = f'Removed {pun["strike_count"]} Strike{"s" if pun["strike_count"] > 1 else ""}'

                punishments += f'> {config.removeTick} {stamp} **{punType}**\n'

            elif pun['type'] == 'strike':
                punishments += f'> {config.addTick} {stamp} **{punType.format(pun["strike_count"], "s" if pun["strike_count"] > 1 else "")}**\n'

            else:
                punishments += f'> {config.addTick} {stamp} **{punType}**\n'

        punishments = (
            f'Showing {puns}/{punsCnt} punishment entries. '
            f'For a full history including responsible moderator, active status, and more use the `/history {user.id}` command'
            f'\n\n{punishments}'
        )

        if activeMute:
            embed.description += f'\n**User is currently muted until <t:{activeMute}:f>**'

        if totalStrikes:
            embed.description += f'\nUser currently has {activeStrikes} active strike{"s" if activeStrikes != 1 else ""} ({totalStrikes} in total)'

    embed.add_field(name='Punishments', value=punishments, inline=False)
    return await ctx.send(embed=embed)


class RiskyConfirmation(discord.ui.View):
    message: discord.Message | None = None

    def __init__(self, timeout=120.0):
        super().__init__(timeout=timeout)
        self.value = None

    @discord.ui.button(label='Yes', style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.disable_buttons()
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label='No', style=discord.ButtonStyle.primary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self.disable_buttons()
        await interaction.response.edit_message(view=self)
        self.stop()

    def disable_buttons(self):
        for c in self.children:
            c.disabled = True

    async def on_timeout(self):
        self.disable_buttons()
        if self.message:
            await self.message.edit(view=self)
