import os
import re
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore


load_dotenv()

PREFIX = "!mbo"
AUTHORIZED_USER_ID = 742989026625060914
ANNOUNCE_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

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

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX + " ", intents=intents, help_command=None)


def normalize(value):
    return str(value or "").strip()


def today():
    return datetime.now(timezone.utc).date().isoformat()


def is_date(value):
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalize(value)))


def parse_names(value):
    return [item.strip() for item in re.split(r"[\n\r,，]+", normalize(value)) if item.strip()]


def team_color(team):
    return TEAM_META.get(team, {}).get("color", 0x0F766E)


def is_authorized(ctx):
    if ctx.author.id == AUTHORIZED_USER_ID:
        return True
    if getattr(ctx.author.guild_permissions, "administrator", False):
        return True
    return any(role.name == "관리자" for role in getattr(ctx.author, "roles", []))


async def guard(ctx):
    if is_authorized(ctx):
        return True
    await ctx.reply("이 명령어는 Discord 관리자 또는 지정된 관리자만 사용할 수 있습니다.")
    return False


def find_player(name, team=""):
    docs = db.collection("players").where("name", "==", name).stream()
    matches = [{"id": doc.id, **doc.to_dict()} for doc in docs]
    if not team:
        return matches[0] if matches else None
    lowered_team = team.lower()
    return next((player for player in matches if normalize(player.get("team")).lower() == lowered_team), None)


def get_required_player(name, team):
    player = find_player(name, team)
    if not player:
        raise ValueError(f"선수 목록에 없는 선수입니다: {name} ({team})")
    return player


def validate_forced_return(player, target_team):
    original = normalize(player.get("forcedReleaseOriginalTeam"))
    if original and original.lower() != normalize(target_team).lower():
        raise ValueError(f"{player.get('name')} 선수는 임의해지 상태라 원래 팀({original})으로만 복귀할 수 있습니다.")


async def send_announcement(embed):
    if not ANNOUNCE_CHANNEL_ID:
        return
    channel = bot.get_channel(int(ANNOUNCE_CHANNEL_ID)) or await bot.fetch_channel(int(ANNOUNCE_CHANNEL_ID))
    if channel:
        await channel.send(embed=embed)


def movement_embed(kind, date, from_team, players, to_team=""):
    label = MOVEMENT_LABELS.get(kind, kind)
    embed = discord.Embed(title=f"{label} 공지", color=team_color(from_team), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="이동 유형", value=label, inline=True)
    embed.add_field(name="날짜", value=date, inline=True)
    embed.add_field(name="이전 팀", value=from_team, inline=True)
    if to_team and kind not in {"RELEASE", "RETIRE", "FORCED_RELEASE"}:
        embed.add_field(name="새 팀", value=to_team, inline=True)
    embed.add_field(name="선수", value=", ".join(players), inline=False)
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


def update_player(batch, player, payload):
    batch.update(db.collection("players").document(player["id"]), {**payload, "updatedAt": firestore.SERVER_TIMESTAMP})


@bot.command(name="도움말")
async def help_command(ctx):
    if not await guard(ctx):
        return
    embed = discord.Embed(title="MBO Python 봇 명령어", color=0x0F766E)
    embed.add_field(name="!mbo 트레이드 <이전팀> <선수명> <새팀> <상대선수명> [날짜]", value="두 팀 선수의 로스터 팀을 서로 바꿉니다.", inline=False)
    embed.add_field(name="!mbo 방출 <팀> <선수명> [날짜]", value="선수를 무소속으로 이동합니다.", inline=False)
    embed.add_field(name="!mbo 은퇴 <팀> <선수명> [날짜]", value="선수를 무소속으로 이동하고 은퇴 내역을 남깁니다.", inline=False)
    embed.add_field(name="!mbo 임의해지 <팀> <선수명> [날짜]", value="선수를 무소속으로 이동하고 원 소속팀 복귀 제한을 겁니다.", inline=False)
    embed.add_field(name="!mbo 최근이동", value="최근 이동 내역 5건을 조회합니다.", inline=False)
    embed.add_field(name="!mbo 유효성검사 <타순표>", value="타순표 닉네임이 로스터에 있는지 검사합니다. 여러 줄 입력 가능.", inline=False)
    embed.add_field(name="타순표 예시", value="```!mbo 유효성검사\n1. Axrq__ CF\n2번 CUCCl 1B\n3. _w0nyu1 LF```", inline=False)
    await ctx.reply(embed=embed)


@bot.command(name="최근이동")
async def recent_movements(ctx):
    if not await guard(ctx):
        return
    docs = db.collection("movements").order_by("date", direction=firestore.Query.DESCENDING).limit(5).stream()
    embed = discord.Embed(title="최근 이동 내역", color=0x0F766E, timestamp=datetime.now(timezone.utc))
    count = 0
    for doc in docs:
        count += 1
        data = doc.to_dict()
        label = MOVEMENT_LABELS.get(data.get("type"), data.get("type", "이동"))
        route = f"{data.get('fromTeam', '-') } -> {data.get('toTeam', '-')}"
        if data.get("type") == "RELEASE":
            route = f"{data.get('fromTeam', '-')} -> 방출"
        elif data.get("type") == "RETIRE":
            route = f"{data.get('fromTeam', '-')} -> 은퇴"
        elif data.get("type") == "FORCED_RELEASE":
            route = f"{data.get('fromTeam', '-')} -> 임의해지"
        embed.add_field(name=f"{data.get('date', '-')} · {label}", value=f"{data.get('playerName', '-')}\n{route}", inline=False)
    if not count:
        embed.description = "등록된 이동 내역이 없습니다."
    await ctx.reply(embed=embed)


@bot.command(name="방출")
async def release(ctx, team: str = "", players_text: str = "", date_text: str = ""):
    await simple_movement(ctx, "RELEASE", team, players_text, date_text)


@bot.command(name="은퇴")
async def retire(ctx, team: str = "", players_text: str = "", date_text: str = ""):
    await simple_movement(ctx, "RETIRE", team, players_text, date_text)


@bot.command(name="임의해지")
async def forced_release(ctx, team: str = "", players_text: str = "", date_text: str = ""):
    await simple_movement(ctx, "FORCED_RELEASE", team, players_text, date_text)


async def simple_movement(ctx, kind, team, players_text, date_text):
    if not await guard(ctx):
        return
    if not team or not players_text:
        await ctx.reply(f"사용법: `!mbo {MOVEMENT_LABELS[kind]} <팀> <선수명> [날짜]`")
        return
    date = date_text if is_date(date_text) else today()
    from_team = team.upper()
    players = parse_names(players_text)
    batch = db.batch()
    for name in players:
        player = get_required_player(name, from_team)
        payload = {
            "team": "무소속",
            "transfer": f"{date} {from_team}에서 {MOVEMENT_LABELS[kind]}",
        }
        if kind == "FORCED_RELEASE":
            payload["forcedReleaseOriginalTeam"] = from_team
        else:
            payload["forcedReleaseOriginalTeam"] = ""
        update_player(batch, player, payload)
        add_movement(batch, kind, name, from_team, "무소속", date, f"Discord Python 봇 입력: {ctx.author}", [name], [])
    batch.commit()
    embed = movement_embed(kind, date, from_team, players, "무소속")
    await ctx.reply(embed=embed)
    await send_announcement(embed)


@bot.command(name="트레이드")
async def trade(ctx, from_team: str = "", from_players_text: str = "", to_team: str = "", to_players_text: str = "", date_text: str = ""):
    if not await guard(ctx):
        return
    if not from_team or not from_players_text or not to_team or not to_players_text:
        await ctx.reply("사용법: `!mbo 트레이드 <이전팀> <선수명> <새팀> <상대선수명> [날짜]`")
        return
    date = date_text if is_date(date_text) else today()
    from_team = from_team.upper()
    to_team = to_team.upper()
    from_players = parse_names(from_players_text)
    to_players = parse_names(to_players_text)

    from_records = [get_required_player(name, from_team) for name in from_players]
    to_records = [get_required_player(name, to_team) for name in to_players]
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
        f"Discord Python 봇 입력: {ctx.author}",
        from_players,
        to_players,
    )
    batch.commit()
    embed = movement_embed("TRADE", date, from_team, from_players, to_team)
    embed.add_field(name="상대 선수", value=", ".join(to_players), inline=False)
    await ctx.reply(embed=embed)
    await send_announcement(embed)


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
        await ctx.reply("사용법: `!mbo 유효성검사 <타순표>`\n여러 줄 타순표도 그대로 붙여넣을 수 있습니다.")
        return
    entries = [entry for entry in (parse_lineup_line(line) for line in lineup_text.splitlines()) if entry]
    if not entries:
        await ctx.reply("검사할 선수를 찾지 못했습니다.")
        return
    found = []
    missing = []
    for entry in entries:
        player = find_player(entry["name"])
        if player:
            found.append(f"{entry['name']} {entry['position']} · {player.get('team', '팀 미정')}")
        else:
            missing.append(f"{entry['name']} {entry['position']}".strip())
    embed = discord.Embed(title="로스터 유효성 검사", color=0x0F766E)
    embed.add_field(name=f"등록됨 ({len(found)})", value="\n".join(found)[:1024] if found else "-", inline=False)
    embed.add_field(name=f"미등록 ({len(missing)})", value="\n".join(missing)[:1024] if missing else "-", inline=False)
    await ctx.reply(embed=embed)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.reply("알 수 없는 명령어입니다. `!mbo 도움말`을 입력해보세요.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.reply("명령어 형식이 올바르지 않습니다. `!mbo 도움말`을 확인해주세요.")
        return
    print("명령어 처리 오류:", repr(error))
    await ctx.reply(f"오류가 발생했습니다: {error}")


@bot.event
async def on_ready():
    print(f"MBO Python 봇 준비됨: {bot.user}")


bot.run(os.getenv("DISCORD_TOKEN"))
