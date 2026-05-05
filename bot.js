/**
 * MBO 선수 관리 Discord 봇
 *
 * 필요 패키지:
 *   npm install discord.js firebase-admin dotenv
 *
 * .env 파일 설정:
 *   DISCORD_TOKEN=your_discord_bot_token
 *   DISCORD_CHANNEL_ID=your_announcement_channel_id
 *   FIREBASE_PROJECT_ID=mbo-player
 *   FIREBASE_CLIENT_EMAIL=your_service_account_email
 *   FIREBASE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
 *
 * Firebase Admin SDK 서비스 계정 키는 Firebase 콘솔 → 프로젝트 설정 → 서비스 계정에서 발급하세요.
 */

import "dotenv/config";
import { Client, GatewayIntentBits, Events, EmbedBuilder } from "discord.js";
import { initializeApp, cert } from "firebase-admin/app";
import { getFirestore, Timestamp } from "firebase-admin/firestore";

// ─── Firebase Admin 초기화 ───────────────────────────────────────────────────
initializeApp({
  credential: cert({
    projectId: process.env.FIREBASE_PROJECT_ID,
    clientEmail: process.env.FIREBASE_CLIENT_EMAIL,
    privateKey: process.env.FIREBASE_PRIVATE_KEY?.replace(/\\n/g, "\n")
  })
});

const db = getFirestore();

// ─── Discord 클라이언트 초기화 ─────────────────────────────────────────────
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.GuildMembers,
    GatewayIntentBits.MessageContent
  ]
});

const ANNOUNCE_CHANNEL_ID = process.env.DISCORD_CHANNEL_ID;

const TEAM_META = {
  ODV: { name: "오버드라이브", color: 0xFF6A00 },
  RMS: { name: "레이 마린스", color: 0xC9982C },
  NDG: { name: "나이트 드래곤즈", color: 0x2b2b2b },
  CPX: { name: "청화 피닉스", color: 0x1B65BA },
  IH:  { name: "아이언 호네츠", color: 0xFEC804 },
  KRA: { name: "크라켄즈", color: 0xC00000 },
  PLT: { name: "클로베츠 플랜츠", color: 0x0CB218 },
  SLU: { name: "브레이브 슬러거즈", color: 0x850000 },
  "무소속": { name: "무소속", color: 0x64748B }
};

const MOVEMENT_LABELS = {
  TRADE: "트레이드",
  FA_SIGN: "FA 영입",
  RELEASE: "방출",
  RETIRE: "은퇴",
  NICKNAME: "닉네임 변경",
  TRANSFER: "이적",
  FORCED_RELEASE: "임의해지"
};

// ─── 헬퍼 함수 ────────────────────────────────────────────────────────────────
function normalize(value) {
  return String(value ?? "").trim();
}

function parseLines(value) {
  return normalize(value)
    .split(/[\r\n,，]+/)
    .map(l => l.trim())
    .filter(Boolean);
}

async function findPlayerByName(name, team = "") {
  const snapshot = await db.collection("players")
    .where("name", "==", name)
    .get();
  if (team) {
    const match = snapshot.docs.find(d => normalize(d.data().team).toLowerCase() === team.toLowerCase());
    return match ? { id: match.id, ...match.data() } : null;
  }
  return snapshot.empty ? null : { id: snapshot.docs[0].id, ...snapshot.docs[0].data() };
}

async function sendAnnouncement(embed) {
  if (!ANNOUNCE_CHANNEL_ID) return;
  try {
    const channel = await client.channels.fetch(ANNOUNCE_CHANNEL_ID);
    if (channel?.isTextBased()) await channel.send({ embeds: [embed] });
  } catch (err) {
    console.error("공지 채널 전송 실패:", err.message);
  }
}

function teamColor(teamCode) {
  return TEAM_META[teamCode]?.color ?? 0x0f766e;
}

function memberHasRole(message, roleName) {
  return Boolean(
    message.member?.roles?.cache?.some((role) => role.name.toLowerCase() === roleName.toLowerCase())
  );
}

// ─── 명령어 파서 ──────────────────────────────────────────────────────────────
/*
  지원 명령어 (접두사 !mbo):

  선수 목록 조회:
    !mbo 선수목록 [팀코드]
    예: !mbo 선수목록 RMS

  선수 이동 등록:
    !mbo 이동 <유형> <이전팀> <선수명> [새팀] [날짜 YYYY-MM-DD]
    예: !mbo 이동 방출 RMS 김민준
    예: !mbo 이동 트레이드 RMS 김민준 NDG 박도윤 2026-05-01
    예: !mbo 이동 임의해지 CPX 홍길동

  선수 등록:
    !mbo 선수등록 <이름> <팀> [포지션]
    예: !mbo 선수등록 홍길동 RMS 투수

  선수 삭제:
    !mbo 선수삭제 <이름> <팀>

  최근 이동 내역:
    !mbo 최근이동

  도움말:
    !mbo 도움말
*/

const PREFIX = "!mbo";

const TYPE_MAP = {
  "트레이드": "TRADE",
  "fa영입": "FA_SIGN",
  "FA영입": "FA_SIGN",
  "방출": "RELEASE",
  "은퇴": "RETIRE",
  "닉네임변경": "NICKNAME",
  "이적": "TRANSFER",
  "임의해지": "FORCED_RELEASE"
};

client.on(Events.MessageCreate, async (message) => {
  if (message.author.bot) return;
  if (!message.content.startsWith(PREFIX)) return;

  const args = message.content.slice(PREFIX.length).trim().split(/\s+/);
  const cmd = args[0];

  try {
    if (cmd === "도움말") {
      await handleHelp(message);
    } else if (cmd === "선수목록") {
      await handlePlayerList(message, args[1]);
    } else if (cmd === "이동") {
      await handleMovement(message, args.slice(1));
    } else if (cmd === "선수등록") {
      await handleAddPlayer(message, args.slice(1));
    } else if (cmd === "선수삭제") {
      await handleDeletePlayer(message, args.slice(1));
    } else if (cmd === "최근이동") {
      await handleRecentMovements(message);
    } else {
      await message.reply("알 수 없는 명령어입니다. `!mbo 도움말`을 입력해보세요.");
    }
  } catch (err) {
    console.error("명령어 처리 오류:", err);
    await message.reply(`오류가 발생했습니다: ${err.message}`);
  }
});

// ─── 도움말 ───────────────────────────────────────────────────────────────────
async function handleHelp(message) {
  const embed = new EmbedBuilder()
    .setTitle("📋 MBO 봇 명령어")
    .setColor(0x0f766e)
    .addFields(
      { name: "!mbo 선수목록 [팀코드]", value: "선수 목록 조회. 팀 코드 없으면 전체" },
      { name: "!mbo 이동 <유형> <이전팀> <선수1,선수2...> [새팀] [새팀선수1,선수2...] [날짜]",
        value: "유형: 트레이드, FA영입, 방출, 은퇴, 이적, 닉네임변경, 임의해지\n예: `!mbo 이동 방출 RMS 김민준`\n트레이드: `!mbo 이동 트레이드 RMS 김민준 NDG 박도윤`" },
      { name: "!mbo 선수등록 <이름> <팀> [포지션]", value: "선수를 로스터에 등록" },
      { name: "!mbo 선수삭제 <이름> <팀>", value: "선수를 로스터에서 삭제" },
      { name: "!mbo 최근이동", value: "최근 이동 내역 5건 조회" }
    )
    .setFooter({ text: "MBO Web Dashboard와 실시간 연동됩니다." });
  await message.reply({ embeds: [embed] });
}

// ─── 선수 목록 조회 ────────────────────────────────────────────────────────────
async function handlePlayerList(message, teamCode) {
  let query = db.collection("players").orderBy("name");
  if (teamCode) query = db.collection("players").where("team", "==", teamCode.toUpperCase()).orderBy("name");

  const snapshot = await query.get();
  if (snapshot.empty) {
    await message.reply(teamCode ? `**${teamCode}** 팀에 등록된 선수가 없습니다.` : "등록된 선수가 없습니다.");
    return;
  }

  // Group by team
  const byTeam = {};
  snapshot.docs.forEach(d => {
    const data = d.data();
    const team = data.team || "무소속";
    if (!byTeam[team]) byTeam[team] = [];
    const flag = data.forcedReleaseOriginalTeam ? " ⚠️임의해지" : "";
    byTeam[team].push(`${data.name}${data.position ? ` (${data.position})` : ""}${flag}`);
  });

  const embed = new EmbedBuilder()
    .setTitle(teamCode ? `${TEAM_META[teamCode.toUpperCase()]?.name ?? teamCode} 로스터` : "전체 선수 목록")
    .setColor(teamCode ? teamColor(teamCode.toUpperCase()) : 0x0f766e)
    .setTimestamp();

  Object.entries(byTeam).forEach(([team, players]) => {
    embed.addFields({ name: `${TEAM_META[team]?.name ?? team} (${players.length}명)`, value: players.join("\n") || "-" });
  });

  await message.reply({ embeds: [embed] });
}

// ─── 선수 이동 등록 ────────────────────────────────────────────────────────────
async function handleMovement(message, args) {
  /*
    args[0] = 이동유형
    args[1] = 이전팀
    args[2] = 이전팀선수 (쉼표 구분)
    args[3] = 새팀 (트레이드/FA영입/이적) 또는 날짜
    args[4] = 새팀선수 (트레이드 시) 또는 날짜
    args[5] = 날짜 (YYYY-MM-DD)
  */
  if (args.length < 3) {
    await message.reply("사용법: `!mbo 이동 <유형> <이전팀> <선수명> [새팀] [날짜]`");
    return;
  }

  const typeKr = args[0];
  const type = TYPE_MAP[typeKr];
  if (!type) {
    await message.reply(`알 수 없는 이동 유형입니다: ${typeKr}\n지원: ${Object.keys(TYPE_MAP).join(", ")}`);
    return;
  }

  const fromTeam = args[1].toUpperCase();
  const fromPlayers = parseLines(args[2]);

  let toTeam = "";
  let toPlayers = [];
  let dateStr = new Date().toISOString().slice(0, 10);

  if (type === "RETIRE" || type === "RELEASE" || type === "FORCED_RELEASE") {
    toTeam = "무소속";
    if (args[3] && /\d{4}-\d{2}-\d{2}/.test(args[3])) dateStr = args[3];
  } else if (type === "NICKNAME") {
    toTeam = fromTeam;
    if (args[3]) toPlayers = parseLines(args[3]);
    if (args[4] && /\d{4}-\d{2}-\d{2}/.test(args[4])) dateStr = args[4];
  } else if (type === "TRADE") {
    if (!args[3] || !args[4]) {
      await message.reply("트레이드: `!mbo 이동 트레이드 <이전팀> <선수> <새팀> <상대선수> [날짜]`");
      return;
    }
    toTeam = args[3].toUpperCase();
    toPlayers = parseLines(args[4]);
    if (args[5] && /\d{4}-\d{2}-\d{2}/.test(args[5])) dateStr = args[5];
  } else {
    // FA_SIGN, TRANSFER
    if (args[3]) toTeam = args[3].toUpperCase();
    if (args[4] && /\d{4}-\d{2}-\d{2}/.test(args[4])) dateStr = args[4];
  }

  // Validate players exist
  for (const name of fromPlayers) {
    const player = await findPlayerByName(name, fromTeam);
    if (!player) {
      await message.reply(`❌ 선수 목록에 없는 선수: **${name}** (팀: ${fromTeam})`);
      return;
    }
    if (
      ["FA_SIGN", "TRANSFER", "TRADE"].includes(type) &&
      player.forcedReleaseOriginalTeam &&
      toTeam.toLowerCase() !== player.forcedReleaseOriginalTeam.toLowerCase()
    ) {
      await message.reply(`⚠️ **${name}** 선수는 임의해지 상태입니다. 반드시 원래 팀(${player.forcedReleaseOriginalTeam})으로만 복귀할 수 있습니다.`);
      return;
    }
  }

  if (type === "TRADE") {
    for (const name of toPlayers) {
      const player = await findPlayerByName(name, toTeam);
      if (!player) {
        await message.reply(`❌ 선수 목록에 없는 선수: **${name}** (팀: ${toTeam})`);
        return;
      }
      if (
        player.forcedReleaseOriginalTeam &&
        fromTeam.toLowerCase() !== player.forcedReleaseOriginalTeam.toLowerCase()
      ) {
        await message.reply(`⚠️ **${name}** 선수는 임의해지 상태입니다. 반드시 원래 팀(${player.forcedReleaseOriginalTeam})으로만 복귀할 수 있습니다.`);
        return;
      }
    }
  }

  // Update players and record movements
  const batch = db.batch();

  for (const name of fromPlayers) {
    const player = await findPlayerByName(name, fromTeam);
    if (!player) continue;
    const ref = db.collection("players").doc(player.id);

    if (type === "NICKNAME" && toPlayers.length) {
      const idx = fromPlayers.indexOf(name);
      const newName = toPlayers[idx];
      batch.update(ref, {
        name: newName,
        transfer: `${name} -> ${newName} 닉네임 변경`,
        updatedAt: Timestamp.now()
      });
    } else if (type === "FORCED_RELEASE") {
      batch.update(ref, {
        team: "무소속",
        forcedReleaseOriginalTeam: fromTeam,
        transfer: `${dateStr} ${fromTeam}에서 임의해지`,
        updatedAt: Timestamp.now()
      });
    } else if (type === "FA_SIGN") {
      const updateData = {
        team: toTeam,
        transfer: `${dateStr} FA 영입: ${toTeam}`,
        updatedAt: Timestamp.now()
      };
      if (player.forcedReleaseOriginalTeam) updateData.forcedReleaseOriginalTeam = "";
      batch.update(ref, updateData);
    } else if (type === "TRADE") {
      batch.update(ref, {
        team: toTeam,
        transfer: `${dateStr} ${fromTeam}에서 ${toTeam}으로 트레이드`,
        updatedAt: Timestamp.now()
      });
    } else {
      const newTeam = (type === "RETIRE" || type === "RELEASE") ? "무소속" : toTeam;
      batch.update(ref, {
        team: newTeam,
        transfer: type === "RETIRE" ? `${dateStr} 은퇴` : type === "RELEASE" ? `${dateStr} ${fromTeam}에서 방출` : `${dateStr} ${fromTeam}에서 ${toTeam}으로 이적`,
        updatedAt: Timestamp.now()
      });
    }

    // Add movement record
    const movRef = db.collection("movements").doc();
    batch.set(movRef, {
      type,
      playerName: name,
      fromPlayers: [name],
      toPlayers: type === "TRADE" ? toPlayers : [],
      fromTeam,
      toTeam,
      date: dateStr,
      note: `Discord 봇 입력: ${message.author.tag}`,
      createdBy: "discord-bot",
      createdByName: message.author.tag,
      createdAt: Timestamp.now(),
      updatedAt: Timestamp.now()
    });
  }

  // Trade: also update toPlayers team
  if (type === "TRADE") {
    for (const name of toPlayers) {
      const player = await findPlayerByName(name, toTeam);
      if (!player) continue;
      const ref = db.collection("players").doc(player.id);
      batch.update(ref, {
        team: fromTeam,
        transfer: `${dateStr} ${toTeam}에서 ${fromTeam}으로 트레이드`,
        updatedAt: Timestamp.now()
      });
    }
  }

  await batch.commit();

  // Build announcement embed
  const typeLabel = MOVEMENT_LABELS[type] ?? type;
  const embed = new EmbedBuilder()
    .setTitle(`📢 ${typeLabel} 공지`)
    .setColor(teamColor(fromTeam))
    .addFields(
      { name: "이동 유형", value: typeLabel, inline: true },
      { name: "날짜", value: dateStr, inline: true },
      { name: "이전 팀", value: fromTeam, inline: true },
      { name: "선수", value: fromPlayers.join(", ") },
    )
    .setTimestamp()
    .setFooter({ text: `입력: ${message.author.tag}` });

  if (toTeam && type !== "RELEASE" && type !== "RETIRE" && type !== "FORCED_RELEASE") {
    embed.addFields({ name: "새 팀", value: toTeam, inline: true });
  }
  if (toPlayers.length && type !== "NICKNAME") {
    embed.addFields({ name: "상대 선수", value: toPlayers.join(", ") });
  }
  if (type === "FORCED_RELEASE") {
    embed.addFields({ name: "⚠️ 주의", value: "임의해지 선수는 원 소속팀으로만 복귀 가능합니다." });
  }

  await message.reply({ embeds: [embed] });
  await sendAnnouncement(embed);
}

// ─── 선수 등록 ────────────────────────────────────────────────────────────────
async function handleAddPlayer(message, args) {
  if (memberHasRole(message, "PLAYER")) {
    await message.reply("PLAYER 역할이 있는 사용자는 Discord 명령어로 선수 등록을 할 수 없습니다.");
    return;
  }

  if (args.length < 2) {
    await message.reply("사용법: `!mbo 선수등록 <이름> <팀> [포지션]`");
    return;
  }

  const name = args[0];
  const team = args[1].toUpperCase();
  const position = args.slice(2).join(" ");

  const existing = await findPlayerByName(name, team);
  if (existing) {
    await message.reply(`❌ 이미 **${team}** 팀에 **${name}** 선수가 등록되어 있습니다.`);
    return;
  }

  await db.collection("players").add({
    name, team, position,
    number: "",
    transfer: "",
    createdAt: Timestamp.now(),
    updatedAt: Timestamp.now()
  });

  const embed = new EmbedBuilder()
    .setTitle("✅ 선수 등록 완료")
    .setColor(teamColor(team))
    .addFields(
      { name: "이름", value: name, inline: true },
      { name: "팀", value: team, inline: true },
      { name: "포지션", value: position || "-", inline: true }
    )
    .setTimestamp()
    .setFooter({ text: `등록: ${message.author.tag}` });

  await message.reply({ embeds: [embed] });
  await sendAnnouncement(embed);
}

// ─── 선수 삭제 ────────────────────────────────────────────────────────────────
async function handleDeletePlayer(message, args) {
  if (args.length < 2) {
    await message.reply("사용법: `!mbo 선수삭제 <이름> <팀>`");
    return;
  }

  const name = args[0];
  const team = args[1].toUpperCase();

  const player = await findPlayerByName(name, team);
  if (!player) {
    await message.reply(`❌ **${team}** 팀에 **${name}** 선수를 찾을 수 없습니다.`);
    return;
  }

  await db.collection("players").doc(player.id).delete();
  await message.reply(`🗑️ **${team}** 팀 **${name}** 선수가 삭제되었습니다.`);
}

// ─── 최근 이동 내역 ────────────────────────────────────────────────────────────
async function handleRecentMovements(message) {
  const snapshot = await db.collection("movements")
    .orderBy("date", "desc")
    .limit(5)
    .get();

  if (snapshot.empty) {
    await message.reply("등록된 이동 내역이 없습니다.");
    return;
  }

  const embed = new EmbedBuilder()
    .setTitle("📋 최근 이동 내역 (5건)")
    .setColor(0x0f766e)
    .setTimestamp();

  snapshot.docs.forEach(d => {
    const m = d.data();
    const typeLabel = MOVEMENT_LABELS[m.type] ?? m.type;
    const route =
      m.type === "RELEASE" ? `${m.fromTeam} → 방출`
      : m.type === "RETIRE" ? `${m.fromTeam} → 무소속`
      : m.type === "FORCED_RELEASE" ? `${m.fromTeam} → 임의해지`
      : m.type === "NICKNAME" ? `${m.fromTeam} 닉네임 변경`
      : `${m.fromTeam} → ${m.toTeam}`;

    embed.addFields({
      name: `${m.date} · ${typeLabel}`,
      value: `${m.playerName}\n${route}`
    });
  });

  await message.reply({ embeds: [embed] });
}

// ─── Firestore 실시간 변경 감지 → Discord 공지 ────────────────────────────────
let isInitialLoad = true;

db.collection("movements")
  .orderBy("createdAt", "desc")
  .limit(1)
  .onSnapshot((snapshot) => {
    if (isInitialLoad) {
      isInitialLoad = false;
      return;
    }

    snapshot.docChanges().forEach(async (change) => {
      if (change.type !== "added") return;
      const m = change.doc.data();

      // Skip movements created by the bot itself
      if (m.createdBy === "discord-bot") return;

      const typeLabel = MOVEMENT_LABELS[m.type] ?? m.type;
      const route =
        m.type === "RELEASE" ? `${m.fromTeam} → 방출`
        : m.type === "RETIRE" ? `${m.fromTeam} → 무소속`
        : m.type === "FORCED_RELEASE" ? `${m.fromTeam} → 임의해지`
        : m.type === "NICKNAME" ? `${m.fromTeam} 닉네임 변경`
        : `${m.fromTeam} → ${m.toTeam}`;

      const embed = new EmbedBuilder()
        .setTitle(`📢 웹 대시보드에서 ${typeLabel} 등록됨`)
        .setColor(teamColor(m.fromTeam))
        .addFields(
          { name: "이동 유형", value: typeLabel, inline: true },
          { name: "날짜", value: m.date, inline: true },
          { name: "경로", value: route },
          { name: "선수", value: m.playerName || "-" },
          { name: "등록자", value: m.createdByName || "-", inline: true }
        )
        .setTimestamp()
        .setFooter({ text: "MBO Web Dashboard" });

      if (m.note) embed.addFields({ name: "메모", value: m.note });

      await sendAnnouncement(embed);
    });
  });

// ─── 봇 준비 ──────────────────────────────────────────────────────────────────
client.once(Events.ClientReady, (c) => {
  console.log(`✅ MBO 봇 준비됨: ${c.user.tag}`);
  console.log(`📢 공지 채널 ID: ${ANNOUNCE_CHANNEL_ID}`);
});

client.login(process.env.DISCORD_TOKEN);
