import os
import re
import asyncio
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
    text = normalize(value)
    text = text.replace("，", ",")
    return [item.strip() for item in re.split(r"[\n\r,]+", text) if item.strip()]


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
        raise ValueError("사용법: `!mbo 이동 트레이드 <이전팀> <보내는선수> <새팀> <받는선수들> [날짜]`")

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


def parse_simple_movement_args(args):
    body, date = split_date_from_args(args)
    parts = body.split()

    if len(parts) < 2:
        raise ValueError("사용법: `!mbo 이동 <방출|은퇴|임의해지> <팀> <선수명들> [날짜]`")

    team = parts[0].upper()
    players_text = " ".join(parts[1:])
    players = parse_names(players_text)

    if not players:
        raise ValueError("선수를 찾지 못했습니다.")

    return team, players, date


def commit_simple_movement_sync(kind, team, players, date, author_text):
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
        add_movement(batch, kind, name, from_team, "무소속", date, f"Discord Python 봇 입력: {author_text}", [name], [])

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


@bot.command(name="도움말")
async def help_command(ctx):
    if not await guard(ctx):
        return

    embed = discord.Embed(title="MBO Python 봇 명령어", color=0x0F766E)

    embed.add_field(
        name="!mbo 이동 트레이드 <이전팀> <보내는선수> <새팀> <받는선수들> [날짜]",
        value="선수를 트레이드합니다. 받는 선수는 쉼표로 여러 명 입력할 수 있습니다.",
        inline=False,
    )

    embed.add_field(
        name="!mbo 트레이드 <이전팀> <보내는선수> <새팀> <받는선수들> [날짜]",
        value="이동 트레이드와 동일합니다.",
        inline=False,
    )

    embed.add_field(
        name="!mbo 이동 방출 <팀> <선수명들> [날짜]",
        value="선수를 무소속으로 이동합니다. 여러 명은 쉼표로 구분합니다.",
        inline=False,
    )

    embed.add_field(
        name="!mbo 이동 은퇴 <팀> <선수명들> [날짜]",
        value="선수를 무소속으로 이동하고 은퇴 내역을 남깁니다.",
        inline=False,
    )

    embed.add_field(
        name="!mbo 이동 임의해지 <팀> <선수명들> [날짜]",
        value="선수를 무소속으로 이동하고 원 소속팀 복귀 제한을 겁니다.",
        inline=False,
    )

    embed.add_field(
        name="!mbo 최근이동",
        value="최근 이동 내역 5건을 조회합니다.",
        inline=False,
    )

    embed.add_field(
        name="!mbo 최근 이동",
        value="최근 이동 내역 5건을 조회합니다.",
        inline=False,
    )

    embed.add_field(
        name="!mbo 유효성검사 <타순표>",
        value="타순표 닉네임이 로스터에 있는지 검사합니다. 여러 줄 입력 가능.",
        inline=False,
    )

    embed.add_field(
        name="트레이드 예시",
        value="```!mbo 이동 트레이드 CPX papaya_yaru ODV KR_Windy, chan_seu1_12 2026-05-05```",
        inline=False,
    )

    embed.add_field(
        name="타순표 예시",
        value="```!mbo 유효성검사\n1. Axrq__ CF\n2번 CUCCl 1B\n3. _w0nyu1 LF```",
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
    if not await guard(ctx):
        return

    movement_type = normalize(movement_type)

    if movement_type == "트레이드":
        from_team, from_players, to_team, to_players, date = parse_trade_args(args)

        await run_blocking(
            commit_trade_sync,
            from_team,
            from_players,
            to_team,
            to_players,
            date,
            str(ctx.author),
        )

        embed = movement_embed("TRADE", date, from_team, from_players, to_team)
        embed.add_field(name="상대 선수", value=", ".join(to_players), inline=False)

        await ctx.reply(embed=embed)
        await send_announcement(embed)
        return

    if movement_type == "방출":
        team, players, date = parse_simple_movement_args(args)

        await run_blocking(
            commit_simple_movement_sync,
            "RELEASE",
            team,
            players,
            date,
            str(ctx.author),
        )

        embed = movement_embed("RELEASE", date, team, players, "무소속")

        await ctx.reply(embed=embed)
        await send_announcement(embed)
        return

    if movement_type == "은퇴":
        team, players, date = parse_simple_movement_args(args)

        await run_blocking(
            commit_simple_movement_sync,
            "RETIRE",
            team,
            players,
            date,
            str(ctx.author),
        )

        embed = movement_embed("RETIRE", date, team, players, "무소속")

        await ctx.reply(embed=embed)
        await send_announcement(embed)
        return

    if movement_type == "임의해지":
        team, players, date = parse_simple_movement_args(args)

        await run_blocking(
            commit_simple_movement_sync,
            "FORCED_RELEASE",
            team,
            players,
            date,
            str(ctx.author),
        )

        embed = movement_embed("FORCED_RELEASE", date, team, players, "무소속")

        await ctx.reply(embed=embed)
        await send_announcement(embed)
        return

    await ctx.reply("사용법: `!mbo 이동 <트레이드|방출|은퇴|임의해지> ...` 또는 `!mbo 도움말`")


@bot.command(name="트레이드")
async def trade(ctx, *, args: str = ""):
    if not await guard(ctx):
        return

    from_team, from_players, to_team, to_players, date = parse_trade_args(args)

    await run_blocking(
        commit_trade_sync,
        from_team,
        from_players,
        to_team,
        to_players,
        date,
        str(ctx.author),
    )

    embed = movement_embed("TRADE", date, from_team, from_players, to_team)
    embed.add_field(name="상대 선수", value=", ".join(to_players), inline=False)

    await ctx.reply(embed=embed)
    await send_announcement(embed)


@bot.command(name="방출")
async def release(ctx, *, args: str = ""):
    await simple_movement(ctx, "RELEASE", args)


@bot.command(name="은퇴")
async def retire(ctx, *, args: str = ""):
    await simple_movement(ctx, "RETIRE", args)


@bot.command(name="임의해지")
async def forced_release(ctx, *, args: str = ""):
    await simple_movement(ctx, "FORCED_RELEASE", args)


async def simple_movement(ctx, kind, args):
    if not await guard(ctx):
        return

    team, players, date = parse_simple_movement_args(args)

    await run_blocking(
        commit_simple_movement_sync,
        kind,
        team,
        players,
        date,
        str(ctx.author),
    )

    embed = movement_embed(kind, date, team, players, "무소속")

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
        player = await find_player(entry["name"])

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
    original = getattr(error, "original", error)

    if isinstance(error, commands.CommandNotFound):
        await ctx.reply("알 수 없는 명령어입니다. `!mbo 도움말`을 입력해보세요.")
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply("명령어에 필요한 값이 부족합니다. `!mbo 도움말`을 확인해주세요.")
        return

    if isinstance(error, commands.BadArgument):
        await ctx.reply("명령어 형식이 올바르지 않습니다. `!mbo 도움말`을 확인해주세요.")
        return

    if isinstance(original, ValueError):
        await ctx.reply(str(original))
        return

    print("명령어 처리 오류:", repr(original))
    await ctx.reply(f"오류가 발생했습니다: {original}")


@bot.event
async def on_ready():
    print(f"MBO Python 봇 준비됨: {bot.user}")


bot.run(os.getenv("DISCORD_TOKEN"))
