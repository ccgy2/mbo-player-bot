"""Guild-scoped Discord roster lifecycle. Importing this module makes no network calls.

Only ``players`` identity/lifecycle fields are changed. Team, punishments, movements,
positions and shirt numbers remain intact when a member leaves or re-registers.
"""

import asyncio
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def text(value):
    return str(value if value is not None else "").strip()


def snowflake(value):
    value = text(value)
    return value if value.isdigit() and 15 <= len(value) <= 25 else ""


def is_active_player(record):
    """Missing lifecycle fields are legacy active players, including manual entries."""
    return record.get("rosterStatus") != "left_guild"


@dataclass(frozen=True)
class Identity:
    guild_id: str
    user_id: str
    username: str
    display_name: str

    @classmethod
    def from_member(cls, member):
        guild_id = snowflake(member.guild.id)
        user_id = snowflake(member.id)
        if not guild_id or not user_id:
            raise ValueError("Discord 서버/사용자 ID를 확인할 수 없습니다.")
        return cls(guild_id, user_id, str(member),
                   text(getattr(member, "display_name", "")) or text(member.name))


class FirestoreRosterStore:
    """Synchronous Firestore adapter, serialized across the bot's worker threads.

    The old reaction importer stored discordId without a guild. Only the resolved
    roster guild may adopt those records; records scoped to another guild are never
    touched. A name is never used to process a departure.
    """

    def __init__(self, db, server_timestamp, delete_field):
        self.db = db
        self.timestamp = server_timestamp
        self.delete_field = delete_field
        self.lock = threading.RLock()

    def _by_id(self, user_id):
        found = {}
        # Older imports may have stored numeric IDs. New writes always use strings.
        values = (user_id, int(user_id)) if int(user_id) <= (2 ** 63 - 1) else (user_id,)
        for value in values:
            for snapshot in self.db.collection("players").where("discordId", "==", value).stream():
                found[snapshot.id] = snapshot
        return list(found.values())

    def _scoped(self, user_id, guild_id, adopt_legacy=True):
        snapshots = self._by_id(user_id)
        foreign = any(text(item.to_dict().get("discordGuildId")) not in {"", guild_id}
                      for item in snapshots)
        return [item for item in snapshots
                if text(item.to_dict().get("discordGuildId")) == guild_id
                or (adopt_legacy and not foreign and not text(item.to_dict().get("discordGuildId")))]

    def linked_user_ids(self, guild_id):
        with self.lock:
            snapshots = list(self.db.collection("players").stream())
            foreign = {snowflake(item.to_dict().get("discordId")) for item in snapshots
                       if text(item.to_dict().get("discordGuildId"))
                       and text(item.to_dict().get("discordGuildId")) != guild_id}
            return {user_id for item in snapshots
                    if (user_id := snowflake(item.to_dict().get("discordId")))
                    and (text(item.to_dict().get("discordGuildId")) == guild_id
                         or (not text(item.to_dict().get("discordGuildId")) and user_id not in foreign))}

    def _patch(self, snapshot, updates):
        if updates:
            snapshot.reference.update({**updates, "updatedAt": self.timestamp})

    def _identity_updates(self, snapshot, identity):
        data = snapshot.to_dict()
        desired = {"discordId": identity.user_id, "discordGuildId": identity.guild_id,
                   "discordName": identity.username, "discordDisplayName": identity.display_name,
                   "discordSyncVersion": 1}
        updates = {key: value for key, value in desired.items() if data.get(key) != value}
        previous_name = text(data.get("name"))
        conflict = False
        if identity.display_name and previous_name != identity.display_name:
            collisions = [item for item in self.db.collection("players")
                          .where("name", "==", identity.display_name).stream()
                          if item.id != snapshot.id]
            if collisions:
                conflict = True
                if data.get("discordNameConflict") != identity.display_name:
                    updates["discordNameConflict"] = identity.display_name
            else:
                updates.update(name=identity.display_name, discordPreviousDisplayName=previous_name)
                if "discordNameConflict" in data:
                    updates["discordNameConflict"] = self.delete_field
        elif "discordNameConflict" in data:
            updates["discordNameConflict"] = self.delete_field
        return updates, {"changed": "name" in updates, "conflict": conflict,
                         "previousName": previous_name, "displayName": identity.display_name}

    def refresh_identity(self, identity):
        """A nickname update/join cannot make a departed player active again."""
        with self.lock:
            matches = self._scoped(identity.user_id, identity.guild_id)
            if not matches:
                return None
            if len(matches) != 1:
                return {"changed": False, "conflict": True, "displayName": identity.display_name,
                        "previousName": "", "reason": "duplicate_discord_identity"}
            updates, result = self._identity_updates(matches[0], identity)
            self._patch(matches[0], updates)
            return result

    def register(self, identity, team, source, author, allow_restore=True):
        """Register from a verified role/reaction; never reassign an existing team."""
        with self.lock:
            matches = self._scoped(identity.user_id, identity.guild_id)
            status = "updated"
            if len(matches) > 1:
                return "conflict", identity.display_name
            if not matches:
                names = list(self.db.collection("players").where("name", "==", identity.display_name).stream())
                candidates = [item for item in names
                              if not text(item.to_dict().get("discordId"))
                              and text(item.to_dict().get("discordGuildId")) in {"", identity.guild_id}
                              and text(item.to_dict().get("team")).casefold() == text(team).casefold()]
                # A display name is not an account identifier: never steal a link,
                # select the first homonym or move a similarly named player's team.
                if names and (len(names) != 1 or len(candidates) != 1):
                    return "conflict", identity.display_name
                if candidates:
                    matches = candidates
                    status = "linked"
            if matches:
                snapshot = matches[0]
                data = snapshot.to_dict()
                if not is_active_player(data) and not allow_restore:
                    return "awaiting_registration", text(data.get("name"))
                updates, result = self._identity_updates(snapshot, identity)
                if data.get("rosterStatus") != "active":
                    updates["rosterStatus"] = "active"
                if data.get("discordRegistrationSource") != source:
                    updates["discordRegistrationSource"] = source
                if not is_active_player(data):
                    updates["discordRejoinedAt"] = self.timestamp
                    status = "restored"
                for field in ("discordLeftAt", "discordLeftReason"):
                    if field in data:
                        updates[field] = self.delete_field
                self._patch(snapshot, updates)
                if result["conflict"]:
                    return "name_conflict", text(data.get("name"))
                return ("renamed" if result["changed"] and status == "updated" else status), identity.display_name

            # Deterministic document IDs also make duplicate reaction deliveries
            # idempotent. create() never overwrites an existing document.
            reference = self.db.collection("players").document(f"discord-{identity.guild_id}-{identity.user_id}")
            now_date = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
            payload = {"name": identity.display_name, "team": team, "position": "", "number": "",
                       "transfer": f"{now_date} Discord 자동 등록: {team}",
                       "discordId": identity.user_id, "discordGuildId": identity.guild_id,
                       "discordName": identity.username, "discordDisplayName": identity.display_name,
                       "discordSyncVersion": 1, "discordRegistrationSource": source,
                       "rosterStatus": "active", "createdBy": "discord-reaction-import" if source == "reaction" else "discord-role-import",
                       "createdByName": author, "createdAt": self.timestamp, "updatedAt": self.timestamp}
            try:
                reference.create(payload)
            except Exception:
                # If another delivery already created this exact identity, retry
                # the ordinary merge path. A database outage is never swallowed.
                existing = reference.get()
                if not existing.exists:
                    raise
                existing_data = existing.to_dict()
                if (text(existing_data.get("discordId")) != identity.user_id
                        or text(existing_data.get("discordGuildId")) != identity.guild_id):
                    return "conflict", identity.display_name
                # No recursive create retry: leave cross-process races to the next
                # verified sync instead of risking an unbounded retry on corruption.
                return "updated", text(existing_data.get("name"))
            return "created", identity.display_name

    def deactivate(self, guild_id, user_id, reason):
        """Idempotent soft removal, applied to all explicitly linked duplicate rows."""
        guild_id, user_id = snowflake(guild_id), snowflake(user_id)
        if not guild_id or not user_id:
            return 0
        with self.lock:
            changed = 0
            for snapshot in self._scoped(user_id, guild_id):
                data = snapshot.to_dict()
                if not is_active_player(data):
                    continue
                self._patch(snapshot, {"rosterStatus": "left_guild", "discordId": user_id,
                                       "discordGuildId": guild_id, "discordSyncVersion": 1,
                                       "discordLeftAt": self.timestamp, "discordLeftReason": reason})
                changed += 1
            return changed

    def link_manual(self, identity, name, team, author):
        """Explicit administrator repair for a legacy player whose nickname differs."""
        with self.lock:
            matches = [item for item in self.db.collection("players").where("name", "==", name).stream()
                       if text(item.to_dict().get("team")).casefold() == text(team).casefold()]
            if len(matches) != 1:
                raise ValueError("선수명과 팀이 정확히 일치하는 선수 1명을 지정해주세요.")
            snapshot = matches[0]
            data = snapshot.to_dict()
            if text(data.get("discordId")) not in {"", identity.user_id}:
                raise ValueError("이미 다른 Discord 계정과 연결된 선수입니다. 기존 연결을 자동 덮어쓰지 않습니다.")
            if text(data.get("discordGuildId")) not in {"", identity.guild_id}:
                raise ValueError("다른 Discord 서버에 연결된 선수입니다.")
            existing = self._scoped(identity.user_id, identity.guild_id)
            if any(item.id != snapshot.id for item in existing):
                raise ValueError("이 Discord 계정은 이미 다른 선수와 연결되어 있습니다.")
            # Linking repairs identity, without changing the recorded player name
            # or granting a new roster registration to a departed player.
            self._patch(snapshot, {"discordId": identity.user_id, "discordGuildId": identity.guild_id,
                                   "discordName": identity.username, "discordDisplayName": identity.display_name,
                                   "discordSyncVersion": 1, "discordLinkedBy": author})
            return text(data.get("name"))


def is_unknown_member(error):
    # Unknown Guild, Forbidden, rate limiting and network errors are NOT evidence
    # of a departure. Discord's Unknown Member error code is 10007.
    return getattr(error, "status", None) == 404 and getattr(error, "code", None) == 10007


class RosterSynchronizer:
    """Async coordinator; all destructive-inference paths require REST confirmation."""

    def __init__(self, bot, store, guild_id, role_ids=(), default_team="무소속", logger=print,
                 registration_enabled=True):
        self.bot, self.store = bot, store
        self.guild_id = snowflake(guild_id)
        self.role_ids = {snowflake(value) for value in role_ids if snowflake(value)}
        self.default_team = default_team
        self.registration_enabled = registration_enabled
        self.log = logger
        self._locks = {}
        self._epochs = {}
        self._reconcile_lock = asyncio.Lock()

    def accepts(self, guild_id):
        return bool(self.guild_id and snowflake(guild_id) == self.guild_id)

    def _lock(self, user_id):
        return self._locks.setdefault(str(user_id), asyncio.Lock())

    def note_event(self, user_id):
        user_id = str(user_id)
        self._epochs[user_id] = self._epochs.get(user_id, 0) + 1
        return self._epochs[user_id]

    def has_registration_role(self, member):
        return self.registration_enabled and bool(self.role_ids.intersection(str(role.id) for role in member.roles))

    async def register_member(self, member, source="reaction", author="Discord 자동 등록", allow_restore=True, team=None):
        if not self.registration_enabled or getattr(member, "bot", False) or not self.accepts(member.guild.id):
            return "ignored", ""
        user_id = str(member.id)
        epoch = self.note_event(user_id)
        async with self._lock(user_id):
            # A reaction's cached Member can already have left the guild.
            try:
                current = await member.guild.fetch_member(member.id)
            except Exception as error:
                if is_unknown_member(error):
                    if self._epochs.get(user_id) == epoch:
                        await asyncio.to_thread(self.store.deactivate, self.guild_id, user_id, "registration_member_absent")
                    return "absent", ""
                raise
            if self._epochs.get(user_id) != epoch:
                return "stale", ""
            if not self.accepts(current.guild.id) or str(current.id) != user_id or current.bot:
                return "ignored", ""
            if source == "role" and not self.has_registration_role(current):
                return "ignored", ""
            return await asyncio.to_thread(self.store.register, Identity.from_member(current),
                                           team or self.default_team, source, author, allow_restore)

    async def member_changed(self, member):
        if getattr(member, "bot", False) or not self.accepts(member.guild.id):
            return None
        if self.has_registration_role(member):
            return await self.register_member(member, "role")
        user_id = str(member.id)
        epoch = self.note_event(user_id)
        async with self._lock(user_id):
            if self._epochs.get(user_id) != epoch:
                return None
            return await asyncio.to_thread(self.store.refresh_identity, Identity.from_member(member))

    async def member_removed(self, guild_id, user_id):
        if not self.accepts(guild_id) or str(user_id) == str(getattr(self.bot.user, "id", "")):
            return 0
        user_id = str(user_id)
        self.note_event(user_id)
        async with self._lock(user_id):
            return await asyncio.to_thread(self.store.deactivate, self.guild_id, user_id, "member_remove")

    async def reconcile(self, guild):
        if (not self.accepts(guild.id) or getattr(guild, "unavailable", False)
                or self._reconcile_lock.locked()):
            return {"skipped": True, "reason": "unavailable_or_wrong_guild"}
        async with self._reconcile_lock:
            epochs = dict(self._epochs)
            try:
                members = {str(member.id): member async for member in guild.fetch_members(limit=None)}
            except Exception as error:
                self.log(f"선수 동기화 보류: 서버 멤버 전체 조회 실패 ({type(error).__name__})")
                return {"skipped": True, "reason": "incomplete_member_fetch"}
            bot_id = str(getattr(self.bot.user, "id", ""))
            if (not members or bot_id not in members or getattr(guild, "unavailable", False)
                    or any(not self.accepts(member.guild.id) for member in members.values())):
                self.log("선수 동기화 보류: 멤버 목록의 완전성 또는 서버를 확인할 수 없습니다.")
                return {"skipped": True, "reason": "incomplete_member_fetch"}

            linked = await asyncio.to_thread(self.store.linked_user_ids, self.guild_id)
            removed = registered = refreshed = failures = conflicts = 0
            targets = linked | {user_id for user_id, member in members.items()
                                if not member.bot and self.has_registration_role(member)}
            for user_id in sorted(targets):
                if getattr(guild, "unavailable", False):
                    failures += 1
                    break
                if self._epochs.get(user_id, 0) != epochs.get(user_id, 0):
                    continue
                async with self._lock(user_id):
                    if self._epochs.get(user_id, 0) != epochs.get(user_id, 0):
                        continue
                    try:
                        # Confirm each target individually. A partial successful
                        # pagination or a join during the sweep cannot cause removal.
                        current = await guild.fetch_member(int(user_id))
                    except Exception as error:
                        if (is_unknown_member(error) and not getattr(guild, "unavailable", False)
                                and self._epochs.get(user_id, 0) == epochs.get(user_id, 0)):
                            removed += await asyncio.to_thread(self.store.deactivate, self.guild_id, user_id, "reconcile_not_found")
                        else:
                            failures += 1
                        continue
                    if self._epochs.get(user_id, 0) != epochs.get(user_id, 0):
                        continue
                    if (current.bot or not self.accepts(current.guild.id) or str(current.id) != user_id
                            or getattr(guild, "unavailable", False)):
                        continue
                    if self.has_registration_role(current):
                        status, _ = await asyncio.to_thread(self.store.register, Identity.from_member(current),
                                                           self.default_team, "role", "Discord 역할 정기 동기화", True)
                        if status in {"created", "restored", "linked"}:
                            registered += 1
                        elif status in {"conflict", "name_conflict"}:
                            conflicts += 1
                    else:
                        result = await asyncio.to_thread(self.store.refresh_identity, Identity.from_member(current))
                        refreshed += bool(result and result.get("changed"))
            return {"skipped": False, "members": len(members), "removed": removed,
                    "registered": registered, "renamed": refreshed, "failures": failures, "conflicts": conflicts}
