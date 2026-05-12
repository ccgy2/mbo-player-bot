import os
import re
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore


load_dotenv()

PREFIX = "!"
AUTHORIZED_USER_ID = 742989026625060914
ANNOUNCE_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
LOG_CHANNEL_ID = os.getenv("DISCORD_LOG_CHANNEL_ID")
CONFIG_REF = None
SNAPSHOT_UNSUBSCRIBES = []
SNAPSHOTS_STARTED = False

DEFAULT_ROLE_IDS = {
    "teams": {
        "RMS": "1467379371059974442",
        "NDG": "1467379373169705003",
        "CPX": "1467379373504987136",
        "IH": "1467379375954460901",
        "KRA": "1467884269669056747",
        "PLT": "1467885245016703039",
        "ODV": "1467886778194333874",
        "SLU": "1467907005229302094",
    },
    "retire": "1486690482250846350",
    "forcedRelease": "1486690539012231178",
}

TEAM_META = {
    "ODV": {"name": "오버드라이브", "color": 0xFF6A00},
    "RMS": {"name": "레이 마린스", "color": 0xC9982C},
    "NDG": {"name": "나이트 드래곤즈", "color": 0x2B2B2B},
    "CPX": {"name": "청화 피닉스", "color": 0x1B65BA},
    "IH": {"name": "아이언 호네츠", "color": 0xFEC804},
    "KRA": {"name": "크라켄즈", "color": 0xC00000},
    "PLT": {"name": "클로베츠 플랜츠", "color": 0x0CB218},
    "SLU": {"name": "브레이브 슬러거즈", "color": 0x850000},
    "무소속": {"name": "무소속", "color": 0x64748B},
}

MOVEMENT_LABELS = {
    "TRADE": "트레이드",
    "FA_SIGN": "FA 영입",
    "NICKNAME": "닉네임 변경",
    "TRANSFER": "이적",
    "RELEASE": "방출",
    "RETIRE": "은퇴",
    "FORCED_RELEASE": "임의해지",
}


def init_firebase():
    private_key = os.getenv("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n")
    cred = credentials.Certificate(
        {
            "type": "service_account",
            "project_id": os.getenv("FIREBASE_PROJECT_ID"),
            "private_key": private_key,
            "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    firebase_admin.initialize_app(cred)
    return firestore.client()


db = init_firebase()
CONFIG_REF = db.collection("appMeta").document("discordBot")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

def get_bot_config_sync():
    snapshot = CONFIG_REF.get()
    return snapshot.to_dict() if snapshot.exists else {}


async def get_bot_config():
    return await asyncio.to_thread(get_bot_config_sync)


def set_bot_config_sync(payload):
    CONFIG_REF.set({**payload, "updatedAt": firestore.SERVER_TIMESTAMP}, merge=True)


async def set_bot_config(payload):
    await asyncio.to_thread(set_bot_config_sync, payload)


def parse_channel_id(value):
    text = normalize(value)
    match = re.search(r"(\d{15,25})", text)
    if not match:
        return ""
    return match.group(1)


def parse_role_id(value):
    text = normalize(value)
    match = re.search(r"(\d{15,25})", text)
    if not match:
        return ""
    return match.group(1)


def parse_user_id(value):
    text = normalize(value)
    match = re.search(r"(\d{15,25})", text)
    if not match:
        return ""
    return match.group(1)


def merge_role_config(config):
    saved = config.get("roleIds") or {}
    saved_teams = saved.get("teams") or {}
    return {
        "teams": {**DEFAULT_ROLE_IDS["teams"], **saved_teams},
        "retire": normalize(saved.get("retire")) or DEFAULT_ROLE_IDS["retire"],
        "forcedRelease": normalize(saved.get("forcedRelease")) or DEFAULT_ROLE_IDS["forcedRelease"],
    }


async def configured_role_ids():
    return merge_role_config(await get_bot_config())


async def resolve_channel(channel_id):
    if not channel_id:
        return None
    try:
        return bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
    except Exception as exc:
        print("채널 조회 실패:", repr(exc))
        return None


async def configured_public_channel_id():
    config = await get_bot_config()
    return normalize(config.get("channelId")) or normalize(ANNOUNCE_CHANNEL_ID)


async def configured_log_channel_id():
    config = await get_bot_config()
    return normalize(config.get("logChannelId")) or normalize(LOG_CHANNEL_ID)


async def send_log(title, description="", fields=None, color=0x475569):
    channel = await resolve_channel(await configured_log_channel_id())
    if not channel:
        return
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    for name, value, inline in fields or []:
        embed.add_field(name=name, value=str(value)[:1024] or "-", inline=inline)
    await channel.send(embed=embed)


async def find_member_by_player_name(guild, player_name):
    target = normalize(player_name).lower()
    if not target:
        return None

    def is_match(member):
        values = [
            getattr(member, "display_name", ""),
            getattr(member, "name", ""),
            getattr(member, "global_name", ""),
        ]
        return any(normalize(value).lower() == target for value in values)

    member = next((item for item in guild.members if is_match(item)), None)
    if member:
        return member

    try:
        queried = await guild.query_members(query=player_name, limit=20)
    except Exception as exc:
        print("멤버 조회 실패:", repr(exc))
        return None

    return next((item for item in queried if is_match(item)), None)


def get_role(guild, role_id):
    if not role_id:
        return None
    return guild.get_role(int(role_id))


def movement_role_targets(data):
    kind = data.get("type")
    from_team = normalize(data.get("fromTeam")).upper()
    to_team = normalize(data.get("toTeam")).upper()
    from_players = names_from_value(data.get("fromPlayers")) or names_from_value(data.get("playerName"))
    to_players = names_from_value(data.get("toPlayers"))

    if kind == "TRADE":
        return [(name, to_team) for name in from_players] + [(name, from_team) for name in to_players]

    if kind in {"FA_SIGN", "TRANSFER"}:
        return [(name, to_team) for name in from_players]

    if kind == "RELEASE":
        return [(name, "") for name in from_players]

    if kind == "RETIRE":
        return [(name, "RETIRE") for name in from_players]

    if kind == "FORCED_RELEASE":
        return [(name, "FORCED_RELEASE") for name in from_players]

    return []


async def sync_member_roles_for_movement(data):
    role_ids = await configured_role_ids()
    team_role_ids = {team: normalize(role_id) for team, role_id in role_ids["teams"].items() if normalize(role_id)}
    retire_role_id = normalize(role_ids.get("retire"))
    forced_role_id = normalize(role_ids.get("forcedRelease"))
    managed_role_ids = set(team_role_ids.values()) | {retire_role_id, forced_role_id}
    managed_role_ids.discard("")
    targets = movement_role_targets(data)

    if not targets or not managed_role_ids:
        return []

    results = []

    for guild in bot.guilds:
        guild_role_ids = {str(role.id) for role in guild.roles}
        if not managed_role_ids.intersection(guild_role_ids):
            continue

        for player_name, target_key in targets:
            member = await find_member_by_player_name(guild, player_name)
            if not member:
                results.append(f"{guild.name} / {player_name}: 멤버를 찾지 못함")
                continue

            remove_roles = [role for role in member.roles if str(role.id) in managed_role_ids]
            add_role = None
            target_label = target_key or "역할 제거"
            if target_key == "RETIRE":
                add_role = get_role(guild, retire_role_id)
                target_label = "은퇴"
            elif target_key == "FORCED_RELEASE":
                add_role = get_role(guild, forced_role_id)
                target_label = "임의탈퇴"
            elif target_key:
                add_role = get_role(guild, team_role_ids.get(target_key))

            if target_key and not add_role:
                results.append(f"{guild.name} / {player_name}: {target_label} 역할을 찾지 못함")
                continue

            try:
                if remove_roles:
                    await member.remove_roles(*remove_roles, reason="MBO 선수 이동 역할 동기화")
                if add_role and add_role not in member.roles:
                    await member.add_roles(add_role, reason="MBO 선수 이동 역할 동기화")
                results.append(f"{guild.name} / {player_name}: {target_label}")
            except discord.Forbidden:
                results.append(f"{guild.name} / {player_name}: 권한 부족")
            except Exception as exc:
                results.append(f"{guild.name} / {player_name}: {exc}")

    if results:
        await send_log(
            "Discord 역할 동기화",
            "\n".join(results)[:4000],
            [("이동 유형", MOVEMENT_LABELS.get(data.get("type"), data.get("type", "-")), True)],
            0x475569,
        )
    return results


async def sync_roles_for_discord_movement(kind, from_team, players, to_team, date, to_players=None):
    return await sync_member_roles_for_movement(
        {
            "type": kind,
            "playerName": ", ".join(players),
            "fromPlayers": players,
            "toPlayers": to_players or [],
            "fromTeam": from_team,
            "toTeam": to_team,
            "date": date,
            "createdBy": "discord-python-bot",
            "createdByName": "Discord Python Bot",
        }
    )


def add_role_sync_result(embed, results):
    problems = [
        result
        for result in results or []
        if any(pattern in result for pattern in ["멤버를 찾지 못함", "권한 부족", "역할을 찾지 못함"])
    ]
    if problems:
        embed.add_field(name="역할 처리 확인", value="\n".join(problems)[:1024], inline=False)


async def sync_roles_for_player_registration(name, team):
    if team == "무소속":
        return []
    return await sync_member_roles_for_movement(
        {
            "type": "FA_SIGN",
            "playerName": name,
            "fromPlayers": [name],
            "toPlayers": [],
            "fromTeam": "무소속",
            "toTeam": team,
            "date": today(),
            "createdBy": "discord-python-bot",
            "createdByName": "Discord Python Bot",
        }
    )


def normalize(value):
    return str(value or "").strip()


def today():
    return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


def is_date(value):
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalize(value)))


def parse_names(value):
    text = normalize(value)
    text = text.replace("，", ",")
    return [item.strip() for item in re.split(r"[\n\r,]+", text) if item.strip()]


def names_from_value(value):
    if isinstance(value, list):
        return [normalize(item) for item in value if normalize(item)]
    return parse_names(value)


def team_color(team):
    return TEAM_META.get(team, {}).get("color", 0x0F766E)


def is_authorized(ctx):
    if ctx.author.id == AUTHORIZED_USER_ID:
        return True
    if getattr(ctx.author.guild_permissions, "administrator", False):
        return True
    return any(role.name == "관리자" for role in getattr(ctx.author, "roles", []))


def is_authorized_member(member):
    if not member:
        return False
    if member.id == AUTHORIZED_USER_ID:
        return True
    if getattr(member.guild_permissions, "administrator", False):
        return True
    return any(role.name == "관리자" for role in getattr(member, "roles", []))


async def guard(ctx):
    if is_authorized(ctx):
        return True
    await ctx.reply("이 명령어는 Discord 관리자 또는 지정된 관리자만 사용할 수 있습니다.")
    return False


async def run_blocking(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def find_player_sync(name, team=""):
    docs = db.collection("players").where("name", "==", name).stream()
    matches = [{"id": doc.id, **doc.to_dict()} for doc in docs]
    if not team:
        return matches[0] if matches else None
    lowered_team = team.lower()
    return next((player for player in matches if normalize(player.get("team")).lower() == lowered_team), None)


async def find_player(name, team=""):
    return await run_blocking(find_player_sync, name, team)


async def get_required_player(name, team):
    player = await find_player(name, team)
    if not player:
        raise ValueError(f"선수 목록에 없는 선수입니다: {name} ({team})")
    return player


def validate_forced_return(player, target_team):
    original = normalize(player.get("forcedReleaseOriginalTeam"))
    if original and original.lower() != normalize(target_team).lower():
        raise ValueError(f"{player.get('name')} 선수는 임의해지 상태라 원래 팀({original})으로만 복귀할 수 있습니다.")


async def send_announcement(embed):
    channel = await resolve_channel(await configured_public_channel_id())
    if channel:
        await channel.send(embed=embed)


def movement_target_label(kind, to_team):
    if kind == "RELEASE":
        return "방출"
    if kind == "RETIRE":
        return "은퇴"
    if kind == "FORCED_RELEASE":
        return "임의탈퇴"
    return to_team or "-"


def movement_embed(kind, date, from_team, players, to_team="", to_players=None, reason=""):
    label = MOVEMENT_LABELS.get(kind, kind)
    to_players = to_players or []
    embed = discord.Embed(title=f"🔄 {label} 승인", color=team_color(from_team), timestamp=datetime.now(timezone.utc))

    if kind == "TRADE" and to_players:
        player_lines = players + to_players
        from_lines = [from_team] * len(players) + [to_team] * len(to_players)
        to_lines = [to_team] * len(players) + [from_team] * len(to_players)
    else:
        player_lines = players
        from_lines = [from_team] * len(players)
        to_lines = [movement_target_label(kind, to_team)] * len(players)

    embed.add_field(name="선수", value="\n".join(player_lines) or "-", inline=True)
    embed.add_field(name="이전소속", value="\n".join(from_lines) or "-", inline=True)
    embed.add_field(name="신규 소속", value="\n".join(to_lines) or "-", inline=True)
    if normalize(reason):
        embed.add_field(name="사유", value=normalize(reason)[:1024], inline=False)
    embed.set_footer(text="MBOMgr System (승인됨)")
    return embed


def movement_request_embed(request_no, payload):
    kind = payload.get("kind")
    label = MOVEMENT_LABELS.get(kind, kind)
    embed = discord.Embed(
        title=f"⏳ {label} 승인 대기",
        color=0xF59E0B,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="요청번호", value=request_no, inline=True)
    embed.add_field(name="요청자", value=payload.get("requesterName", "-"), inline=True)
    embed.add_field(name="처리일", value=payload.get("date", "-"), inline=True)

    if kind == "TRADE":
        from_players = payload.get("fromPlayers", [])
        to_players = payload.get("toPlayers", [])
        embed.add_field(name="선수", value="\n".join(from_players + to_players) or "-", inline=True)
        embed.add_field(
            name="이전소속",
            value="\n".join([payload.get("fromTeam", "-")] * len(from_players) + [payload.get("toTeam", "-")] * len(to_players)) or "-",
            inline=True,
        )
        embed.add_field(
            name="신규 소속",
            value="\n".join([payload.get("toTeam", "-")] * len(from_players) + [payload.get("fromTeam", "-")] * len(to_players)) or "-",
            inline=True,
        )
    else:
        players = payload.get("players", [])
        embed.add_field(name="선수", value="\n".join(players) or "-", inline=True)
        embed.add_field(name="이전소속", value=payload.get("fromTeam", "-"), inline=True)
        embed.add_field(name="신규 소속", value=movement_target_label(kind, payload.get("toTeam", "")), inline=True)

    if normalize(payload.get("reason")):
        embed.add_field(name="사유", value=normalize(payload.get("reason"))[:1024], inline=False)
    embed.set_footer(text=f"!승인 @관리자 {request_no}")
    return embed


def add_movement(batch, kind, player_name, from_team, to_team, date, note, from_players=None, to_players=None):
    ref = db.collection("movements").document()
    batch.set(
        ref,
        {
            "type": kind,
            "playerName": player_name,
            "fromPlayers": from_players or [player_name],
            "toPlayers": to_players or [],
            "fromTeam": from_team,
            "toTeam": to_team,
            "date": date,
            "note": note,
            "createdBy": "discord-python-bot",
            "createdByName": "Discord Python Bot",
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
    )


def next_request_number_sync(request_date):
    snapshot = CONFIG_REF.get()
    config = snapshot.to_dict() if snapshot.exists else {}
    if normalize(config.get("requestCounterDate")) != request_date:
        next_value = 1
    else:
        next_value = int(config.get("requestCounterValue") or 0) + 1

    CONFIG_REF.set(
        {
            "requestCounterDate": request_date,
            "requestCounterValue": next_value,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )
    return f"{next_value:04d}"


def create_movement_request_sync(payload):
    request_date = today()
    request_no = next_request_number_sync(request_date)
    request_id = f"{request_date}-{request_no}"
    db.collection("movementRequests").document(request_id).set(
        {
            "requestNo": request_no,
            "requestDate": request_date,
            "status": "PENDING",
            "payload": payload,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }
    )
    return request_no


def fetch_pending_request_sync(request_no):
    request_id = f"{today()}-{request_no}"
    snapshot = db.collection("movementRequests").document(request_id).get()
    if not snapshot.exists:
        raise ValueError(f"오늘 날짜의 승인 대기 요청을 찾지 못했습니다: {request_no}")

    data = snapshot.to_dict()
    if data.get("status") != "PENDING":
        raise ValueError(f"이미 처리된 요청입니다: {request_no}")
    return request_id, data


def mark_request_approved_sync(request_id, approver_text):
    db.collection("movementRequests").document(request_id).set(
        {
            "status": "APPROVED",
            "approvedBy": approver_text,
            "approvedAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )


def update_player(batch, player, payload):
    batch.update(db.collection("players").document(player["id"]), {**payload, "updatedAt": firestore.SERVER_TIMESTAMP})


def split_date_from_args(args):
    text = normalize(args)
    parts = text.split()

    if parts and is_date(parts[-1]):
        date = parts[-1]
        body = " ".join(parts[:-1]).strip()
        return body, date

    return text, today()


def parse_trade_args(args):
    body, date = split_date_from_args(args)
    parts = body.split()

    if len(parts) < 4:
        raise ValueError("사용법: `!트레이드 <이전팀> <보내는선수> <새팀> <받는선수들> [날짜]`")

    from_team = parts[0].upper()
    from_players_text = parts[1]
    to_team = parts[2].upper()
    to_players_text = " ".join(parts[3:])

    from_players = parse_names(from_players_text)
    to_players = parse_names(to_players_text)

    if not from_players:
        raise ValueError("보내는 선수를 찾지 못했습니다.")

    if not to_players:
        raise ValueError("받는 선수를 찾지 못했습니다.")

    return from_team, from_players, to_team, to_players, date


def parse_players_team_args(args, usage):
    body, date = split_date_from_args(args)
    parts = body.split()

    if len(parts) < 2:
        raise ValueError(usage)

    if parts[0].upper() in TEAM_META:
        team = parts[0].upper()
        players_text = " ".join(parts[1:])
    else:
        team_index = next((index for index, part in enumerate(parts[1:], start=1) if part.upper() in TEAM_META), -1)
        if team_index < 0:
            team = parts[-1].upper()
            players_text = " ".join(parts[:-1])
            reason = ""
        else:
            team = parts[team_index].upper()
            players_text = " ".join(parts[:team_index])
            reason = " ".join(parts[team_index + 1 :])

    if team not in TEAM_META or team == "무소속":
        raise ValueError(f"팀 코드를 확인해주세요: {team}")

    if parts[0].upper() in TEAM_META:
        reason = ""

    players = parse_names(players_text)

    if not players:
        raise ValueError("선수를 찾지 못했습니다.")

    return team, players, date, normalize(reason)


def parse_simple_movement_args(args):
    return parse_players_team_args(args, "사용법: `!<방출|은퇴|임의해지> <선수명들> <팀> [사유] [날짜]`")


def parse_fa_sign_args(args):
    return parse_players_team_args(args, "사용법: `!영입 <선수명들> <팀> [사유] [날짜]`")


def parse_register_args(args):
    parts = normalize(args).split()
    if len(parts) < 2:
        raise ValueError("사용법: `!등록 <닉네임> <팀>`")

    team = parts[-1].upper()
    name = " ".join(parts[:-1]).strip()

    if team not in TEAM_META:
        raise ValueError(f"팀 코드를 확인해주세요: {team}")

    if not name:
        raise ValueError("등록할 닉네임을 입력해주세요.")

    return name, team


def commit_player_registration_sync(name, team, author_text):
    existing = find_player_sync(name, team)
    if existing:
        raise ValueError(f"이미 등록된 선수입니다: {name} ({team})")

    db.collection("players").add(
        {
            "name": name,
            "team": team,
            "position": "",
            "number": "",
            "transfer": f"{today()} Discord 봇 등록: {team}",
            "createdBy": "discord-python-bot",
            "createdByName": author_text,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }
    )


def movement_note(author_text, reason=""):
    reason = normalize(reason)
    if reason:
        return f"Discord 봇 입력: {author_text}\n사유: {reason}"
    return f"Discord 봇 입력: {author_text}"


async def create_movement_request(ctx, payload):
    request_no = await run_blocking(create_movement_request_sync, payload)
    embed = movement_request_embed(request_no, payload)
    await ctx.reply(embed=embed)
    return request_no


def approve_movement_request_sync(request_id, payload, approver_text):
    kind = payload.get("kind")

    if kind == "TRADE":
        commit_trade_sync(
            payload.get("fromTeam"),
            payload.get("fromPlayers", []),
            payload.get("toTeam"),
            payload.get("toPlayers", []),
            payload.get("date"),
            approver_text,
        )
    elif kind == "FA_SIGN":
        commit_fa_sign_sync(
            payload.get("players", []),
            payload.get("toTeam"),
            payload.get("date"),
            approver_text,
            payload.get("reason", ""),
        )
    elif kind in {"RELEASE", "RETIRE", "FORCED_RELEASE"}:
        commit_simple_movement_sync(
            kind,
            payload.get("fromTeam"),
            payload.get("players", []),
            payload.get("date"),
            approver_text,
            payload.get("reason", ""),
        )
    else:
        raise ValueError(f"승인할 수 없는 이동 유형입니다: {kind}")

    mark_request_approved_sync(request_id, approver_text)


def commit_simple_movement_sync(kind, team, players, date, author_text, reason=""):
    from_team = team.upper()
    batch = db.batch()

    for name in players:
        docs = db.collection("players").where("name", "==", name).stream()
        matches = [{"id": doc.id, **doc.to_dict()} for doc in docs]
        player = next((p for p in matches if normalize(p.get("team")).lower() == from_team.lower()), None)

        if not player:
            raise ValueError(f"선수 목록에 없는 선수입니다: {name} ({from_team})")

        payload = {
            "team": "무소속",
            "transfer": f"{date} {from_team}에서 {MOVEMENT_LABELS[kind]}",
        }

        if kind == "FORCED_RELEASE":
            payload["forcedReleaseOriginalTeam"] = from_team
        else:
            payload["forcedReleaseOriginalTeam"] = ""

        update_player(batch, player, payload)
        add_movement(batch, kind, name, from_team, "무소속", date, movement_note(author_text, reason), [name], [])

    batch.commit()


def find_fa_sign_player_sync(name):
    docs = db.collection("players").where("name", "==", name).stream()
    matches = [{"id": doc.id, **doc.to_dict()} for doc in docs]

    if not matches:
        raise ValueError(f"선수 목록에 없는 선수입니다: {name}")

    free_agents = [player for player in matches if normalize(player.get("team")) in {"", "무소속"}]
    if free_agents:
        return free_agents[0]

    teams = ", ".join(normalize(player.get("team")) or "팀 미정" for player in matches)
    raise ValueError(f"FA 영입은 무소속 선수만 가능합니다: {name} (현재 소속: {teams})")


def commit_fa_sign_sync(players, to_team, date, author_text, reason=""):
    to_team = to_team.upper()
    batch = db.batch()

    for name in players:
        player = find_fa_sign_player_sync(name)
        from_team = normalize(player.get("team")) or "무소속"
        validate_forced_return(player, to_team)

        update_player(
            batch,
            player,
            {
                "team": to_team,
                "forcedReleaseOriginalTeam": "",
                "transfer": f"{date} FA 영입: {to_team}",
            },
        )
        add_movement(batch, "FA_SIGN", name, from_team, to_team, date, movement_note(author_text, reason), [name], [])

    batch.commit()


def commit_trade_sync(from_team, from_players, to_team, to_players, date, author_text):
    from_team = from_team.upper()
    to_team = to_team.upper()

    from_records = []
    to_records = []

    for name in from_players:
        docs = db.collection("players").where("name", "==", name).stream()
        matches = [{"id": doc.id, **doc.to_dict()} for doc in docs]
        player = next((p for p in matches if normalize(p.get("team")).lower() == from_team.lower()), None)

        if not player:
            raise ValueError(f"선수 목록에 없는 선수입니다: {name} ({from_team})")

        from_records.append(player)

    for name in to_players:
        docs = db.collection("players").where("name", "==", name).stream()
        matches = [{"id": doc.id, **doc.to_dict()} for doc in docs]
        player = next((p for p in matches if normalize(p.get("team")).lower() == to_team.lower()), None)

        if not player:
            raise ValueError(f"선수 목록에 없는 선수입니다: {name} ({to_team})")

        to_records.append(player)

    for player in from_records:
        validate_forced_return(player, to_team)

    for player in to_records:
        validate_forced_return(player, from_team)

    batch = db.batch()

    for player in from_records:
        update_player(
            batch,
            player,
            {
                "team": to_team,
                "forcedReleaseOriginalTeam": "",
                "transfer": f"{date} {from_team}에서 {to_team}으로 트레이드",
            },
        )

    for player in to_records:
        update_player(
            batch,
            player,
            {
                "team": from_team,
                "forcedReleaseOriginalTeam": "",
                "transfer": f"{date} {to_team}에서 {from_team}으로 트레이드",
            },
        )

    add_movement(
        batch,
        "TRADE",
        f"{', '.join(from_players)} ↔ {', '.join(to_players)}",
        from_team,
        to_team,
        date,
        f"Discord Python 봇 입력: {author_text}",
        from_players,
        to_players,
    )

    batch.commit()


def fetch_recent_movements_sync():
    docs = db.collection("movements").order_by("date", direction=firestore.Query.DESCENDING).limit(5).stream()
    return [doc.to_dict() for doc in docs]




@bot.before_invoke
async def log_discord_command(ctx):
    if is_authorized(ctx):
        await send_log(
            "Discord 명령어 실행",
            f"{ctx.author} 님이 명령어를 실행했습니다.",
            [("실행자", f"{ctx.author} ({ctx.author.id})", False), ("명령어", ctx.message.content[:1000], False), ("채널", ctx.channel.mention, True)],
            0x475569,
        )

@bot.command(name="채널")
async def set_public_channel_command(ctx, action: str = "", channel_text: str = ""):
    if not await guard(ctx):
        return
    if normalize(action) != "설정":
        await ctx.reply("사용법: `!채널 설정 <#채널>`")
        return
    channel_id = parse_channel_id(channel_text)
    if not channel_id:
        await ctx.reply("설정할 채널을 멘션해주세요. 예: `!채널 설정 #공지채널`")
        return
    await set_bot_config({"channelId": channel_id})
    await ctx.reply(f"웹/Discord 공지 채널을 <#{channel_id}> 로 설정했습니다.")
    await send_log(
        "공지 채널 설정",
        f"{ctx.author} 님이 공지 채널을 <#{channel_id}> 로 설정했습니다.",
        [("실행자", f"{ctx.author} ({ctx.author.id})", False), ("채널", f"<#{channel_id}>", False)],
        0x0F766E,
    )


@bot.command(name="로그채널")
async def set_log_channel_command(ctx, action: str = "", channel_text: str = ""):
    if not await guard(ctx):
        return
    if normalize(action) != "설정":
        await ctx.reply("사용법: `!로그채널 설정 <#채널>`")
        return
    channel_id = parse_channel_id(channel_text)
    if not channel_id:
        await ctx.reply("설정할 채널을 멘션해주세요. 예: `!로그채널 설정 #로그채널`")
        return
    await set_bot_config({"logChannelId": channel_id})
    await ctx.reply(f"로그 채널을 <#{channel_id}> 로 설정했습니다.")
    await send_log(
        "로그 채널 설정",
        f"{ctx.author} 님이 로그 채널을 <#{channel_id}> 로 설정했습니다.",
        [("실행자", f"{ctx.author} ({ctx.author.id})", False), ("채널", f"<#{channel_id}>", False)],
        0x0F766E,
    )


@bot.command(name="역할")
async def set_role_command(ctx, category: str = "", team_or_role: str = "", role_text: str = ""):
    if not await guard(ctx):
        return

    category = normalize(category)
    role_ids = await configured_role_ids()

    if category in {"목록", "리스트"}:
        lines = [f"{team}: <@&{role_id}>" for team, role_id in sorted(role_ids["teams"].items())]
        lines.append(f"은퇴: <@&{role_ids['retire']}>")
        lines.append(f"임의탈퇴/임의해지: <@&{role_ids['forcedRelease']}>")
        await ctx.reply("현재 역할 설정입니다.\n" + "\n".join(lines))
        return

    if category == "팀":
        team = normalize(team_or_role).upper()
        role_id = parse_role_id(role_text)
        if team not in TEAM_META or team == "무소속":
            await ctx.reply("팀 코드를 확인해주세요. 예: `!역할 팀 RMS @역할`")
            return
        if not role_id:
            await ctx.reply("역할을 멘션해주세요. 예: `!역할 팀 RMS @역할`")
            return
        role_ids["teams"][team] = role_id
        await set_bot_config({"roleIds": role_ids})
        await ctx.reply(f"{team} 역할을 <@&{role_id}> 로 설정했습니다.")
        await send_log("팀 역할 설정", f"{ctx.author} 님이 {team} 역할을 <@&{role_id}> 로 설정했습니다.", [], 0x0F766E)
        return

    if category.upper() in TEAM_META and category != "무소속":
        team = category.upper()
        role_id = parse_role_id(team_or_role)
        if not role_id:
            await ctx.reply("역할을 멘션해주세요. 예: `!역할 RMS @역할`")
            return
        role_ids["teams"][team] = role_id
        await set_bot_config({"roleIds": role_ids})
        await ctx.reply(f"{team} 역할을 <@&{role_id}> 로 설정했습니다.")
        await send_log("팀 역할 설정", f"{ctx.author} 님이 {team} 역할을 <@&{role_id}> 로 설정했습니다.", [], 0x0F766E)
        return

    special_map = {
        "은퇴": "retire",
        "임의탈퇴": "forcedRelease",
        "임의해지": "forcedRelease",
    }
    if category in special_map:
        role_id = parse_role_id(team_or_role)
        if not role_id:
            await ctx.reply(f"역할을 멘션해주세요. 예: `!역할 {category} @역할`")
            return
        key = special_map[category]
        role_ids[key] = role_id
        await set_bot_config({"roleIds": role_ids})
        await ctx.reply(f"{category} 역할을 <@&{role_id}> 로 설정했습니다.")
        await send_log("상태 역할 설정", f"{ctx.author} 님이 {category} 역할을 <@&{role_id}> 로 설정했습니다.", [], 0x0F766E)
        return

    await ctx.reply(
        "사용법: `!역할 팀 <팀코드> <@역할>`, `!역할 은퇴 <@역할>`, "
        "`!역할 임의탈퇴 <@역할>`, `!역할 목록`"
    )


@bot.command(name="역할진단")
async def role_diagnosis_command(ctx, *, player_name: str = ""):
    if not await guard(ctx):
        return

    player_name = normalize(player_name)
    if not player_name:
        await ctx.reply("사용법: `!역할진단 <선수명>`")
        return

    role_ids = await configured_role_ids()
    managed_role_ids = set(role_ids["teams"].values()) | {role_ids["retire"], role_ids["forcedRelease"]}
    managed_role_ids.discard("")
    lines = []

    for guild in bot.guilds:
        member = await find_member_by_player_name(guild, player_name)
        if not member:
            lines.append(f"{guild.name}: `{player_name}`와 정확히 일치하는 멤버를 찾지 못했습니다.")
            continue

        bot_member = guild.me or guild.get_member(bot.user.id)
        highest = bot_member.top_role.name if bot_member else "확인 불가"
        member_roles = [role.name for role in member.roles if str(role.id) in managed_role_ids]
        lines.append(
            f"{guild.name}: {member.mention}\n"
            f"관리 역할: {', '.join(member_roles) if member_roles else '없음'}\n"
            f"봇 최고 역할: {highest}"
        )

    await ctx.reply("\n\n".join(lines)[:1900] if lines else "봇이 들어가 있는 서버를 찾지 못했습니다.")


@bot.command(name="등록")
async def register_player_command(ctx, *, args: str = ""):
    if not await guard(ctx):
        return

    name, team = parse_register_args(args)

    await run_blocking(
        commit_player_registration_sync,
        name,
        team,
        str(ctx.author),
    )
    role_results = await sync_roles_for_player_registration(name, team)

    embed = player_event_embed(
        {
            "name": name,
            "team": team,
            "position": "",
            "number": "",
        }
    )
    embed.title = "로스터 등록 승인"
    embed.set_footer(text="MBOMgr System (승인됨)")
    add_role_sync_result(embed, role_results)
    await ctx.reply(embed=embed)


@bot.command(name="승인")
async def approve_request_command(ctx, admin_text: str = "", request_no: str = ""):
    if not await guard(ctx):
        return

    admin_id = parse_user_id(admin_text)
    request_no = normalize(request_no)
    if not admin_id or not request_no:
        await ctx.reply("사용법: `!승인 <@관리자디스코드> <요청번호>`")
        return

    if admin_id != str(ctx.author.id):
        await ctx.reply("최종 승인자는 명령을 실행한 관리자 본인을 멘션해주세요.")
        return

    approver = ctx.guild.get_member(int(admin_id)) if ctx.guild else None
    if not is_authorized_member(approver):
        await ctx.reply("멘션한 사용자는 승인 권한이 없습니다.")
        return

    request_id, request_data = await run_blocking(fetch_pending_request_sync, request_no)
    payload = request_data.get("payload") or {}

    await run_blocking(
        approve_movement_request_sync,
        request_id,
        payload,
        str(ctx.author),
    )

    kind = payload.get("kind")
    if kind == "TRADE":
        role_results = await sync_roles_for_discord_movement(
            "TRADE",
            payload.get("fromTeam"),
            payload.get("fromPlayers", []),
            payload.get("toTeam"),
            payload.get("date"),
            payload.get("toPlayers", []),
        )
        embed = movement_embed(
            "TRADE",
            payload.get("date"),
            payload.get("fromTeam"),
            payload.get("fromPlayers", []),
            payload.get("toTeam"),
            payload.get("toPlayers", []),
        )
    else:
        players = payload.get("players", [])
        to_team = payload.get("toTeam", "무소속")
        from_team = payload.get("fromTeam", "무소속")
        role_results = await sync_roles_for_discord_movement(kind, from_team, players, to_team, payload.get("date"))
        embed = movement_embed(kind, payload.get("date"), from_team, players, to_team, reason=payload.get("reason", ""))

    embed.add_field(name="요청번호", value=request_no, inline=True)
    embed.add_field(name="승인자", value=ctx.author.mention, inline=True)
    add_role_sync_result(embed, role_results)
    await ctx.reply(embed=embed)
    await send_announcement(embed)


@bot.command(name="도움말")
async def help_command(ctx):
    if not await guard(ctx):
        return

    embed = discord.Embed(title="MBO Python 봇 명령어", color=0x0F766E)

    embed.add_field(
        name="!채널 설정 <#채널>",
        value="웹/Discord에서 발생한 선수 이동, 로스터 등록 등 공지를 보낼 채널을 설정합니다.",
        inline=False,
    )

    embed.add_field(
        name="!로그채널 설정 <#채널>",
        value="누가 언제 웹 또는 Discord에서 작업했는지 기록할 로그 채널을 설정합니다.",
        inline=False,
    )

    embed.add_field(
        name="!역할 팀 <팀코드> <@역할>",
        value="팀별 Discord 역할을 설정합니다. 예: `!역할 팀 RMS @RMS`",
        inline=False,
    )

    embed.add_field(
        name="!역할 은퇴 <@역할> / !역할 임의탈퇴 <@역할>",
        value="은퇴, 임의탈퇴 상태 역할을 설정합니다. 현재 설정은 `!역할 목록`으로 확인합니다.",
        inline=False,
    )

    embed.add_field(
        name="!역할진단 <선수명>",
        value="선수명과 일치하는 Discord 멤버, 현재 관리 역할, 봇 역할 위치를 확인합니다.",
        inline=False,
    )

    embed.add_field(
        name="!등록 <닉네임> <팀>",
        value="선수를 로스터에 등록하고, 닉네임과 일치하는 Discord 멤버에게 팀 역할을 지급합니다.",
        inline=False,
    )

    embed.add_field(
        name="!승인 <@관리자디스코드> <요청번호>",
        value="승인 대기 이동 요청을 최종 승인합니다. 승인 전에는 로스터가 변경되지 않습니다.",
        inline=False,
    )


    embed.add_field(
        name="!트레이드 <이전팀> <보내는선수> <새팀> <받는선수들> [날짜]",
        value="트레이드 승인 요청을 만듭니다. 받는 선수는 쉼표로 여러 명 입력할 수 있습니다.",
        inline=False,
    )

    embed.add_field(
        name="!영입 <선수명들> <팀> [사유] [날짜]",
        value="FA 영입 승인 요청을 만듭니다. 여러 명은 쉼표로 구분합니다.",
        inline=False,
    )

    embed.add_field(
        name="!방출 <선수명들> <팀> [사유] [날짜]",
        value="방출 승인 요청을 만듭니다. 여러 명은 쉼표로 구분합니다.",
        inline=False,
    )

    embed.add_field(
        name="!은퇴 <선수명들> <팀> [사유] [날짜]",
        value="은퇴 승인 요청을 만듭니다.",
        inline=False,
    )

    embed.add_field(
        name="!임의해지 <선수명들> <팀> [사유] [날짜]",
        value="임의해지 승인 요청을 만듭니다.",
        inline=False,
    )

    embed.add_field(
        name="!최근이동",
        value="최근 이동 내역 5건을 조회합니다.",
        inline=False,
    )

    embed.add_field(
        name="!최근 이동",
        value="최근 이동 내역 5건을 조회합니다.",
        inline=False,
    )

    embed.add_field(
        name="!유효성검사 <타순표>",
        value="타순표 닉네임이 로스터에 있는지 검사합니다. 여러 줄 입력 가능.",
        inline=False,
    )

    embed.add_field(
        name="트레이드 예시",
        value="```!트레이드 CPX papaya_yaru ODV KR_Windy, chan_seu1_12 2026-05-05```",
        inline=False,
    )

    embed.add_field(
        name="영입/방출 예시",
        value="```!영입 PlayerName RMS 테스트 참가\n!방출 PlayerName RMS 개인 사정\n!승인 @관리자 0001```",
        inline=False,
    )

    embed.add_field(
        name="타순표 예시",
        value="```!유효성검사\n1. Axrq__ CF\n2번 CUCCl 1B\n3. _w0nyu1 LF```",
        inline=False,
    )

    await ctx.reply(embed=embed)


@bot.command(name="최근이동", aliases=["최근"])
async def recent_movements(ctx, *unused):
    if not await guard(ctx):
        return

    records = await run_blocking(fetch_recent_movements_sync)

    embed = discord.Embed(title="최근 이동 내역", color=0x0F766E, timestamp=datetime.now(timezone.utc))

    if not records:
        embed.description = "등록된 이동 내역이 없습니다."
        await ctx.reply(embed=embed)
        return

    for data in records:
        label = MOVEMENT_LABELS.get(data.get("type"), data.get("type", "이동"))
        route = f"{data.get('fromTeam', '-')} -> {data.get('toTeam', '-')}"

        if data.get("type") == "RELEASE":
            route = f"{data.get('fromTeam', '-')} -> 방출"
        elif data.get("type") == "RETIRE":
            route = f"{data.get('fromTeam', '-')} -> 은퇴"
        elif data.get("type") == "FORCED_RELEASE":
            route = f"{data.get('fromTeam', '-')} -> 임의해지"

        embed.add_field(
            name=f"{data.get('date', '-')} · {label}",
            value=f"{data.get('playerName', '-')}\n{route}",
            inline=False,
        )

    await ctx.reply(embed=embed)


@bot.command(name="이동")
async def legacy_movement(ctx, movement_type: str = "", *, args: str = ""):
    movement_type = normalize(movement_type)

    if movement_type == "트레이드":
        from_team, from_players, to_team, to_players, date = parse_trade_args(args)
        await create_movement_request(
            ctx,
            {
                "kind": "TRADE",
                "fromTeam": from_team,
                "fromPlayers": from_players,
                "toTeam": to_team,
                "toPlayers": to_players,
                "date": date,
                "requesterId": str(ctx.author.id),
                "requesterName": str(ctx.author),
            },
        )
        return

    if movement_type in {"영입", "FA영입", "FA_SIGN", "FA"}:
        to_team, players, date, reason = parse_fa_sign_args(args)
        await create_movement_request(
            ctx,
            {
                "kind": "FA_SIGN",
                "fromTeam": "무소속",
                "players": players,
                "toTeam": to_team,
                "date": date,
                "reason": reason,
                "requesterId": str(ctx.author.id),
                "requesterName": str(ctx.author),
            },
        )
        return

    if movement_type == "방출":
        team, players, date, reason = parse_simple_movement_args(args)
        await create_movement_request(
            ctx,
            {
                "kind": "RELEASE",
                "fromTeam": team,
                "players": players,
                "toTeam": "무소속",
                "date": date,
                "reason": reason,
                "requesterId": str(ctx.author.id),
                "requesterName": str(ctx.author),
            },
        )
        return

    if movement_type == "은퇴":
        team, players, date, reason = parse_simple_movement_args(args)
        await create_movement_request(
            ctx,
            {
                "kind": "RETIRE",
                "fromTeam": team,
                "players": players,
                "toTeam": "무소속",
                "date": date,
                "reason": reason,
                "requesterId": str(ctx.author.id),
                "requesterName": str(ctx.author),
            },
        )
        return

    if movement_type in {"임의해지", "임의탈퇴"}:
        team, players, date, reason = parse_simple_movement_args(args)
        await create_movement_request(
            ctx,
            {
                "kind": "FORCED_RELEASE",
                "fromTeam": team,
                "players": players,
                "toTeam": "무소속",
                "date": date,
                "reason": reason,
                "requesterId": str(ctx.author.id),
                "requesterName": str(ctx.author),
            },
        )
        return

    await ctx.reply("사용법: `!<트레이드|영입|방출|은퇴|임의해지> ...` 또는 `!도움말`")


@bot.command(name="트레이드")
async def trade(ctx, *, args: str = ""):
    from_team, from_players, to_team, to_players, date = parse_trade_args(args)
    await create_movement_request(
        ctx,
        {
            "kind": "TRADE",
            "fromTeam": from_team,
            "fromPlayers": from_players,
            "toTeam": to_team,
            "toPlayers": to_players,
            "date": date,
            "requesterId": str(ctx.author.id),
            "requesterName": str(ctx.author),
        },
    )


@bot.command(name="영입", aliases=["FA영입", "FA"])
async def fa_sign(ctx, *, args: str = ""):
    to_team, players, date, reason = parse_fa_sign_args(args)
    await create_movement_request(
        ctx,
        {
            "kind": "FA_SIGN",
            "fromTeam": "무소속",
            "players": players,
            "toTeam": to_team,
            "date": date,
            "reason": reason,
            "requesterId": str(ctx.author.id),
            "requesterName": str(ctx.author),
        },
    )


@bot.command(name="방출")
async def release(ctx, *, args: str = ""):
    await simple_movement(ctx, "RELEASE", args)


@bot.command(name="은퇴")
async def retire(ctx, *, args: str = ""):
    await simple_movement(ctx, "RETIRE", args)


@bot.command(name="임의해지", aliases=["임의탈퇴"])
async def forced_release(ctx, *, args: str = ""):
    await simple_movement(ctx, "FORCED_RELEASE", args)


async def simple_movement(ctx, kind, args):
    team, players, date, reason = parse_simple_movement_args(args)
    await create_movement_request(
        ctx,
        {
            "kind": kind,
            "fromTeam": team,
            "players": players,
            "toTeam": "무소속",
            "date": date,
            "reason": reason,
            "requesterId": str(ctx.author.id),
            "requesterName": str(ctx.author),
        },
    )


def parse_lineup_line(line):
    cleaned = normalize(line)
    cleaned = re.sub(r"^\d+\s*(?:번|[.)])\s*", "", cleaned)

    if not cleaned:
        return None

    parts = cleaned.split()

    if not parts:
        return None

    name = parts[0]
    position = next((token for token in parts[1:] if "교체" not in token), "")

    return {"name": name, "position": position}


@bot.command(name="유효성검사")
async def validate_roster(ctx, *, lineup_text: str = ""):
    if not await guard(ctx):
        return

    if not lineup_text:
        await ctx.reply("사용법: `!유효성검사 <타순표>`\n여러 줄 타순표도 그대로 붙여넣을 수 있습니다.")
        return

    entries = [entry for entry in (parse_lineup_line(line) for line in lineup_text.splitlines()) if entry]

    if not entries:
        await ctx.reply("검사할 선수를 찾지 못했습니다.")
        return

    found = []
    missing = []

    for entry in entries:
        player = await find_player(entry["name"])

        if player:
            found.append(f"{entry['name']} {entry['position']} · {player.get('team', '팀 미정')}")
        else:
            missing.append(f"{entry['name']} {entry['position']}".strip())

    embed = discord.Embed(title="로스터 유효성 검사", color=0x0F766E)
    embed.add_field(name=f"등록됨 ({len(found)})", value="\n".join(found)[:1024] if found else "-", inline=False)
    embed.add_field(name=f"미등록 ({len(missing)})", value="\n".join(missing)[:1024] if missing else "-", inline=False)

    await ctx.reply(embed=embed)




def movement_event_embed(data):
    kind = data.get("type", "이동")
    label = MOVEMENT_LABELS.get(kind, kind)
    from_team = data.get("fromTeam", "-")
    to_team = data.get("toTeam", "-")
    from_players = names_from_value(data.get("fromPlayers")) or names_from_value(data.get("playerName"))
    to_players = names_from_value(data.get("toPlayers"))
    embed = discord.Embed(title=f"🔄 {label} 승인", color=team_color(from_team), timestamp=datetime.now(timezone.utc))

    if kind == "TRADE" and to_players:
        player_lines = from_players + to_players
        from_lines = [from_team] * len(from_players) + [to_team] * len(to_players)
        to_lines = [to_team] * len(from_players) + [from_team] * len(to_players)
    else:
        player_lines = from_players
        from_lines = [from_team] * len(from_players)
        to_lines = [movement_target_label(kind, to_team)] * len(from_players)

    if kind == "NICKNAME" and to_players:
        embed.add_field(name="이전 닉네임", value="\n".join(from_players) or "-", inline=True)
        embed.add_field(name="새 닉네임", value="\n".join(to_players) or "-", inline=True)
        embed.add_field(name="소속", value=from_team or "-", inline=True)
    else:
        embed.add_field(name="선수", value="\n".join(player_lines) or "-", inline=True)
        embed.add_field(name="이전소속", value="\n".join(from_lines) or "-", inline=True)
        embed.add_field(name="신규 소속", value="\n".join(to_lines) or "-", inline=True)
    embed.add_field(name="등록자", value=data.get("createdByName", "웹/알 수 없음"), inline=True)
    if data.get("note"):
        embed.add_field(name="메모", value=str(data.get("note"))[:1024], inline=False)
    embed.set_footer(text="MBOMgr System (승인됨)")
    return embed


def player_event_embed(data):
    team = data.get("team", "팀 미정")
    embed = discord.Embed(title="로스터 등록", color=team_color(team), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="선수", value=data.get("name", "-"), inline=True)
    embed.add_field(name="팀", value=team, inline=True)
    embed.add_field(name="포지션", value=data.get("position") or "-", inline=True)
    if data.get("number"):
        embed.add_field(name="등번호", value=data.get("number"), inline=True)
    return embed


async def publish_firestore_event(title, embed, source, actor="-", announce=True):
    if announce:
        await send_announcement(embed)
    await send_log(
        title,
        f"{source}에서 발생한 이벤트입니다.",
        [("발생 위치", source, True), ("처리자", actor or "-", True)],
        embed.color.value if embed.color else 0x475569,
    )


def schedule_from_snapshot(coro):
    if not bot.loop.is_closed():
        asyncio.run_coroutine_threadsafe(coro, bot.loop)


def start_firestore_watchers():
    global SNAPSHOTS_STARTED
    if SNAPSHOTS_STARTED:
        return
    SNAPSHOTS_STARTED = True
    state = {"movements_initial": True, "players_initial": True}

    def on_movements_snapshot(col_snapshot, changes, read_time):
        if state["movements_initial"]:
            state["movements_initial"] = False
            return
        for change in changes:
            if change.type.name != "ADDED":
                continue
            data = change.document.to_dict()
            embed = movement_event_embed(data)
            actor = data.get("createdByName", "웹/알 수 없음")
            schedule_from_snapshot(sync_member_roles_for_movement(data))
            if data.get("createdBy") == "discord-python-bot":
                schedule_from_snapshot(publish_firestore_event("Discord 선수 이동", embed, "Discord", actor, False))
            else:
                schedule_from_snapshot(publish_firestore_event("웹 선수 이동", embed, "웹", actor, True))

    def on_players_snapshot(col_snapshot, changes, read_time):
        if state["players_initial"]:
            state["players_initial"] = False
            return
        for change in changes:
            if change.type.name != "ADDED":
                continue
            data = change.document.to_dict()
            embed = player_event_embed(data)
            actor = data.get("createdByName", "웹/알 수 없음")
            if data.get("createdBy") == "discord-python-bot":
                schedule_from_snapshot(publish_firestore_event("Discord 로스터 등록", embed, "Discord", actor, False))
            else:
                schedule_from_snapshot(publish_firestore_event("웹 로스터 등록", embed, "웹", actor, True))

    SNAPSHOT_UNSUBSCRIBES.append(db.collection("movements").on_snapshot(on_movements_snapshot))
    SNAPSHOT_UNSUBSCRIBES.append(db.collection("players").on_snapshot(on_players_snapshot))

@bot.event
async def on_command_error(ctx, error):
    original = getattr(error, "original", error)

    if isinstance(error, commands.CommandNotFound):
        await ctx.reply("알 수 없는 명령어입니다. `!도움말`을 입력해보세요.")
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply("명령어에 필요한 값이 부족합니다. `!도움말`을 확인해주세요.")
        return

    if isinstance(error, commands.BadArgument):
        await ctx.reply("명령어 형식이 올바르지 않습니다. `!도움말`을 확인해주세요.")
        return

    if isinstance(original, ValueError):
        await ctx.reply(str(original))
        return

    print("명령어 처리 오류:", repr(original))
    await ctx.reply(f"오류가 발생했습니다: {original}")


@bot.event
async def on_ready():
    start_firestore_watchers()
    print(f"MBO Python 봇 준비됨: {bot.user}")


bot.run(os.getenv("DISCORD_TOKEN"))
