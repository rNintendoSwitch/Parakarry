import asyncio
import datetime
import re
import time
import typing

import discord
import pymongo

import config

mclient = pymongo.MongoClient(
	config.mongoHost,
	username=config.mongoUser,
	password=config.mongoPass
)
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
    'blacklist': 'Blacklist',
    'unblacklist': 'Unblacklist',
    'note': 'User note'
}

def resolve_duration(data):
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

def humanize_duration(duration):
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

async def _create_thread(bot, channel, message, creator, recipient, is_mention, content=None, is_mod=False, ban_appeal=False):
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
        'bot_id': str(bot.user.id),
        'ban_appeal': ban_appeal,
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
                'content': message.content if not content else content,
                'type': 'mention' if is_mention else 'thread_message',
                'author': {
                    'id': str(message.author.id),
                    'name': message.author.name,
                    'discriminator': message.author.discriminator,
                    'avatar_url': str(message.author.avatar_url_as(static_format='png', size=1024)),
                    'mod': is_mod
                },
                'attachments': attachments,
                'channel': {
                    'id': str(message.channel.id),
                    'name': message.channel.name
                } if is_mention else {}
            }
        ]
    })

    return _id

async def _close_thread(bot, ctx, target_channel):
    db = mclient.modmail.logs
    doc = db.find_one({'channel_id': str(ctx.channel.id)})
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

    try:
        channel = bot.get_channel(ctx.channel.id)
        await channel.delete(reason=f'Modmail closed by {ctx.author}')

    except discord.NotFound:
        pass

    try:
        mailer = await ctx.guild.fetch_member(int(doc['recipient']['id']))
        await mailer.send('__Your modmail thread has been closed__. If you need to contact the chat-moderators you may send me another DM to open a new modmail thread')

    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
        await bot.get_channel(config.adminChannel).send(f'Failed to send DM to <@{doc["recipient"]["id"]}> for modmail closure. They have not been notified')


    user = doc['recipient']

    embed = discord.Embed(description=config.logUrl + doc['_id'], color=0xB8E986, timestamp=datetime.datetime.utcnow())
    embed.set_author(name=f'Mod mail closed | {user["name"]}#{user["discriminator"]} ({user["id"]})')
    embed.add_field(name='User', value=f'<@{user["id"]}>', inline=True)
    embed.add_field(name='Moderator', value=f'{ctx.author.mention}', inline=True)
    await target_channel.send(embed=embed)

async def _trigger_create_thread(bot, member, message, open_type, is_mention=False, moderator=None, content=None, anonymous=True):
    db = mclient.modmail.logs
    banAppeal = False

    if open_type == 'user':
        if not mclient.bowser.users.find_one({'_id': member.id})['modmail']: # Modmail restricted, deny thread creation
            return await member.send('Sorry, I cannot create a new modmail thread because you are currently blacklisted. ' \
                                            'You may DM a moderator if you still need to contact a Discord staff member.')
    guild = bot.get_guild(config.guild)
    try:
        await guild.fetch_member(member.id)

    except discord.NotFound:
        # If the user is not in the primary guild
        banAppeal = True

    category = guild.get_channel(config.category)
    channel = await category.create_text_channel(f'{member.name}-{member.discriminator}', reason='New modmail opened')

    if banAppeal:
        embed = discord.Embed(title='New ban appeal submitted', color=0xEE5F5F)

    else:
        embed = discord.Embed(title='New modmail opened', color=0xE3CF59)

    embed.set_author(name=f'{member} ({member.id})', icon_url=member.avatar_url)

    threadCount = db.count_documents({'recipient.id': str(member.id)})
    docID = await _create_thread(bot, channel, message, member if not moderator else moderator, member, is_mention, content=None if not content else content, is_mod=True if moderator else False, ban_appeal=banAppeal)

    punsDB = mclient.bowser.puns
    puns = punsDB.find({'user': member.id, 'active': True})
    punsCnt = punsDB.count_documents({'user': member.id, 'active': True})
    if banAppeal:
        description = f'A new ban appeal has been submitted by {member} ({member.mention}) and needs to be reviewed'

    elif open_type == 'moderator':
        description = f'A modmail thread has been opened with {member} ({member.mention}) by {moderator} ({moderator.mention}). There are {threadCount} previous threads involving this user'

    else:
        description = f"A new modmail needs to be reviewed from {member} ({member.mention}). There are {threadCount} previous threads involving this user"

    description += f'. Archive link: {config.logUrl}{docID}'
    if punsCnt:
        description += '\n\n__User has active punishments:__\n'
        for pun in puns:
            timestamp = datetime.datetime.utcfromtimestamp(pun['timestamp']).strftime('%b %d, %y at %H:%M UTC')
            description += f"**{punNames[pun['type']]}** by <@{pun['moderator']}> on {timestamp}\n    ･ {pun['reason']}\n"

    embed.description = description
    mailMsg = await channel.send(embed=embed)
    await _info(await bot.get_context(mailMsg), bot, member.id)

    if banAppeal:
        await member.send(f'Hi there!\nYou have submitted a ban appeal to the chat moderators who oversee the **{guild.name}** Discord.\n\nI will send you a message when a moderator responds to this thread. Every message you send to me while your thread is open will also be sent to the moderation team -- so you can message me anytime to add information or to reply to a moderator\'s message. You\'ll know your message has been sent when I react to your message with a ✅.\n\nPlease be patient for a response; the moderation team will have active discussions about the appeal and may take some time to reply. We ask that you be civil and respectful during this process so constructive conversation can be had in both directions. At the end of this process, moderators will either lift or uphold your ban -- you will receive an official message stating the final decision.')

    elif open_type == 'moderator':
        try:
            attachments = [x.url for x in message.attachments]
            await member.send(f'Hi there!\nThe chat moderators who oversee the **{guild.name}** Discord have opened a modmail with you!\n\nI will send you a message when a moderator responds to this thread. Every message you send to me while your thread is open will also be sent to the moderation team -- so you can message me anytime to add information or to reply to a moderator\'s message. You\'ll know your message has been sent when I react to your message with a ✅.')
            await member.send(f'Message from **{"Moderator" if anonymous else message.author}**: {content if content else ""}')
            if attachments:
                await member.send('\n'.join(attachments))

            embed = discord.Embed(title='Moderator message', description=content, color=0x7ED321)
            if not anonymous:
                embed.set_author(name=f'{moderator} ({moderator.id})', icon_url=moderator.avatar_url)

            else:
                embed.title = '[ANON] Moderator message'
                embed.set_author(name=f'{moderator} ({moderator.id}) as r/NintendoSwitch', icon_url='https://cdn.mattbsg.xyz/rns/snoo.png')
    
            if len(attachments) > 1: # More than one attachment, use fields
                for x in range(len(attachments)):
                    embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])
    
            elif attachments and re.search(r'\.(gif|jpe?g|tiff|png|webp|bmp)$', str(attachments[0]), re.IGNORECASE): # One attachment, image
                embed.set_image(url=attachments[0])
    
            elif attachments: # Still have an attachment, but not an image
                embed.add_field(name=f'Attachment', value=attachments[0])
    
            await channel.send(embed=embed)

        except discord.Forbidden:
            # Cleanup if there really was an issue messaging the user, i.e. bot blocked
            db.delete_one({'_id': docID})
            await channel.delete()
            await bot.get_channel(config.adminChannel).send(f'Failed to DM {member.mention} for modmail thread created by {moderator.mention}. Thread open action canceled')
            raise

    else:
        await member.send(f'Hi there!\nYou have opened a modmail thread with the chat moderators who oversee the **{guild.name}** Discord and they have received your message.\n\nI will send you a message when moderators respond to this thread. Every message you send to me while your thread is open will also be sent to the moderation team -- so you can message me anytime to add information or to reply to a moderator\'s message. You\'ll know your message has been sent when I react to your message with a ✅. \n\nPlease be patient for a response; if this is an urgent issue you may also ping the Chat-Mods with @Chat-Mods in a channel')
    
    return channel

async def _info(ctx, bot, user: typing.Union[discord.Member, int]):
    inServer = True
    if type(user) == int:
        # User doesn't share the ctx server, fetch it instead
        dbUser = mclient.bowser.users.find_one({'_id': user})
        inServer = False

        user = await bot.fetch_user(user)

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
    fieldValue = 'View history to get full details on all notes.\n\n'
    if noteCnt:
        noteList = []
        for x in noteDocs.sort('timestamp', pymongo.DESCENDING):
            stamp = datetime.datetime.utcfromtimestamp(x['timestamp']).strftime('`[%m/%d/%y]`')
            noteContent = f'{stamp}: {x["reason"]}'

            fieldLength = 0
            for value in noteList: fieldLength += len(value)
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
        for pun in punsCol.sort('timestamp', pymongo.DESCENDING):
            if puns >= 5:
                break

            puns += 1
            stamp = datetime.datetime.utcfromtimestamp(pun['timestamp']).strftime('%m/%d/%y %H:%M:%S UTC')
            punType = punNames[pun['type']]
            if pun['type'] in ['clear', 'unmute', 'unban', 'unblacklist']:
                punishments += f'- [{stamp}] {punType}\n'

            else:
                punishments += f'+ [{stamp}] {punType}\n'

        punishments = f'Showing {puns}/{punsCnt} punishment entries. ' \
            f'For a full history including responsible moderator, active status, and more use `{bot.command_prefix[0]}history {user.id}`' \
            f'\n```diff\n{punishments}```'
    embed.add_field(name='Punishments', value=punishments, inline=False)
    return await ctx.send(embed=embed)
