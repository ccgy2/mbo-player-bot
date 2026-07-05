import os
import re
import asyncio
import csv
import hashlib
import io
import urllib.request
import zipfile
from xml.sax.saxutils import escape as escape_xml
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
PUNISHMENT_SYNC_STARTED = False
PUNISHMENT_SHEET_ID = "1kf6UP4zCvL6drY4GN9CIhwM97f10aIGrlpRzYESuqgU"
PUNISHMENT_SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{PUNISHMENT_SHEET_ID}/export?format=csv"

DEFAULT_ROLE_IDS = {
    "teams": {},
    "retire": "1486690482250846350",
    "forcedRelease": "1486690539012231178",
    "owner": "",
}

FREE_AGENT_TEAM = "무소속"
DEFAULT_TEAM_META = {
    FREE_AGENT_TEAM: {"name": FREE_AGENT_TEAM, "color": 0x64748B},
}

MOVEMENT_LABELS = {
    "TRADE": "트레이드",
    "FA_SIGN": "FA 영입",
    "NICKNAME": "닉네임 변경",
    "TRANSFER": "이적",
    "RELEASE": "방출",
    "RETIRE": "은퇴",
    "FORCED_RELEASE": "임의해지",
    "REGISTER": "로스터 등록",
}


def init_firebase():
    json_path = os.path.abspath(os.path.join(os.getcwd(), "..", "mbo-player-firebase-adminsdk-fbsvc-4ce86ceba5.json"))
    if os.path.exists(json_path):
        cred = credentials.Certificate(json_path)
        firebase_admin.initialize_app(cred)
        return firestore.client()

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
        "owner": normalize(saved.get("owner")) or DEFAULT_ROLE_IDS["owner"],
    }


async def configured_role_ids():
    return merge_role_config(await get_bot_config())


def merge_team_owners(config):
    return {team.upper(): normalize(owner_id) for team, owner_id in (config.get("teamOwners") or {}).items()}


async def configured_team_owners():
    return merge_team_owners(await get_bot_config())


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


async def configured_punishment_channel_id():
    config = await get_bot_config()
    return normalize(config.get("punishmentChannelId")) or await configured_public_channel_id()


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


def parse_hex_color(value):
    text = normalize(value)
    if not text:
        return None
    if not text.startswith("#"):
        text = f"#{text}"
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", text):
        return None
    return int(text[1:], 16)


def team_meta_sync():
    meta = dict(DEFAULT_TEAM_META)
    try:
        docs = db.collection("clubs").stream()
        for doc_snapshot in docs:
            data = doc_snapshot.to_dict()
            name = normalize(data.get("name"))
            color = parse_hex_color(data.get("color"))
            if name and color is not None:
                meta[name.upper()] = {"name": name, "color": color}
    except Exception as exc:
        print("팀 목록 조회 실패:", repr(exc))
    return meta


def valid_team_sync(team, allow_free_agent=False):
    key = normalize(team).upper()
    if allow_free_agent and key == FREE_AGENT_TEAM:
        return True
    return key in team_meta_sync() and key != FREE_AGENT_TEAM


def team_color(team):
    key = normalize(team).upper()
    return team_meta_sync().get(key, {}).get("color", 0x0F766E)


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


async def has_owner_role(member):
    if not member:
        return False
    role_ids = await configured_role_ids()
    owner_role_id = normalize(role_ids.get("owner"))
    if not owner_role_id:
        return False
    return any(str(role.id) == owner_role_id for role in getattr(member, "roles", []))


async def can_request_for_team(ctx, team):
    if is_authorized(ctx):
        return True
    if await has_owner_role(ctx.author):
        return True
    owners = await configured_team_owners()
    return normalize(owners.get(normalize(team).upper())) == str(ctx.author.id)


async def guard_team_request(ctx, team):
    if await can_request_for_team(ctx, team):
        return True
    await ctx.reply(f"{team} 구단주 또는 관리자만 이 요청을 만들 수 있습니다.")
    return False


async def guard_trade_request(ctx, from_team, to_team):
    if is_authorized(ctx):
        return True
    if await can_request_for_team(ctx, from_team) or await can_request_for_team(ctx, to_team):
        return True
    await ctx.reply(f"{from_team} 또는 {to_team} 구단주만 트레이드 요청을 만들 수 있습니다.")
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


async def send_punishment_announcement(embed):
    channel = await resolve_channel(await configured_punishment_channel_id())
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
        player_text = f"{', '.join(players)} ↔ {', '.join(to_players)}"
        team_text = f"{from_team} ↔ {to_team}"
    else:
        player_text = ", ".join(players)
        team_text = f"{from_team} → {movement_target_label(kind, to_team)}"

    embed.add_field(name="선수", value=player_text or "-", inline=False)
    embed.add_field(name="팀", value=team_text, inline=True)
    embed.add_field(name="날짜", value=date or "-", inline=True)
    if normalize(reason):
        embed.add_field(name="사유", value=normalize(reason)[:1024], inline=False)
    embed.set_footer(text="KMBLeague System (승인됨)")
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
    elif kind == "NICKNAME":
        embed.add_field(name="이전 닉네임", value=payload.get("oldName", "-"), inline=True)
        embed.add_field(name="새 닉네임", value=payload.get("newName", "-"), inline=True)
        embed.add_field(name="소속", value=payload.get("team", "-"), inline=True)
    elif kind == "REGISTER":
        embed.add_field(name="선수", value=payload.get("name", "-"), inline=True)
        embed.add_field(name="팀", value=payload.get("team", "-"), inline=True)
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


def format_request_number(generation, value):
    if generation <= 0:
        return f"{value:04d}"
    return f"{generation}-{value:04d}"


def next_request_number_sync():
    snapshot = CONFIG_REF.get()
    config = snapshot.to_dict() if snapshot.exists else {}
    generation = int(config.get("requestCounterGeneration") or 0)
    value = int(config.get("requestCounterValue") or 0) + 1

    if generation <= 0 and value > 9999:
        generation = 1
        value = 0
    elif generation > 0 and value > 9999:
        generation += 1
        value = 0

    CONFIG_REF.set(
        {
            "requestCounterGeneration": generation,
            "requestCounterValue": value,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )
    return format_request_number(generation, value)


def create_movement_request_sync(payload):
    request_no = next_request_number_sync()
    request_id = request_no
    db.collection("movementRequests").document(request_id).set(
        {
            "requestNo": request_no,
            "status": "PENDING",
            "payload": payload,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }
    )
    return request_no


def trade_signature(from_team, from_players, to_team, to_players):
    sides = [
        (normalize(from_team).upper(), sorted(normalize(name).lower() for name in from_players)),
        (normalize(to_team).upper(), sorted(normalize(name).lower() for name in to_players)),
    ]
    sides.sort(key=lambda item: item[0])
    body = "|".join(f"{team}:{','.join(players)}" for team, players in sides)
    return hashlib.sha1(f"TRADE|{body}".encode("utf-8")).hexdigest()


def create_trade_consent_sync(payload):
    signature = trade_signature(
        payload.get("fromTeam"),
        payload.get("fromPlayers", []),
        payload.get("toTeam"),
        payload.get("toPlayers", []),
    )
    ref = db.collection("tradeConsents").document(signature)
    snapshot = ref.get()
    requester_team = normalize(payload.get("requesterTeam") or payload.get("fromTeam")).upper()

    if not snapshot.exists:
        ref.set(
            {
                "status": "WAITING",
                "signature": signature,
                "firstTeam": requester_team,
                "firstPayload": payload,
                "createdAt": firestore.SERVER_TIMESTAMP,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }
        )
        return {"ready": False, "signature": signature, "waitingFor": normalize(payload.get("toTeam")).upper()}

    data = snapshot.to_dict()
    if data.get("status") == "MATCHED":
        return {"ready": True, "requestNo": data.get("requestNo"), "alreadyMatched": True}
    if data.get("status") != "WAITING":
        ref.set(
            {
                "status": "WAITING",
                "signature": signature,
                "firstTeam": requester_team,
                "firstPayload": payload,
                "createdAt": firestore.SERVER_TIMESTAMP,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }
        )
        return {"ready": False, "signature": signature, "waitingFor": normalize(payload.get("toTeam")).upper()}
    if normalize(data.get("firstTeam")).upper() == requester_team:
        return {"ready": False, "signature": signature, "waitingFor": normalize(payload.get("toTeam")).upper(), "duplicate": True}

    first_payload = data.get("firstPayload") or payload
    request_no = create_movement_request_sync(first_payload)
    ref.set(
        {
            "status": "MATCHED",
            "secondTeam": requester_team,
            "secondPayload": payload,
            "requestNo": request_no,
            "matchedAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )
    return {"ready": True, "requestNo": request_no}


def fetch_pending_request_sync(request_no):
    request_id = normalize(request_no)
    snapshot = db.collection("movementRequests").document(request_id).get()
    if not snapshot.exists:
        raise ValueError(f"승인 대기 요청을 찾지 못했습니다: {request_no}")

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


def mark_request_denied_sync(request_id, denier_text):
    db.collection("movementRequests").document(request_id).set(
        {
            "status": "DENIED",
            "deniedBy": denier_text,
            "deniedAt": firestore.SERVER_TIMESTAMP,
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

    if valid_team_sync(parts[0]):
        team = parts[0].upper()
        players_text = " ".join(parts[1:])
    else:
        team_index = next((index for index, part in enumerate(parts[1:], start=1) if valid_team_sync(part)), -1)
        if team_index < 0:
            team = parts[-1].upper()
            players_text = " ".join(parts[:-1])
            reason = ""
        else:
            team = parts[team_index].upper()
            players_text = " ".join(parts[:team_index])
            reason = " ".join(parts[team_index + 1 :])

    if not valid_team_sync(team):
        raise ValueError(f"팀 코드를 확인해주세요: {team}")

    if valid_team_sync(parts[0]):
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

    if not valid_team_sync(team, allow_free_agent=True):
        raise ValueError(f"팀 코드를 확인해주세요: {team}")

    if not name:
        raise ValueError("등록할 닉네임을 입력해주세요.")

    return name, team


def parse_nickname_args(args):
    parts = normalize(args).split()
    if len(parts) < 4:
        raise ValueError("사용법: `!닉네임변경 <이전닉> <새로운닉> <팀명> <날짜>`")

    old_name = parts[0]
    new_name = parts[1]
    team = parts[2].upper()
    date = parts[3]

    if not valid_team_sync(team):
        raise ValueError(f"팀 코드를 확인해주세요: {team}")
    if not is_date(date):
        raise ValueError("날짜는 YYYY-MM-DD 형식으로 입력해주세요.")

    return old_name, new_name, team, date


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


def commit_nickname_change_sync(old_name, new_name, team, date, author_text):
    player = find_player_sync(old_name, team)
    if not player:
        raise ValueError(f"선수 목록에 없는 선수입니다: {old_name} ({team})")

    existing = find_player_sync(new_name, team)
    if existing:
        raise ValueError(f"이미 같은 팀에 존재하는 닉네임입니다: {new_name} ({team})")

    batch = db.batch()
    update_player(
        batch,
        player,
        {
            "name": new_name,
            "transfer": f"{date} {old_name} → {new_name} 닉네임 변경",
        },
    )
    add_movement(
        batch,
        "NICKNAME",
        old_name,
        team,
        team,
        date,
        f"Discord 봇 입력: {author_text}",
        [old_name],
        [new_name],
    )
    batch.commit()


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


async def create_trade_consent(ctx, from_team, from_players, to_team, to_players, date):
    if not await can_request_for_team(ctx, from_team):
        await ctx.reply(f"{from_team} 구단주 또는 관리자만 {from_team} 측 트레이드 요청을 올릴 수 있습니다.")
        return None

    payload = {
        "kind": "TRADE",
        "fromTeam": from_team,
        "fromPlayers": from_players,
        "toTeam": to_team,
        "toPlayers": to_players,
        "date": date,
        "requesterTeam": from_team,
        "requesterId": str(ctx.author.id),
        "requesterName": str(ctx.author),
    }
    result = await run_blocking(create_trade_consent_sync, payload)

    if result.get("alreadyMatched"):
        await ctx.reply(f"이미 승인 대기 요청으로 올라간 트레이드입니다. 요청번호: `{result.get('requestNo')}`")
        return result.get("requestNo")

    if not result.get("ready"):
        suffix = "이미 같은 팀 요청이 접수되어 있습니다." if result.get("duplicate") else "상대 팀 요청을 기다립니다."
        embed = discord.Embed(title="⏳ 트레이드 양팀 확인 대기", color=0xF59E0B, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="요청 팀", value=from_team, inline=True)
        embed.add_field(name="상대 팀", value=to_team, inline=True)
        embed.add_field(name="선수", value=f"{', '.join(from_players)} ↔ {', '.join(to_players)}", inline=False)
        embed.add_field(name="상태", value=suffix, inline=False)
        embed.set_footer(text=f"상대 팀은 반대로 입력: !트레이드 {to_team} {','.join(to_players)} {from_team} {','.join(from_players)} {date}")
        await ctx.reply(embed=embed)
        return None

    request_no = result.get("requestNo")
    embed = movement_request_embed(request_no, payload)
    embed.title = "⏳ 트레이드 승인 대기"
    embed.add_field(name="양팀 확인", value="양 팀 구단주 요청이 모두 접수되었습니다.", inline=False)
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
    elif kind == "NICKNAME":
        commit_nickname_change_sync(
            payload.get("oldName"),
            payload.get("newName"),
            payload.get("team"),
            payload.get("date"),
            approver_text,
        )
    elif kind == "REGISTER":
        commit_player_registration_sync(
            payload.get("name"),
            payload.get("team"),
            approver_text,
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


def find_player_team_by_name_sync(name):
    player = find_player_sync(name)
    return normalize(player.get("team")) if player else ""


def punishment_payload(nickname, team, reason, penalty, note, date=None, release_date="", status="징계 중", author_text=""):
    return {
        "status": normalize(status) or "징계 중",
        "date": normalize(date) or today(),
        "dateText": normalize(date) or today(),
        "team": normalize(team),
        "nickname": normalize(nickname),
        "reason": normalize(reason),
        "penalty": normalize(penalty),
        "releaseDate": normalize(release_date),
        "releaseDateText": normalize(release_date),
        "note": normalize(note),
        "pardon": "",
        "createdBy": "discord-python-bot",
        "createdByName": author_text or "Discord Python Bot",
        "createdAt": firestore.SERVER_TIMESTAMP,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }


def is_roster_removal_punishment(record):
    text = " ".join(
        normalize(record.get(key))
        for key in ["status", "penalty", "note", "releaseDateText", "releaseDate"]
    )
    if "중" not in normalize(record.get("status")):
        return False
    if normalize(record.get("pardon")):
        return False
    return "영구제명" in re.sub(r"\s+", "", text)


def punishment_roster_text(record):
    date = normalize(record.get("dateText") or record.get("date")) or today()
    reason = normalize(record.get("reason")) or "-"
    penalty = normalize(record.get("penalty")) or "-"
    note = normalize(record.get("note"))
    suffix = f" ({note})" if note else ""
    return f"{date} 처벌: {reason} / {penalty}{suffix}"


def apply_roster_removal_for_punishment(batch, record):
    if not is_roster_removal_punishment(record):
        return False
    player = find_player_sync(record.get("nickname"))
    if not player:
        return False
    if normalize(player.get("team")) == "무소속" and normalize(player.get("transfer")) == punishment_roster_text(record):
        return False
    update_player(
        batch,
        player,
        {
            "team": "무소속",
            "transfer": punishment_roster_text(record),
            "forcedReleaseOriginalTeam": "",
        },
    )
    return True


def revert_roster_removal_for_punishment(batch, record):
    if not is_roster_removal_punishment(record):
        return False
    player = find_player_sync(record.get("nickname"))
    original_team = normalize(record.get("team"))
    if not player or not original_team:
        return False
    if normalize(player.get("team")) != "무소속" or normalize(player.get("transfer")) != punishment_roster_text(record):
        return False
    update_player(
        batch,
        player,
        {
            "team": original_team,
            "transfer": "",
            "forcedReleaseOriginalTeam": "",
        },
    )
    return True


def add_punishment_sync(payload):
    batch = db.batch()
    batch.set(db.collection("punishments").document(), payload)
    apply_roster_removal_for_punishment(batch, payload)
    batch.commit()


def resolve_punishment_doc_sync(punishment_id):
    key = normalize(punishment_id)
    if not key:
        raise ValueError("처벌 기록 ID를 입력해주세요.")

    direct_ref = db.collection("punishments").document(key)
    direct_snapshot = direct_ref.get()
    if direct_snapshot.exists:
        return direct_ref, direct_snapshot.to_dict()

    matches = []
    for doc_snapshot in db.collection("punishments").stream():
        if doc_snapshot.id.startswith(key):
            matches.append((doc_snapshot.reference, doc_snapshot.to_dict()))

    if not matches:
        raise ValueError(f"처벌 기록을 찾지 못했습니다: {key}")
    if len(matches) > 1:
        raise ValueError(f"처벌 기록 ID가 여러 개와 일치합니다. 더 길게 입력해주세요: {key}")
    return matches[0]


def update_punishment_sync(punishment_id, payload):
    ref, previous = resolve_punishment_doc_sync(punishment_id)
    batch = db.batch()
    revert_roster_removal_for_punishment(batch, previous)
    payload = {**payload, "updatedAt": firestore.SERVER_TIMESTAMP}
    payload.pop("createdAt", None)
    batch.update(ref, payload)
    apply_roster_removal_for_punishment(batch, payload)
    batch.commit()


def delete_punishment_sync(punishment_id):
    ref, previous = resolve_punishment_doc_sync(punishment_id)
    batch = db.batch()
    revert_roster_removal_for_punishment(batch, previous)
    batch.delete(ref)
    batch.commit()


def sheet_record_id(record):
    key = "|".join(
        normalize(record.get(key))
        for key in ["status", "dateText", "team", "nickname", "reason", "penalty", "releaseDateText", "note", "pardon"]
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def parse_sheet_date(text):
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", normalize(text))
    return match.group(1) if match else normalize(text)


def punishment_records_from_sheet_sync():
    raw = urllib.request.urlopen(PUNISHMENT_SHEET_CSV_URL, timeout=30).read().decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(raw)))
    records = []
    for row in rows[2:]:
        row = row + [""] * 10
        status, date_text, team, nickname, reason, penalty, release_text, note, pardon = [
            normalize(item) for item in row[1:10]
        ]
        if not nickname or status == "-":
            continue
        records.append(
            {
                "status": status,
                "date": parse_sheet_date(date_text),
                "dateText": date_text,
                "team": team,
                "nickname": nickname,
                "reason": reason,
                "penalty": penalty,
                "releaseDate": parse_sheet_date(release_text),
                "releaseDateText": release_text,
                "note": note,
                "pardon": pardon,
                "source": "google-sheet-import",
                "sourceSheetId": PUNISHMENT_SHEET_ID,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }
        )
    return records


def sync_punishments_from_sheet_sync():
    records = punishment_records_from_sheet_sync()
    synced = 0
    removed = 0
    for start in range(0, len(records), 200):
        batch = db.batch()
        for record in records[start : start + 200]:
            batch.set(db.collection("punishments").document(sheet_record_id(record)), record, merge=True)
            if apply_roster_removal_for_punishment(batch, record):
                removed += 1
            synced += 1
        batch.commit()
    return synced, removed


def fetch_punishments_sync(query_text):
    query_text = normalize(query_text)
    docs = db.collection("punishments").order_by("date", direction=firestore.Query.DESCENDING).stream()
    records = [{"id": doc.id, **doc.to_dict()} for doc in docs]
    if not query_text:
        return records

    lowered = query_text.lower()
    uppered = query_text.upper()
    nickname_matches = [record for record in records if normalize(record.get("nickname")).lower() == lowered]
    team_matches = [record for record in records if normalize(record.get("team")).upper() == uppered]
    return nickname_matches or team_matches


def punishment_summary(records):
    active = [record for record in records if "중" in normalize(record.get("status"))]
    return len(records), len(active)


def punishment_embed(title, records):
    total, active_count = punishment_summary(records)
    embed = discord.Embed(title=title, color=0xEF4444 if active_count else 0x0F766E, timestamp=datetime.now(timezone.utc))
    embed.description = f"총 처벌 기록 {total}건 · 징계 중 {active_count}건"
    for index, record in enumerate(records[:10], start=1):
        embed.add_field(
            name=f"{index}. {normalize(record.get('dateText') or record.get('date')) or '-'} · {normalize(record.get('status')) or '-'}",
            value=(
                f"ID: `{normalize(record.get('id'))[:8] or '-'}`\n"
                f"{normalize(record.get('nickname')) or '-'} ({normalize(record.get('team')) or '팀 미정'})\n"
                f"{normalize(record.get('reason')) or '-'} -> {normalize(record.get('penalty')) or '-'}\n"
                f"해제: {normalize(record.get('releaseDateText') or record.get('releaseDate')) or '-'}"
            ),
            inline=False,
        )
    if len(records) > 10:
        embed.add_field(name="안내", value="10건까지만 표시합니다.", inline=False)
    if not records:
        embed.add_field(name="내역", value="처벌 기록이 없습니다.", inline=False)
    return embed


def fetch_team_roster_sync(team):
    docs = db.collection("players").where("team", "==", team).stream()
    players = [{"id": doc.id, **doc.to_dict()} for doc in docs]
    return sorted(players, key=lambda player: normalize(player.get("name")).lower())


def movement_names(data):
    return names_from_value(data.get("fromPlayers")) + names_from_value(data.get("toPlayers")) + names_from_value(data.get("playerName"))


def movement_includes_alias(data, aliases):
    lowered = {normalize(alias).lower() for alias in aliases if normalize(alias)}
    return any(normalize(name).lower() in lowered for name in movement_names(data))


def player_aliases_from_movements(player_name, movements):
    aliases = {normalize(player_name)}
    changed = True
    while changed:
        changed = False
        for data in movements:
            if data.get("type") != "NICKNAME":
                continue
            from_names = names_from_value(data.get("fromPlayers"))
            to_names = names_from_value(data.get("toPlayers"))
            for index, old_name in enumerate(from_names):
                new_name = normalize(to_names[index] if index < len(to_names) else "")
                old_name = normalize(old_name)
                if not old_name or not new_name:
                    continue
                if old_name in aliases and new_name not in aliases:
                    aliases.add(new_name)
                    changed = True
                if new_name in aliases and old_name not in aliases:
                    aliases.add(old_name)
                    changed = True
    return aliases


def fetch_player_transfer_info_sync(player_name):
    player = find_player_sync(player_name)
    if not player:
        raise ValueError(f"현재 로스터에서 해당 닉네임을 찾지 못했습니다: {player_name}")

    docs = db.collection("movements").order_by("date", direction=firestore.Query.DESCENDING).stream()
    movements = [doc.to_dict() for doc in docs]
    aliases = player_aliases_from_movements(player.get("name"), movements)
    records = [data for data in movements if movement_includes_alias(data, aliases)]
    return player, records


def movement_route_text(data):
    kind = data.get("type")
    from_team = normalize(data.get("fromTeam")) or "-"
    to_team = normalize(data.get("toTeam")) or "-"
    if kind == "RELEASE":
        return f"{from_team} -> 방출"
    if kind == "RETIRE":
        return f"{from_team} -> 은퇴"
    if kind == "FORCED_RELEASE":
        return f"{from_team} -> 임의해지"
    if kind == "NICKNAME":
        return f"{from_team} 닉네임 변경"
    return f"{from_team} -> {to_team}"


def transfer_txt_bytes(player, movements):
    lines = [f"{player.get('name')} 이적 정보", f"현재 소속: {normalize(player.get('team')) or '-'}", ""]
    if not movements:
        lines.append("등록된 이적 정보가 없습니다.")
    for index, data in enumerate(movements, start=1):
        label = MOVEMENT_LABELS.get(data.get("type"), data.get("type", "이동"))
        lines.append(
            f"{index}. {normalize(data.get('date')) or '-'} | {label} | {movement_route_text(data)} | "
            f"{', '.join(movement_names(data)) or '-'}"
        )
        if normalize(data.get("note")):
            lines.append(f"   메모: {normalize(data.get('note'))}")
    return "\n".join(lines).encode("utf-8")


def transfer_xlsx_bytes(player, movements):
    rows = [["날짜", "유형", "선수", "이전 팀", "새 팀", "경로", "메모"]]
    rows.extend(
        [
            data.get("date"),
            MOVEMENT_LABELS.get(data.get("type"), data.get("type", "이동")),
            ", ".join(movement_names(data)),
            data.get("fromTeam"),
            data.get("toTeam"),
            movement_route_text(data),
            data.get("note"),
        ]
        for data in movements
    )
    sheet_rows = []
    for row_index, values in enumerate(rows, start=1):
        cells = "".join(xlsx_cell(chr(64 + column_index), row_index, value) for column_index, value in enumerate(values, start=1))
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')

    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{escape_xml(normalize(player.get("name")))}" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(sheet_rows)}</sheetData>"
            "</worksheet>"
        ),
    }

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as workbook:
        for path, content in files.items():
            workbook.writestr(path, content)
    return output.getvalue()


def roster_txt_bytes(team, players):
    lines = [f"{team} 로스터", ""]
    for index, player in enumerate(players, start=1):
        lines.append(
            f"{index}. {normalize(player.get('name'))}"
            f" | 포지션: {normalize(player.get('position')) or '-'}"
            f" | 등번호: {normalize(player.get('number')) or '-'}"
        )
    return "\n".join(lines).encode("utf-8")


def xlsx_cell(column, row, value):
    return f'<c r="{column}{row}" t="inlineStr"><is><t>{escape_xml(normalize(value))}</t></is></c>'


def roster_xlsx_bytes(team, players):
    rows = [["순번", "닉네임", "팀", "포지션", "등번호", "이동내역"]]
    rows.extend(
        [
            str(index),
            player.get("name"),
            player.get("team"),
            player.get("position"),
            player.get("number"),
            player.get("transfer"),
        ]
        for index, player in enumerate(players, start=1)
    )
    sheet_rows = []
    for row_index, values in enumerate(rows, start=1):
        cells = "".join(xlsx_cell(chr(64 + column_index), row_index, value) for column_index, value in enumerate(values, start=1))
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')

    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{escape_xml(team)}" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(sheet_rows)}</sheetData>"
            "</worksheet>"
        ),
    }

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as workbook:
        for path, content in files.items():
            workbook.writestr(path, content)
    return output.getvalue()


class RosterDownloadView(discord.ui.View):
    def __init__(self, team, players):
        super().__init__(timeout=300)
        self.team = team
        self.players = players

    @discord.ui.button(label="TXT 다운로드", style=discord.ButtonStyle.secondary)
    async def download_txt(self, interaction, button):
        file = discord.File(io.BytesIO(roster_txt_bytes(self.team, self.players)), filename=f"roster_{self.team}.txt")
        await interaction.response.send_message(file=file, ephemeral=True)

    @discord.ui.button(label="XLSX 다운로드", style=discord.ButtonStyle.primary)
    async def download_xlsx(self, interaction, button):
        file = discord.File(io.BytesIO(roster_xlsx_bytes(self.team, self.players)), filename=f"roster_{self.team}.xlsx")
        await interaction.response.send_message(file=file, ephemeral=True)




class TransferInfoDownloadView(discord.ui.View):
    def __init__(self, player, movements):
        super().__init__(timeout=300)
        self.player = player
        self.movements = movements

    @discord.ui.button(label="TXT 다운로드", style=discord.ButtonStyle.secondary)
    async def download_txt(self, interaction, button):
        file = discord.File(
            io.BytesIO(transfer_txt_bytes(self.player, self.movements)),
            filename=f"transfer_{normalize(self.player.get('name'))}.txt",
        )
        await interaction.response.send_message(file=file, ephemeral=True)

    @discord.ui.button(label="XLSX 다운로드", style=discord.ButtonStyle.primary)
    async def download_xlsx(self, interaction, button):
        file = discord.File(
            io.BytesIO(transfer_xlsx_bytes(self.player, self.movements)),
            filename=f"transfer_{normalize(self.player.get('name'))}.xlsx",
        )
        await interaction.response.send_message(file=file, ephemeral=True)


def discord_member_names(member):
    return {
        normalize(getattr(member, "display_name", "")).lower(),
        normalize(getattr(member, "name", "")).lower(),
        normalize(getattr(member, "global_name", "")).lower(),
    } - {""}


async def can_view_transfer_info(ctx, player):
    if is_authorized(ctx):
        return True
    if normalize(player.get("name")).lower() in discord_member_names(ctx.author):
        return True
    owners = await configured_team_owners()
    team = normalize(player.get("team")).upper()
    return normalize(owners.get(team)) == str(ctx.author.id)


def transfer_info_embed(player, movements):
    embed = discord.Embed(
        title=f"{normalize(player.get('name'))} 이적 정보",
        color=team_color(player.get("team")),
        timestamp=datetime.now(timezone.utc),
    )
    embed.description = f"현재 소속: {normalize(player.get('team')) or '-'} · 총 {len(movements)}건"

    for index, data in enumerate(movements[:10], start=1):
        label = MOVEMENT_LABELS.get(data.get("type"), data.get("type", "이동"))
        embed.add_field(
            name=f"{index}. {normalize(data.get('date')) or '-'} · {label}",
            value=f"{movement_route_text(data)}\n{', '.join(movement_names(data)) or '-'}",
            inline=False,
        )

    if len(movements) > 10:
        embed.add_field(name="안내", value="10건까지만 미리 표시합니다. 전체 내역은 다운로드 버튼을 사용하세요.", inline=False)
    if not movements:
        embed.add_field(name="내역", value="등록된 이적 정보가 없습니다.", inline=False)
    return embed


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
    special_map = {
        "구단주": "owner",
        "은퇴": "retire",
        "임의탈퇴": "forcedRelease",
        "임의해지": "forcedRelease",
    }

    if category in {"목록", "리스트"}:
        lines = [f"{team}: <@&{role_id}>" for team, role_id in sorted(role_ids["teams"].items())]
        if role_ids.get("owner"):
            lines.append(f"구단주: <@&{role_ids['owner']}>")
        lines.append(f"은퇴: <@&{role_ids['retire']}>")
        lines.append(f"임의탈퇴/임의해지: <@&{role_ids['forcedRelease']}>")
        await ctx.reply("현재 역할 설정입니다.\n" + "\n".join(lines))
        return

    if category == "팀":
        team = normalize(team_or_role).upper()
        role_id = parse_role_id(role_text)
        if not team or team == FREE_AGENT_TEAM:
            await ctx.reply("팀명을 확인해주세요. 예: `!역할 팀 DSP @역할`")
            return
        if not role_id:
            await ctx.reply("역할을 멘션해주세요. 예: `!역할 팀 DSP @역할`")
            return
        role_ids["teams"][team] = role_id
        await set_bot_config({"roleIds": role_ids})
        await ctx.reply(f"{team} 역할을 <@&{role_id}> 로 설정했습니다.")
        await send_log("팀 역할 설정", f"{ctx.author} 님이 {team} 역할을 <@&{role_id}> 로 설정했습니다.", [], 0x0F766E)
        return

    if category and category not in special_map:
        team = category.upper()
        role_id = parse_role_id(team_or_role)
        if not role_id:
            await ctx.reply("역할을 멘션해주세요. 예: `!역할 DSP @역할`")
            return
        role_ids["teams"][team] = role_id
        await set_bot_config({"roleIds": role_ids})
        await ctx.reply(f"{team} 역할을 <@&{role_id}> 로 설정했습니다.")
        await send_log("팀 역할 설정", f"{ctx.author} 님이 {team} 역할을 <@&{role_id}> 로 설정했습니다.", [], 0x0F766E)
        return

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
        "사용법: `!역할 팀 <팀코드> <@역할>`, `!역할 구단주 <@역할>`, `!역할 은퇴 <@역할>`, "
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


@bot.command(name="구단주")
async def set_team_owner_command(ctx, member_text: str = "", team_text: str = ""):
    if not await guard(ctx):
        return

    owner_id = parse_user_id(member_text)
    team = normalize(team_text).upper()
    if not owner_id or not valid_team_sync(team):
        await ctx.reply("사용법: `!구단주 <@플레이어> <팀명>`")
        return

    config = await get_bot_config()
    owners = merge_team_owners(config)
    owners[team] = owner_id
    await set_bot_config({"teamOwners": owners})
    await ctx.reply(f"{team} 구단주를 <@{owner_id}> 로 설정했습니다.")


@bot.command(name="등록")
async def register_player_command(ctx, *, args: str = ""):
    name, team = parse_register_args(args)
    await create_movement_request(
        ctx,
        {
            "kind": "REGISTER",
            "name": name,
            "team": team,
            "date": today(),
            "requesterId": str(ctx.author.id),
            "requesterName": str(ctx.author),
        },
    )


@bot.command(name="닉네임변경", aliases=["닉변"])
async def nickname_change_command(ctx, *, args: str = ""):
    old_name, new_name, team, date = parse_nickname_args(args)
    if not await guard_team_request(ctx, team):
        return

    await create_movement_request(
        ctx,
        {
            "kind": "NICKNAME",
            "oldName": old_name,
            "newName": new_name,
            "team": team,
            "fromTeam": team,
            "toTeam": team,
            "date": date,
            "requesterId": str(ctx.author.id),
            "requesterName": str(ctx.author),
        },
    )


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
    elif kind == "NICKNAME":
        role_results = []
        embed = discord.Embed(title="🔄 닉네임 변경 승인", color=team_color(payload.get("team")), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="변경", value=f"{payload.get('oldName', '-')} → {payload.get('newName', '-')}", inline=False)
        embed.add_field(name="팀", value=payload.get("team", "-"), inline=True)
        embed.add_field(name="날짜", value=payload.get("date", "-"), inline=True)
        embed.set_footer(text="KMBLeague System (승인됨)")
    elif kind == "REGISTER":
        role_results = await sync_roles_for_player_registration(payload.get("name"), payload.get("team"))
        embed = player_event_embed({"name": payload.get("name"), "team": payload.get("team")})
        embed.title = "로스터 등록 승인"
        embed.set_footer(text="KMBLeague System (승인됨)")
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


@bot.command(name="거부")
async def deny_request_command(ctx, admin_text: str = "", request_no: str = ""):
    if not await guard(ctx):
        return

    admin_id = parse_user_id(admin_text)
    request_no = normalize(request_no)
    if not admin_id or not request_no:
        await ctx.reply("사용법: `!거부 <@관리자디스코드> <요청번호>`")
        return

    if admin_id != str(ctx.author.id):
        await ctx.reply("최종 거부자는 명령을 실행한 관리자 본인을 멘션해주세요.")
        return

    denier = ctx.guild.get_member(int(admin_id)) if ctx.guild else None
    if not is_authorized_member(denier):
        await ctx.reply("멘션한 사용자는 거부 권한이 없습니다.")
        return

    request_id, request_data = await run_blocking(fetch_pending_request_sync, request_no)
    await run_blocking(mark_request_denied_sync, request_id, str(ctx.author))
    payload = request_data.get("payload") or {}
    label = MOVEMENT_LABELS.get(payload.get("kind"), payload.get("kind", "요청"))
    embed = discord.Embed(title=f"🚫 {label} 요청 거부", color=0xEF4444, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="요청번호", value=request_no, inline=True)
    embed.add_field(name="거부자", value=ctx.author.mention, inline=True)
    embed.set_footer(text="KMBLeague System (거부됨)")
    await ctx.reply(embed=embed)


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
        value="팀별 Discord 역할을 설정합니다. 예: `!역할 팀 DSP @DSP`",
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
        name="!거부 <@관리자디스코드> <요청번호>",
        value="승인 대기 요청을 거부합니다.",
        inline=False,
    )

    embed.add_field(
        name="!구단주 <@플레이어> <팀명>",
        value="팀 구단주를 지정합니다. 구단주는 자기 팀 영입/방출/은퇴/임의해지를 요청할 수 있습니다.",
        inline=False,
    )

    embed.add_field(
        name="!팀로스터 <팀명> / !로스터 <팀명>",
        value="현재 해당 팀 소속 선수만 보여주고 TXT/XLSX 다운로드 버튼을 제공합니다.",
        inline=False,
    )

    embed.add_field(
        name="!정보 <닉네임> / !이적정보 <닉네임>",
        value="선수의 지금까지 이적 정보를 보여주고 TXT/XLSX 다운로드 버튼을 제공합니다.",
        inline=False,
    )

    embed.add_field(
        name="!처벌기록 <닉네임 또는 팀명>",
        value="해당 선수 또는 팀의 총 처벌 기록과 현재 징계 여부를 조회합니다.",
        inline=False,
    )

    embed.add_field(
        name="!처벌 <처벌사유> <닉네임> <자세한 처벌내용> <징계일>",
        value="처벌 기록을 등록합니다. 예: `!처벌 욕설 PlayerName 심한 욕설 3일침묵`",
        inline=False,
    )

    embed.add_field(
        name="!처벌수정 <처벌ID> <처벌사유> <닉네임> <자세한 처벌내용> <징계내용>",
        value="처벌기록에서 보이는 ID로 처벌 기록을 수정합니다.",
        inline=False,
    )

    embed.add_field(
        name="!처벌삭제 <처벌ID>",
        value="처벌기록에서 보이는 ID로 처벌 기록을 삭제합니다.",
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
        name="!닉네임변경 <이전닉> <새로운닉> <팀명> <날짜>",
        value="닉네임 변경 승인 요청을 만듭니다.",
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
        value="```!트레이드 DSP PlayerA ABC PlayerB 2026-05-05```",
        inline=False,
    )

    embed.add_field(
        name="영입/방출 예시",
        value="```!영입 PlayerName DSP 테스트 참가\n!방출 PlayerName DSP 개인 사정\n!승인 @관리자 0001```",
        inline=False,
    )

    embed.add_field(
        name="타순표 예시",
        value="```!유효성검사\n1. Axrq__ CF\n2번 CUCCl 1B\n3. _w0nyu1 LF```",
        inline=False,
    )

    if len(embed.fields) > 25:
        fields = list(embed.fields)
        pages = []
        for index in range(0, len(fields), 25):
            page = discord.Embed(
                title="MBO Python 봇 명령어" if index == 0 else "MBO Python 봇 명령어 계속",
                color=0x0F766E,
            )
            for field in fields[index : index + 25]:
                page.add_field(name=field.name, value=field.value, inline=field.inline)
            pages.append(page)

        await ctx.reply(embed=pages[0])
        for page in pages[1:]:
            await ctx.send(embed=page)
        return

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


@bot.command(name="로스터", aliases=["팀로스터"])
async def roster_command(ctx, team_text: str = ""):
    team = normalize(team_text).upper()
    if not valid_team_sync(team):
        await ctx.reply("사용법: `!팀로스터 <팀명>`")
        return

    players = await run_blocking(fetch_team_roster_sync, team)
    embed = discord.Embed(title=f"{team} 로스터", color=team_color(team), timestamp=datetime.now(timezone.utc))
    embed.description = f"총 {len(players)}명"
    if players:
        lines = [
            f"{index}. {normalize(player.get('name'))}"
            f" · {normalize(player.get('position')) or '-'}"
            f" · No.{normalize(player.get('number')) or '-'}"
            for index, player in enumerate(players[:20], start=1)
        ]
        embed.add_field(name="선수 목록", value="\n".join(lines), inline=False)
        if len(players) > 20:
            embed.add_field(name="안내", value="20명까지만 미리 표시합니다. 전체 명단은 다운로드 버튼을 사용하세요.", inline=False)
    else:
        embed.description = "등록된 선수가 없습니다."

    await ctx.reply(embed=embed, view=RosterDownloadView(team, players))


@bot.command(name="정보", aliases=["이적정보"])
async def transfer_info_command(ctx, *, player_name: str = ""):
    player_name = normalize(player_name)
    if not player_name:
        await ctx.reply("사용법: `!정보 <닉네임>` 또는 `!이적정보 <닉네임>`")
        return

    player, movements = await run_blocking(fetch_player_transfer_info_sync, player_name)
    if not await can_view_transfer_info(ctx, player):
        await ctx.reply("이 선수의 이적 정보를 볼 권한이 없습니다.")
        return

    await ctx.reply(embed=transfer_info_embed(player, movements), view=TransferInfoDownloadView(player, movements))

@bot.command(name="처벌채널설정")
async def set_punishment_channel(ctx, channel_mention: str = None):
    # 관리자 권한 확인 (기존 guard 함수 활용)
    if not await guard(ctx):
        return

    if not channel_mention:
        await ctx.reply("사용법: `!처벌채널설정 #채널명` 형태로 채널을 멘션해 주세요.")
        return

    # 멘션에서 채널 ID 추출
    channel_id = parse_channel_id(channel_mention)
    if not channel_id:
        await ctx.reply("올바른 채널을 지정해 주세요.")
        return

    try:
        # DB(Firestore)의 punishmentChannelId 필드에 저장
        await set_bot_config({"punishmentChannelId": channel_id})
        await ctx.reply(f"처벌 기록이 올라갈 채널이 <#{channel_id}>로 설정되었습니다! (일반 공지 채널과 분리됨)")
    except Exception as e:
        await ctx.reply(f"채널 설정 중 오류가 발생했습니다: {e}")

@bot.command(name="처벌기록")
async def punishment_record_command(ctx, *, query_text: str = ""):
    query_text = normalize(query_text)
    if not query_text:
        await ctx.reply("사용법: `!처벌기록 <닉네임 또는 팀명>`")
        return

    records = await run_blocking(fetch_punishments_sync, query_text)
    title = f"{query_text} 처벌 기록"
    await ctx.reply(embed=punishment_embed(title, records))


@bot.command(name="처벌")
async def punishment_command(ctx, *, args: str = ""):
    if not await guard(ctx):
        return

    parts = normalize(args).split()
    if len(parts) < 4:
        await ctx.reply("사용법: `!처벌 <처벌사유> <닉네임> <자세한 처벌내용> <징계일>`")
        return

    reason = parts[0]
    nickname = parts[1]
    penalty = parts[-1]
    note = " ".join(parts[2:-1])
    team = await run_blocking(find_player_team_by_name_sync, nickname)
    payload = punishment_payload(nickname, team, reason, penalty, note, author_text=str(ctx.author))
    await run_blocking(add_punishment_sync, payload)
    await ctx.reply(embed=punishment_embed(f"{nickname} 처벌 등록", [payload]))


@bot.command(name="처벌수정")
async def punishment_update_command(ctx, punishment_id: str = "", *, args: str = ""):
    if not await guard(ctx):
        return

    parts = normalize(args).split()
    if not punishment_id or len(parts) < 4:
        await ctx.reply("사용법: `!처벌수정 <처벌ID> <처벌사유> <닉네임> <자세한 처벌내용> <징계내용>`")
        return

    reason = parts[0]
    nickname = parts[1]
    penalty = parts[-1]
    note = " ".join(parts[2:-1])
    team = await run_blocking(find_player_team_by_name_sync, nickname)
    payload = punishment_payload(nickname, team, reason, penalty, note, author_text=str(ctx.author))
    await run_blocking(update_punishment_sync, punishment_id, payload)
    await ctx.reply(embed=punishment_embed(f"{nickname} 처벌 수정", [{"id": punishment_id, **payload}]))


@bot.command(name="처벌삭제")
async def punishment_delete_command(ctx, punishment_id: str = ""):
    if not await guard(ctx):
        return

    punishment_id = normalize(punishment_id)
    if not punishment_id:
        await ctx.reply("사용법: `!처벌삭제 <처벌ID>`")
        return

    await run_blocking(delete_punishment_sync, punishment_id)
    await ctx.reply(f"처벌 기록 `{punishment_id}` 을 삭제했습니다.")


@bot.command(name="이동")
async def legacy_movement(ctx, movement_type: str = "", *, args: str = ""):
    movement_type = normalize(movement_type)

    if movement_type == "트레이드":
        from_team, from_players, to_team, to_players, date = parse_trade_args(args)
        await create_trade_consent(ctx, from_team, from_players, to_team, to_players, date)
        return

    if movement_type in {"영입", "FA영입", "FA_SIGN", "FA"}:
        to_team, players, date, reason = parse_fa_sign_args(args)
        if not await guard_team_request(ctx, to_team):
            return
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
        if not await guard_team_request(ctx, team):
            return
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
        if not await guard_team_request(ctx, team):
            return
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
        if not await guard_team_request(ctx, team):
            return
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
    await create_trade_consent(ctx, from_team, from_players, to_team, to_players, date)


@bot.command(name="영입", aliases=["FA영입", "FA"])
async def fa_sign(ctx, *, args: str = ""):
    to_team, players, date, reason = parse_fa_sign_args(args)
    if not await guard_team_request(ctx, to_team):
        return
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
    if not await guard_team_request(ctx, team):
        return
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
    
    # 🔥 FA 영입일 때는 골드 색상으로 강조하고, 기본은 이전 팀 색상 유지
    embed_color = discord.Color.gold() if kind == "FA_SIGN" else team_color(from_team)
    
    embed = discord.Embed(title=f"🔄 {label} 승인", color=embed_color, timestamp=datetime.now(timezone.utc))

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
        
    # 🔥 [FA 계약 정보 필드 추가]
    # Firebase에 저장한 contractYears와 contractAmount를 가져와서 포맷팅 후 디스코드 필드로 추가합니다.
    if kind == "FA_SIGN":
        years = data.get("contractYears", 0)
        amount = data.get("contractAmount", 0)
        formatted_amount = f"{amount:,}"  # 3자리 끊어 읽기 쉼표(,) 추가 (예: 50,000,000)
        
        embed.add_field(name="💰 계약 조건", value=f"**{years}년 / {formatted_amount} 포인트**", inline=True)
    else:
        # 다른 이적 구분일 경우 빈칸을 맞춰주기 위해 등록자를 inline=True로 공통 배치
        pass

    embed.add_field(name="등록자", value=data.get("createdByName", "웹/알 수 없음"), inline=True)
    
    if data.get("note"):
        embed.add_field(name="메모", value=str(data.get("note"))[:1024], inline=False)
        
    embed.set_footer(text="KMBLeague System (승인됨)")
    return embed

# bot.py 내의 구단 정보 조회 명령어 예시 수정
@bot.command(name="구단정보", aliases=["구단", "팀"])
async def club_info(ctx, *, team_name: str):
    # 입력받은 팀 이름으로 Firestore에서 구단 문서 조회
    team_ref = db.collection("clubs").document(team_name)
    team_doc = team_ref.get()
    
    if not team_doc.exists:
        await ctx.reply(f"❌ '{team_name}' 구단을 찾을 수 없습니다.")
        return
        
    club_data = team_doc.to_dict()
    owner = club_data.get("owner", "없음")
    
    # 🔥 파이어베이스에서 구단 총 연봉 값 추출 및 포맷팅
    total_salary = club_data.get("totalSalary", 0)
    formatted_salary = f"{total_salary:,} 포인트"
    
    # 해당 구단 소속 선수단 리스트 가져오기
    players_ref = db.collection("players").where("team", "==", team_name)
    players_docs = players_ref.stream()
    
    player_list = []
    for p in players_docs:
        p_data = p.to_dict()
        p_name = p_data.get("name", "무명선수")
        p_amount = p_data.get("contractAmount", 0)
        # 선수 개인 계약금이 있으면 이름 옆에 같이 노출 (예: 홍길동(5,000,000))
        if p_amount > 0:
            player_list.append(f"• {p_name} ({p_amount:,}P)")
        else:
            player_list.append(f"• {p_name}")

    players_str = "\n".join(player_list) if player_list else "등록된 선수가 없습니다."

    # 메인 알림 임베드 구성
    embed = discord.Embed(
        title=f"🏛️ {team_name} 구단 정보 메인",
        color=discord.Color.blue()
    )
    embed.add_field(name="👑 구단주 / 프런트", value=owner, inline=True)
    # 🔥 총 연봉 필드를 메인 영역에 확실하게 추가
    embed.add_field(name="💰 구단 총 연봉", value=f"**{formatted_salary}**", inline=True)
    embed.add_field(name="🏃 소속 선수단 명단 (개별 계약금)", value=players_str, inline=False)
    
    embed.set_footer(text="KMBLeague 소속 구단 정보 현황")
    await ctx.send(embed=embed)

def get_east_asian_width(text):
    """한글과 영문/숫자의 글자 폭이 달라서 밀리는 현상을 방지하는 정렬 헬퍼 함수"""
    import unicodedata
    width = 0
    for char in text:
        if unicodedata.east_asian_width(char) in ('F', 'W', 'A'):
            width += 2  # 한글/이모지는 2칸
        else:
            width += 1  # 영문/숫자/공백은 1칸
    return width

def pad_text(text, target_width):
    """지정한 폭에 맞게 공백을 채워 정렬해 주는 함수"""
    text = str(text)
    current_width = get_east_asian_width(text)
    if current_width >= target_width:
        return text
    return text + " " * (target_width - current_width)

@bot.command(name="구단목록", aliases=["구단", "팀목록"])
async def show_clubs_table(ctx):
    # 1. Firestore에서 모든 구단 데이터 가져오기
    clubs_ref = db.collection("clubs")
    clubs_docs = clubs_ref.stream()
    
    club_list = []
    for doc in clubs_docs:
        data = doc.to_dict()
        club_list.append({
            "name": data.get("name", "-"),
            "owner": data.get("owner", "-"),
            "director": data.get("director", data.get("manager", "-")), # 감독/매니저 호환
            "coach": data.get("coach", "-"),
            "salary": data.get("totalSalary", 0)
        })
    
    if not club_list:
        await ctx.reply("📁 현재 등록된 구단 정보가 없습니다.")
        return

    # 2. 스크린샷과 동일한 상단 표 헤더 구성 (각 컬럼별 고정 너비 설정)
    # [ 구단 ] [ 구단주 ] [ 감독 ] [ 코치 ] [ 총 연봉 ]
    header = f"{pad_text('[ 구단 ]', 14)} {pad_text('[ 구단주 ]', 14)} {pad_text('[ 감독 ]', 14)} {pad_text('[ 코치 ]', 14)} {pad_text('[ 총 연봉 ]', 16)}"
    divider = "─" * len(header)
    
    table_lines = []
    table_lines.append(header)
    table_lines.append(divider)
    
    # 3. 각 구단별 데이터를 정렬 규칙에 맞춰 세 줄/네 줄로 누적
    for c in club_list:
        formatted_salary = f"{c['salary']:,}P" # 천 단위 쉼표 및 포인트 표기
        
        line = (
            f"{pad_text(c['name'], 14)} "
            f"{pad_text(c['owner'], 14)} "
            f"{pad_text(c['director'], 14)} "
            f"{pad_text(c['coach'], 14)} "
            f"{pad_text(formatted_salary, 16)}"
        )
        table_lines.append(line)
        
    # 4. 디스코드 코드 블록 (```) 효과를 주어 고정폭 폰트로 출력 (스크린샷 형태 구현)
    content = "\n".join(table_lines)
    
    embed = discord.Embed(
        title="🏛️ KMBLeague 구단별 프런트 및 연봉 현황 메인",
        color=discord.Color.dark_theme(),
        description=f"```\n{content}\n```"
    )
    embed.set_footer(text=f"총 {len(club_list)}개 구단 작동 중 • KMB System")
    
    await ctx.send(embed=embed)

def player_event_embed(data):
    team = data.get("team", "팀 미정")
    embed = discord.Embed(title="로스터 등록", color=team_color(team), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="선수", value=data.get("name", "-"), inline=True)
    embed.add_field(name="팀", value=team, inline=True)
    embed.add_field(name="포지션", value=data.get("position") or "-", inline=True)
    if data.get("number"):
        embed.add_field(name="등번호", value=data.get("number"), inline=True)
    return embed


def punishment_event_embed(data):
    embed = discord.Embed(
        title="처벌 기록 등록",
        color=0xEF4444 if "중" in normalize(data.get("status")) else 0x0F766E,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="대상", value=f"{normalize(data.get('nickname')) or '-'} ({normalize(data.get('team')) or '팀 미정'})", inline=False)
    embed.add_field(name="징계 여부", value=normalize(data.get("status")) or "-", inline=True)
    embed.add_field(name="징계 일자", value=normalize(data.get("dateText") or data.get("date")) or "-", inline=True)
    embed.add_field(name="처벌", value=f"{normalize(data.get('reason')) or '-'} -> {normalize(data.get('penalty')) or '-'}", inline=False)
    embed.add_field(name="해제", value=normalize(data.get("releaseDateText") or data.get("releaseDate")) or "-", inline=True)
    if normalize(data.get("note")):
        embed.add_field(name="비고", value=normalize(data.get("note"))[:1024], inline=False)
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


async def publish_punishment_firestore_event(title, embed, source, actor="-", announce=True):
    if announce:
        await send_punishment_announcement(embed)
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
    state = {"movements_initial": True, "players_initial": True, "punishments_initial": True}

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

    def on_punishments_snapshot(col_snapshot, changes, read_time):
        if state["punishments_initial"]:
            state["punishments_initial"] = False
            return
        for change in changes:
            if change.type.name != "ADDED":
                continue
            data = change.document.to_dict()
            embed = punishment_event_embed(data)
            actor = data.get("createdByName", "웹/시트")
            source = "Google Sheet" if data.get("source") == "google-sheet-import" else "웹/Discord"
            schedule_from_snapshot(publish_punishment_firestore_event("처벌 기록 등록", embed, source, actor, True))

    SNAPSHOT_UNSUBSCRIBES.append(db.collection("movements").on_snapshot(on_movements_snapshot))
    SNAPSHOT_UNSUBSCRIBES.append(db.collection("players").on_snapshot(on_players_snapshot))
    SNAPSHOT_UNSUBSCRIBES.append(db.collection("punishments").on_snapshot(on_punishments_snapshot))


async def punishment_sheet_sync_loop():
    while not bot.is_closed():
        try:
            synced, removed = await run_blocking(sync_punishments_from_sheet_sync)
            print(f"처벌 시트 동기화 완료: {synced}건, 로스터 제외 {removed}건")
        except Exception as exc:
            print("처벌 시트 동기화 실패:", repr(exc))
        await asyncio.sleep(60)


def start_punishment_sheet_sync():
    global PUNISHMENT_SYNC_STARTED
    if PUNISHMENT_SYNC_STARTED:
        return
    PUNISHMENT_SYNC_STARTED = True
    bot.loop.create_task(punishment_sheet_sync_loop())


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
    start_punishment_sheet_sync()
    print(f"MBO Python 봇 준비됨: {bot.user}")


bot.run(os.getenv("DISCORD_TOKEN"))
