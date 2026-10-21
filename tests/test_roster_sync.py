"""Offline regression tests. No Firebase credentials, bot login or HTTP requests."""

import asyncio
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from roster_sync import FirestoreRosterStore, Identity, RosterSynchronizer, is_active_player

GUILD = "100000000000000001"
OTHER_GUILD = "100000000000000002"
USER = "200000000000000001"
OTHER_USER = "200000000000000002"
BOT_ID = "300000000000000001"
ROLE = "400000000000000001"
TIMESTAMP = "2026-10-19T13:13:11Z"
DELETE = "<DELETE-FIELD>"


class Snapshot:
    def __init__(self, reference):
        self.reference = reference
        self.id = reference.id
        self.exists = self.id in reference.db.data[reference.collection]
        self._data = copy.deepcopy(reference.db.data[reference.collection].get(self.id))

    def to_dict(self):
        return copy.deepcopy(self._data)


class Reference:
    def __init__(self, db, collection, key):
        self.db, self.collection, self.id = db, collection, key

    def get(self):
        return Snapshot(self)

    def create(self, data):
        if self.id in self.db.data[self.collection]:
            raise ValueError("Already exists")
        self.db.data[self.collection][self.id] = copy.deepcopy(data)
        self.db.writes.append((self.collection, self.id, "create"))

    def update(self, data):
        target = self.db.data[self.collection][self.id]
        for key, value in data.items():
            if value == DELETE:
                target.pop(key, None)
            else:
                target[key] = copy.deepcopy(value)
        self.db.writes.append((self.collection, self.id, "update"))


class Query:
    def __init__(self, db, collection, conditions=()):
        self.db, self.name, self.conditions = db, collection, conditions

    def where(self, field, operator, value):
        assert operator == "=="
        return Query(self.db, self.name, self.conditions + ((field, value),))

    def order_by(self, *args, **kwargs):
        return self

    def stream(self):
        for key, value in list(self.db.data[self.name].items()):
            if all(value.get(field) == expected for field, expected in self.conditions):
                yield Reference(self.db, self.name, key).get()

    def document(self, key):
        return Reference(self.db, self.name, key)


class FakeDatabase:
    def __init__(self, players=None):
        self.data = {"players": copy.deepcopy(players or {}),
                     "punishments": {"p1": {"nickname": "PlayerOne", "penalty": "경고"}},
                     "movements": {"m1": {"type": "TRADE", "playerName": "PlayerOne"}}}
        self.writes = []

    def collection(self, name):
        self.data.setdefault(name, {})
        return Query(self, name)


class FakeDiscordError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


class Member:
    def __init__(self, guild, user_id=USER, display_name="PlayerOne", roles=(), bot=False):
        self.guild, self.id = guild, int(user_id)
        self.name, self.display_name = "discord_user", display_name
        self.roles = [SimpleNamespace(id=int(role)) for role in roles]
        self.bot = bot

    def __str__(self):
        return self.name


class Guild:
    def __init__(self, guild_id=GUILD):
        self.id, self.unavailable = int(guild_id), False
        self.records = {BOT_ID: Member(self, BOT_ID, "bot", bot=True)}
        self.list_override = None
        self.fetch_errors = {}
        self.list_error = None
        self.before_fetch = None
        self.fetch_calls = []

    def add(self, **kwargs):
        member = Member(self, **kwargs)
        self.records[str(member.id)] = member
        return member

    async def fetch_members(self, limit=None):
        for member in self.list_override if self.list_override is not None else list(self.records.values()):
            yield member
        if self.list_error:
            raise self.list_error

    async def fetch_member(self, user_id):
        user_id = str(user_id)
        self.fetch_calls.append(user_id)
        if self.before_fetch:
            await self.before_fetch(user_id)
        if user_id in self.fetch_errors:
            raise self.fetch_errors[user_id]
        if user_id not in self.records:
            raise FakeDiscordError(404, 10007)
        return self.records[user_id]


def player(**extra):
    return {"name": "PlayerOne", "team": "PBG", "position": "SS", "number": "7",
            "transfer": "기존 이적 기록", "forcedReleaseOriginalTeam": "PBG",
            "discordId": USER, "discordGuildId": GUILD, "rosterStatus": "active", **extra}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.database = FakeDatabase({"original": player()})
        self.store = FirestoreRosterStore(self.database, TIMESTAMP, DELETE)
        self.identity = Identity(GUILD, USER, "discord_user", "PlayerOne")

    def test_leave_hides_roster_and_preserves_history_team_and_details(self):
        before = copy.deepcopy(self.database.data)
        self.assertEqual(self.store.deactivate(GUILD, USER, "member_remove"), 1)
        updated = self.database.data["players"]["original"]
        self.assertFalse(is_active_player(updated))
        for field in ("team", "position", "number", "transfer", "forcedReleaseOriginalTeam"):
            self.assertEqual(updated[field], before["players"]["original"][field])
        self.assertEqual(self.database.data["punishments"], before["punishments"])
        self.assertEqual(self.database.data["movements"], before["movements"])

    def test_repeated_leave_is_idempotent(self):
        self.store.deactivate(GUILD, USER, "member_remove")
        self.assertEqual(self.store.deactivate(GUILD, USER, "reconcile_not_found"), 0)
        self.assertEqual(len(self.database.writes), 1)
        self.assertEqual(self.database.data["players"]["original"]["discordLeftReason"], "member_remove")

    def test_wrong_guild_never_changes_scoped_player(self):
        self.assertEqual(self.store.deactivate(OTHER_GUILD, USER, "member_remove"), 0)
        self.assertEqual(self.database.writes, [])

    def test_unlinked_same_name_is_never_removed(self):
        self.database.data["players"]["unlinked"] = {"name": "PlayerOne", "team": "OWL"}
        self.store.deactivate(GUILD, USER, "member_remove")
        self.assertTrue(is_active_player(self.database.data["players"]["unlinked"]))

    def test_legacy_discord_id_and_numeric_id_are_scoped_without_name_guessing(self):
        for discord_id in (USER, int(USER)):
            with self.subTest(discord_id=discord_id):
                self.database.data["players"] = {"legacy": {"name": "DifferentName", "discordId": discord_id}}
                self.store.deactivate(GUILD, USER, "member_remove")
                self.assertEqual(self.database.data["players"]["legacy"]["discordGuildId"], GUILD)
                self.assertFalse(is_active_player(self.database.data["players"]["legacy"]))

    def test_foreign_scoped_id_prevents_ambiguous_legacy_adoption(self):
        self.database.data["players"] = {"foreign": player(discordGuildId=OTHER_GUILD),
                                         "legacy": {"name": "PlayerOne", "discordId": USER}}
        self.assertEqual(self.store.deactivate(GUILD, USER, "member_remove"), 0)
        self.assertEqual(self.store.linked_user_ids(GUILD), set())

    def test_left_player_requires_fresh_reaction_not_bootstrap_or_nickname(self):
        self.store.deactivate(GUILD, USER, "member_remove")
        self.assertEqual(self.store.register(self.identity, "무소속", "reaction", "startup", False)[0], "awaiting_registration")
        self.store.refresh_identity(Identity(GUILD, USER, "discord_user", "NewNickname"))
        self.assertFalse(is_active_player(self.database.data["players"]["original"]))
        self.assertEqual(self.store.register(self.identity, "무소속", "reaction", "event", True)[0], "restored")
        data = self.database.data["players"]["original"]
        self.assertTrue(is_active_player(data))
        self.assertNotIn("discordLeftAt", data)
        self.assertNotIn("discordLeftReason", data)
        self.assertEqual(data["team"], "PBG")
        self.assertEqual(len(self.database.data["players"]), 1)

    def test_new_registration_duplicate_delivery_creates_one_document(self):
        self.database.data["players"] = {}
        for _ in range(3):
            self.store.register(self.identity, "무소속", "reaction", "event")
        self.assertEqual(len(self.database.data["players"]), 1)
        self.assertIn(f"discord-{GUILD}-{USER}", self.database.data["players"])

    def test_unique_legacy_same_team_can_link_without_changing_team(self):
        self.database.data["players"] = {"manual": {"name": "PlayerOne", "team": "PBG", "position": "P"}}
        self.assertEqual(self.store.register(self.identity, "PBG", "role", "event")[0], "linked")
        self.assertEqual(self.database.data["players"]["manual"]["position"], "P")

    def test_same_name_different_team_does_not_steal_or_create_duplicate(self):
        self.database.data["players"] = {"manual": {"name": "PlayerOne", "team": "OWL"}}
        self.assertEqual(self.store.register(self.identity, "PBG", "reaction", "event")[0], "conflict")
        self.assertEqual(self.database.writes, [])

    def test_same_name_other_discord_account_does_not_steal(self):
        self.database.data["players"] = {"other": player(discordId=OTHER_USER)}
        self.assertEqual(self.store.register(self.identity, "PBG", "reaction", "event")[0], "conflict")
        self.assertEqual(self.database.data["players"]["other"]["discordId"], OTHER_USER)
        self.assertEqual(self.database.writes, [])

    def test_duplicate_linked_rows_all_hide_but_ambiguous_restore_is_blocked(self):
        self.database.data["players"]["duplicate"] = player(name="OldNickname")
        self.assertEqual(self.store.deactivate(GUILD, USER, "member_remove"), 2)
        self.assertEqual(self.store.register(self.identity, "무소속", "reaction", "event")[0], "conflict")
        self.assertTrue(all(not is_active_player(row) for row in self.database.data["players"].values()))

    def test_display_rename_conflict_preserves_existing_name(self):
        self.database.data["players"]["other"] = player(name="OtherName", discordId=OTHER_USER)
        result = self.store.refresh_identity(Identity(GUILD, USER, "discord_user", "OtherName"))
        self.assertTrue(result["conflict"])
        self.assertEqual(self.database.data["players"]["original"]["name"], "PlayerOne")

    def test_corrupt_deterministic_document_does_not_recurse_or_overwrite(self):
        key = f"discord-{GUILD}-{USER}"
        self.database.data["players"] = {key: player(discordGuildId=OTHER_GUILD, name="Different")}
        self.assertEqual(self.store.register(self.identity, "무소속", "reaction", "event")[0], "conflict")
        self.assertEqual(self.database.writes, [])

    def test_manual_link_preserves_name_and_departed_status(self):
        self.database.data["players"] = {"legacy": {"name": "MinecraftNickname", "team": "OWL", "rosterStatus": "left_guild"}}
        name = self.store.link_manual(self.identity, "MinecraftNickname", "OWL", "admin")
        self.assertEqual(name, "MinecraftNickname")
        self.assertFalse(is_active_player(self.database.data["players"]["legacy"]))
        self.assertEqual(self.database.data["players"]["legacy"]["name"], "MinecraftNickname")

    def test_unknown_and_missing_statuses_remain_active(self):
        for value in ({}, {"rosterStatus": "active"}, {"rosterStatus": "custom"}, {"rosterStatus": None}):
            self.assertTrue(is_active_player(value))


class SyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Fake Firestore has no blocking I/O. Run its adapter inline so these tests
        # do not depend on a host thread pool; async REST/event races remain real.
        async def inline_worker(function, *args, **kwargs):
            return function(*args, **kwargs)
        self.worker_patch = patch("roster_sync.asyncio.to_thread", inline_worker)
        self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)
        self.database = FakeDatabase({"original": player()})
        self.store = FirestoreRosterStore(self.database, TIMESTAMP, DELETE)
        self.guild = Guild()
        self.bot = SimpleNamespace(user=SimpleNamespace(id=int(BOT_ID)))
        self.service = RosterSynchronizer(self.bot, self.store, GUILD, logger=lambda *args: None)

    async def test_raw_departure_needs_no_member_cache(self):
        self.assertNotIn(USER, self.guild.records)
        self.assertEqual(await self.service.member_removed(GUILD, USER), 1)
        self.assertFalse(is_active_player(self.database.data["players"]["original"]))

    async def test_wrong_guild_and_bot_departures_are_ignored(self):
        await self.service.member_removed(OTHER_GUILD, USER)
        await self.service.member_removed(GUILD, BOT_ID)
        self.assertEqual(self.database.writes, [])

    async def test_restart_catches_missed_leave_via_confirmed_unknown_member(self):
        result = await self.service.reconcile(self.guild)
        self.assertEqual(result["removed"], 1)
        self.assertIn(USER, self.guild.fetch_calls)
        self.assertFalse(is_active_player(self.database.data["players"]["original"]))

    async def test_partial_fetch_failure_causes_no_database_writes(self):
        self.guild.list_error = FakeDiscordError(403, 50013)
        result = await self.service.reconcile(self.guild)
        self.assertTrue(result["skipped"])
        self.assertEqual(self.database.writes, [])

    async def test_empty_or_wrong_guild_listing_causes_no_database_writes(self):
        for listing in ([], [Member(Guild(OTHER_GUILD), BOT_ID, bot=True)]):
            self.guild.list_override = listing
            result = await self.service.reconcile(self.guild)
            self.assertTrue(result["skipped"])
        self.assertEqual(self.database.writes, [])

    async def test_incomplete_successful_listing_does_not_hide_present_member(self):
        self.guild.add()
        self.guild.list_override = [self.guild.records[BOT_ID]]
        result = await self.service.reconcile(self.guild)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(is_active_player(self.database.data["players"]["original"]))

    async def test_forbidden_unknown_guild_and_network_errors_do_not_mean_departure(self):
        for error in (FakeDiscordError(403, 50013), FakeDiscordError(404, 10004), FakeDiscordError(429, 0), OSError("offline")):
            with self.subTest(error=type(error).__name__):
                self.guild.fetch_errors[USER] = error
                result = await self.service.reconcile(self.guild)
                self.assertEqual(result["removed"], 0)
                self.assertEqual(result["failures"], 1)
        self.assertEqual(self.database.writes, [])

    async def test_join_alone_and_restart_do_not_restore_left_player(self):
        self.store.deactivate(GUILD, USER, "member_remove")
        member = self.guild.add()
        await self.service.member_changed(member)
        await self.service.reconcile(self.guild)
        self.assertFalse(is_active_player(self.database.data["players"]["original"]))

    async def test_fresh_reaction_after_rejoin_restores_same_record(self):
        self.store.deactivate(GUILD, USER, "member_remove")
        member = self.guild.add()
        result = await self.service.register_member(member, "reaction")
        self.assertEqual(result[0], "restored")
        self.assertEqual(len(self.database.data["players"]), 1)
        self.assertEqual(self.database.data["players"]["original"]["team"], "PBG")

    async def test_registration_role_is_optional_and_verified_again(self):
        member = self.guild.add(roles=[ROLE])
        self.database.data["players"] = {}
        await self.service.member_changed(member)
        self.assertEqual(self.database.writes, [])
        self.service.role_ids = {ROLE}
        await self.service.member_changed(member)
        self.assertEqual(len(self.database.data["players"]), 1)
        cached_with_role = member
        self.guild.records[USER] = Member(self.guild)
        self.database.data["players"] = {}
        self.database.writes = []
        await self.service.register_member(cached_with_role, "role")
        self.assertEqual(self.database.writes, [])

    async def test_current_role_at_restart_can_restore_registration(self):
        self.store.deactivate(GUILD, USER, "member_remove")
        self.service.role_ids = {ROLE}
        self.guild.add(roles=[ROLE])
        result = await self.service.reconcile(self.guild)
        self.assertEqual(result["registered"], 1)
        self.assertTrue(is_active_player(self.database.data["players"]["original"]))

    async def test_role_removal_does_not_erase_player_or_team(self):
        member = self.guild.add()
        self.service.role_ids = {ROLE}
        await self.service.member_changed(member)
        self.assertTrue(is_active_player(self.database.data["players"]["original"]))
        self.assertEqual(self.database.data["players"]["original"]["team"], "PBG")

    async def test_stale_reaction_member_is_not_reactivated_after_departure(self):
        cached = Member(self.guild)
        result = await self.service.register_member(cached, "reaction")
        self.assertEqual(result[0], "absent")
        self.assertFalse(is_active_player(self.database.data["players"]["original"]))

    async def test_new_event_invalidates_late_missing_response(self):
        async def later_event(user_id):
            self.service.note_event(user_id)
        self.guild.before_fetch = later_event
        result = await self.service.register_member(Member(self.guild), "reaction")
        self.assertEqual(result[0], "absent")
        self.assertEqual(self.database.writes, [])

    async def test_reconcile_does_not_override_newer_event_during_rest_fetch(self):
        async def later_event(user_id):
            self.service.note_event(user_id)
        self.guild.before_fetch = later_event
        result = await self.service.reconcile(self.guild)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.database.writes, [])

    async def test_departure_during_reaction_fetch_wins(self):
        member = self.guild.add()
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_fetch(user_id):
            entered.set()
            await release.wait()
        self.guild.before_fetch = delayed_fetch
        reaction_task = asyncio.create_task(self.service.register_member(member, "reaction"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        departure_task = asyncio.create_task(self.service.member_removed(GUILD, USER))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(reaction_task, departure_task), timeout=2)
        self.assertFalse(is_active_player(self.database.data["players"]["original"]))

    async def test_invalid_registration_team_does_not_disable_departure_handling(self):
        self.service.registration_enabled = False
        self.service.role_ids = {ROLE}
        member = self.guild.add(roles=[ROLE])
        self.assertEqual((await self.service.register_member(member))[0], "ignored")
        await self.service.member_removed(GUILD, USER)
        self.assertFalse(is_active_player(self.database.data["players"]["original"]))


if __name__ == "__main__":
    unittest.main()
