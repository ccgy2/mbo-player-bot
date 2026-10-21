"""Exercise the shipped bot's handlers and query helpers against offline doubles.

AST extraction deliberately avoids importing bot.py, loading .env, initializing
Firebase, registering slash commands, or connecting a Discord client.
"""

import ast
import asyncio
import copy
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_roster_sync import (BOT_ID, DELETE, GUILD, OTHER_GUILD, ROLE, TIMESTAMP, USER,
                              FakeDatabase, Guild, Member, player)
from roster_sync import FirestoreRosterStore, RosterSynchronizer, is_active_player, snowflake


def load_functions(names, globals_):
    source = Path(__file__).resolve().parents[1] / "bot.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            selected.append(node)
    if len(selected) != len(names):
        raise AssertionError("A required shipped bot helper or handler is missing")
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, str(source), "exec"), globals_)
    return globals_


class QueryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.database = FakeDatabase({"left": player(rosterStatus="left_guild"),
                                      "active": player(name="ActivePlayer", discordId="200000000000000003"),
                                      "manual": {"name": "ManualPlayer", "team": "PBG"}})
        self.functions = load_functions({"find_player_sync", "fetch_team_roster_sync", "fetch_player_choices_sync",
                                         "commit_player_registration_sync", "find_fa_sign_player_sync",
                                         "commit_trade_sync", "fetch_player_transfer_info_sync"},
                                        {"db": self.database, "is_active_player": is_active_player,
                                         "normalize": lambda value: str(value or "").strip(),
                                         "firestore": SimpleNamespace(Query=SimpleNamespace(DESCENDING="DESC")),
                                         "player_aliases_from_movements": lambda name, moves: {name},
                                         "movement_includes_alias": lambda move, aliases: move.get("playerName") in aliases})

    def test_active_roster_and_autocomplete_hide_departed_preserve_manual_players(self):
        self.assertIsNone(self.functions["find_player_sync"]("PlayerOne"))
        self.assertEqual({row["name"] for row in self.functions["fetch_team_roster_sync"]("PBG")},
                         {"ActivePlayer", "ManualPlayer"})
        self.assertEqual({name for name, _ in self.functions["fetch_player_choices_sync"]()},
                         {"ActivePlayer", "ManualPlayer"})

    def test_history_query_still_returns_departed_player_and_movement(self):
        record, movements = self.functions["fetch_player_transfer_info_sync"]("PlayerOne")
        self.assertEqual(record["id"], "left")
        self.assertEqual(movements[0]["type"], "TRADE")

    def test_history_lookup_prefers_active_homonym_after_team_filter(self):
        self.database.data["players"]["current_same_name"] = player(discordId="200000000000000004")
        record = self.functions["find_player_sync"]("PlayerOne", "PBG", include_inactive=True)
        self.assertEqual(record["id"], "current_same_name")

    def test_manual_registration_cannot_duplicate_an_archived_same_player(self):
        with self.assertRaises(ValueError):
            self.functions["commit_player_registration_sync"]("PlayerOne", "PBG", "admin")
        self.assertEqual(self.database.writes, [])

    def test_fa_and_trade_cannot_use_departed_player(self):
        with self.assertRaises(ValueError):
            self.functions["find_fa_sign_player_sync"]("PlayerOne")
        with self.assertRaises(ValueError):
            self.functions["commit_trade_sync"]("PBG", ["PlayerOne"], "OWL", ["ActivePlayer"], "2026-10-19", "admin")
        self.assertEqual(self.database.writes, [])


class CallbackIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        async def inline_worker(function, *args, **kwargs):
            return function(*args, **kwargs)
        self.worker_patch = patch("roster_sync.asyncio.to_thread", inline_worker)
        self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)
        self.database = FakeDatabase({"existing": player()})
        self.store = FirestoreRosterStore(self.database, TIMESTAMP, DELETE)
        self.guild = Guild()
        self.bot = SimpleNamespace(user=SimpleNamespace(id=int(BOT_ID)), guilds=[self.guild],
                                   get_guild=lambda guild_id: self.guild if str(guild_id) == GUILD else None)
        self.service = RosterSynchronizer(self.bot, self.store, GUILD)
        async def configured_service():
            return self.service
        self.functions = load_functions({"on_raw_member_remove", "on_member_join", "on_member_update", "on_raw_reaction_add"},
                                        {"configured_roster_service": configured_service,
                                         "bot": self.bot, "normalize": lambda value: str(value or "").strip(),
                                         "parse_channel_id": lambda value: str(value or ""),
                                         "REACTION_ROSTER_MESSAGE_ID": "500000000000000001",
                                         "REACTION_ROSTER_CHANNEL_ID": "600000000000000001",
                                         "print": lambda *args, **kwargs: None})

    async def test_full_uncached_leave_join_then_reaction_flow(self):
        await self.functions["on_raw_member_remove"](SimpleNamespace(guild_id=int(GUILD), user=SimpleNamespace(id=int(USER), bot=False)))
        self.assertFalse(is_active_player(self.database.data["players"]["existing"]))
        member = self.guild.add()
        await self.functions["on_member_join"](member)
        self.assertFalse(is_active_player(self.database.data["players"]["existing"]))
        await self.functions["on_raw_reaction_add"](SimpleNamespace(guild_id=int(GUILD), user_id=int(USER), member=member,
                                                                    message_id=500000000000000001, channel_id=600000000000000001))
        self.assertTrue(is_active_player(self.database.data["players"]["existing"]))
        self.assertEqual(len(self.database.data["players"]), 1)

    async def test_role_change_handler_registers_only_configured_role(self):
        self.database.data["players"] = {}
        before = self.guild.add()
        after = self.guild.add(roles=[ROLE])
        await self.functions["on_member_update"](before, after)
        self.assertEqual(self.database.writes, [])
        self.service.role_ids = {ROLE}
        await self.functions["on_member_update"](before, after)
        self.assertEqual(len(self.database.data["players"]), 1)

    async def test_wrong_message_channel_and_guild_do_not_register(self):
        self.database.data["players"] = {}
        member = self.guild.add()
        base = {"guild_id": int(GUILD), "user_id": int(USER), "member": member,
                "message_id": 500000000000000001, "channel_id": 600000000000000001}
        for extra in ({"message_id": 500000000000000002}, {"channel_id": 600000000000000002}, {"guild_id": int(OTHER_GUILD)}):
            await self.functions["on_raw_reaction_add"](SimpleNamespace(**{**base, **extra}))
        self.assertEqual(self.database.writes, [])


class GuildConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = Guild()
        self.other_guild = Guild(OTHER_GUILD)
        self.database = FakeDatabase()
        async def get_config():
            return {}
        async def resolve_channel(channel_id):
            return SimpleNamespace(guild=self.other_guild)
        async def inline_worker(function, *args, **kwargs):
            return function(*args, **kwargs)
        self.bot = SimpleNamespace(guilds=[self.guild], user=SimpleNamespace(id=int(BOT_ID)),
                                   get_guild=lambda guild_id: {int(GUILD): self.guild, int(OTHER_GUILD): self.other_guild}.get(guild_id))
        self.functions = load_functions({"configured_roster_service"},
                                        {"ROSTER_SYNC_SERVICE": None, "ROSTER_CONFIGURATION_LOCK": asyncio.Lock(),
                                         "ROSTER_GUILD_ID": "", "ROSTER_ROLE_IDS": "", "REACTION_ROSTER_CHANNEL_ID": "",
                                         "get_bot_config": get_config, "resolve_channel": resolve_channel,
                                         "parse_channel_id": lambda value: str(value or ""), "snowflake": snowflake,
                                         "normalize": lambda value: str(value or "").strip(), "re": re,
                                         "bot": self.bot, "configured_reaction_roster_team": lambda: "무소속",
                                         "run_blocking": inline_worker, "valid_team_sync": lambda team, free: True,
                                         "RosterSynchronizer": RosterSynchronizer,
                                         "roster_store": FirestoreRosterStore(self.database, TIMESTAMP, DELETE),
                                         "print": lambda *args, **kwargs: None})

    async def test_single_guild_legacy_setup_resolves_without_new_required_setting(self):
        service = await self.functions["configured_roster_service"]()
        self.assertEqual(service.guild_id, GUILD)
        self.assertFalse(service.role_ids)

    async def test_multiple_guilds_require_unambiguous_scope(self):
        self.bot.guilds = [self.guild, self.other_guild]
        self.assertIsNone(await self.functions["configured_roster_service"]())
        self.assertEqual(self.database.writes, [])

    async def test_explicit_guild_is_honored(self):
        self.bot.guilds = [self.guild, self.other_guild]
        self.functions["ROSTER_GUILD_ID"] = OTHER_GUILD
        service = await self.functions["configured_roster_service"]()
        self.assertEqual(service.guild_id, OTHER_GUILD)

    async def test_channel_and_explicit_guild_mismatch_is_not_guessed(self):
        self.functions["ROSTER_GUILD_ID"] = GUILD
        self.functions["REACTION_ROSTER_CHANNEL_ID"] = "600000000000000001"
        self.assertIsNone(await self.functions["configured_roster_service"]())
        self.assertEqual(self.database.writes, [])

    async def test_optional_role_setting_does_not_come_from_team_role_config(self):
        self.functions["ROSTER_ROLE_IDS"] = ROLE
        service = await self.functions["configured_roster_service"]()
        self.assertEqual(service.role_ids, {ROLE})


if __name__ == "__main__":
    unittest.main()
