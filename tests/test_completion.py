import ast
import inspect
import io
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from pyquake import client
from pyquake.console import ConsoleSession, SERVER_COMMANDS
from test_client import FakeConnection


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pyquake-completion-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.cvars = client.CvarSystem()
        self.cvars._config_file = str(self.directory / "config.cfg")
        self.cvars.register(client.Cvar("name", "old name", client.CvarFlags.USERINFO,
                                       cvar_type=client.CvarType.STRING))
        for name in ("topcolor", "bottomcolor"):
            self.cvars.register(client.Cvar(name, "0", cvar_type=client.CvarType.INT))
        for name, value in (("cvar_system", self.cvars), ("CURRENT_CLIENT", None),
                            ("_CONSOLE_SESSION", None), ("CON", mock.Mock()),
                            ("_IDLE_RECONNECT", client._IdleReconnectManager()),
                            ("_autosave_config", mock.Mock())):
            patch = mock.patch.object(client, name, value, create=True)
            patch.start()
            self.addCleanup(patch.stop)
        self.session = client._get_console_session()
        self.complete = self.session._complete_input

    async def asyncTearDown(self):
        await self.session.close()

    async def test_every_handled_command_and_live_cvar_is_discoverable(self):
        # Audit the handler itself, so newly added commands cannot silently be
        # omitted from the separately maintained command-name inventory.
        tree = ast.parse(inspect.getsource(ConsoleSession))
        handled = set(SERVER_COMMANDS)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "name":
                for comparator in node.comparators:
                    try:
                        value = ast.literal_eval(comparator)
                    except (ValueError, TypeError):
                        continue
                    handled.update([value] if isinstance(value, str) else value)
        self.cvars.register_command("custom_command", lambda args: None)
        self.cvars.register_alias(self.cvars.find("cl_chatmode"), "legacy_chat")
        self.cvars.command_aliases["custom_alias"] = "ready"
        names = handled | set(self.cvars._commands) | {"custom_alias", "legacy_chat"}
        names.update(variable.name for variable in self.cvars._cvars.values())
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(self.complete("", name)[0].casefold(), name.casefold())
        self.assertEqual(self.complete("", ""), [])

    async def test_substring_matching_rank_and_natural_order(self):
        self.cvars.command_aliases.update({name: "ready" for name in
                                          ("ztestcmd", "testcmd10", "testcmd2", "testcmd")})
        self.assertEqual(self.complete("", "TESTCMD"),
                         ["testcmd", "testcmd2", "testcmd10", "ztestcmd"])
        editor = self.session._terminal_input
        editor._size = lambda: (80, 24)
        editor.text, editor.cursor = "estcmd", 6
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            editor._complete()
        self.assertEqual(editor.text, "estcmd")

    async def test_cvar_names_values_and_argument_boundaries(self):
        self.cvars.register(client.Cvar("unarchived", "1"))
        for command in ("set", "seta"):
            self.assertIn("unarchived", self.complete(command + " ", ""))
            self.assertNotIn("cl_chatmode", self.complete(command + " ", ""))
        for command in ("reset", "toggle", "unset"):
            self.assertIn("cl_chatmode", self.complete(command + " ", ""))
        expected = {"cl_chatmode": ["0", "1", "2", "3"],
                    "cl_autodemo": ["0", "1", "2", "3", "4"],
                    "cl_demo_format": ["dem", "dz"],
                    "cl_demo_minframes": ["0", "72", "720", "2160", "4320", "-72", "-720", "-2160", "-4320"],
                    "topcolor": list(map(str, range(14))), "bottomcolor": list(map(str, range(14)))}
        for name, values in expected.items():
            with self.subTest(name=name):
                self.assertEqual(set(self.complete(name + " ", "")), set(values))
                self.assertEqual(set(self.complete("set " + name + " ", "")), set(values))
                self.assertEqual(self.complete(name + " 0 ", ""), [])
        self.assertEqual(self.complete("cl_download_maxsize ", ""), [])
        self.assertEqual(self.complete("cl_recordingdemo ", ""), [])
        self.assertEqual(self.complete("color ", ""), list(map(str, range(14))))
        self.assertEqual(self.complete("color 4 ", "1"), ["1", "10", "11", "12", "13"])
        self.assertEqual(self.complete("color 4 13 ", ""), [])

    async def test_aliases_nested_commands_and_context_parsing(self):
        self.cvars.command_aliases["warmup"] = "ready"
        for command in ("alias", "unalias", "aliasedit"):
            self.assertEqual(self.complete(command + " ", "warm"), ["warmup"])
        self.assertEqual(self.complete("alias warmup ", ""), [])
        self.assertIn("status", self.complete("cmd ", "sta"))
        self.assertEqual(self.complete("cmd color ", "13"), ["13"])
        self.assertEqual(self.complete("rcon cl_demo_format ", "d"), ["dem", "dz"])
        for context in ('echo "semi; colon"; cl_chatmode ', "echo test\ncl_chatmode ",
                        "// ignore; bad command\ncl_chatmode ", "/cl_chatmode "):
            self.assertEqual(self.complete(context, ""), ["0", "1", "2", "3"])
        self.assertEqual(self.complete("echo test // comment; ", "sta"), [])
        self.assertEqual(self.complete("", "/status"), ["/status"])

    async def test_config_paths_and_quoted_directories_work_in_the_editor(self):
        path = self.directory / "space dir"
        path.mkdir()
        (path / "config one.cfg").write_text("echo test")
        (path / "wrong.txt").write_text("wrong")
        for command in ("exec", "execconfig", "writeconfig"):
            self.assertEqual(self.complete(command + " ", f'"{path / "config"}'),
                             [f'"{path / "config one.cfg"}"'])
        editor = self.session._terminal_input
        editor.text = f'exec "{self.directory}/space'
        editor.cursor = len(editor.text)
        editor._complete()
        self.assertEqual(editor.text, f'exec "{path}/')
        editor.text += "config o"
        editor.cursor = len(editor.text)
        editor._complete()
        self.assertEqual(shlex.split(editor.text), ["exec", str(path / "config one.cfg")])
        self.assertNotIn(f'"{path / "wrong.txt"}"', self.complete("exec ", f'"{path}/'))

    async def test_player_and_download_completions_follow_live_client_state(self):
        active = client.AsyncClient(FakeConnection())
        client.CURRENT_CLIENT = active
        active._score_state = {0: {"active": True, "name_full": "wood man"},
                               1: {"active": False, "name": "gone"}}
        active._players = {1: SimpleNamespace(name="gone")}
        active.models = ["maps/dm3.bsp", "*1", "progs/player.mdl"]
        active.sounds = ["misc/talk.wav"]
        for command in ("tell", "dm", "pm", "identify"):
            self.assertEqual(self.complete(command + " ", "wood"), ['"wood man"'])
            self.assertEqual(self.complete(command + " ", "#"), ["#1"])
            self.assertEqual(self.complete(command + ' "wood man" ', "sta"), [])
        self.assertEqual(self.complete("download ", "maps"), ["maps/dm3.bsp"])
        self.assertEqual(self.complete("download ", "talk"), ["sound/misc/talk.wav"])
        self.assertEqual(self.complete("download maps/dm3.bsp ", ""), ["auto", "server", "web"])
        with mock.patch("pyquake.completion.time.strftime", return_value="_test"):
            self.assertEqual(self.complete("record ", ""), ["dm3_test"])
        active.disconnected = True
        self.assertEqual(self.complete("tell ", "wood"), [])
        self.assertEqual(self.complete("record ", ""), [])

    async def test_own_name_history_survives_restart(self):
        self.cvars.set("name", "new name")
        self.assertEqual(set(self.complete("name ", "")), {'"old name"', '"new name"'})
        await self.session.close()
        client._CONSOLE_SESSION = None
        self.session = client._get_console_session()
        self.complete = self.session._complete_input
        self.assertIn('"old name"', self.complete("name ", "old"))

    async def test_registered_completion_callbacks_and_readline_use_same_path(self):
        self.cvars.register_command("custom", lambda args: None,
                                    completion=lambda args, partial: ["alpha", "beta"] if not args else [])
        self.cvars.register(client.Cvar("custom_mode", "alpha", completion=lambda variable, partial: ["alpha", "beta"]))
        for context in ("custom ", "custom_mode ", "set custom_mode "):
            self.assertEqual(self.complete(context, "b"), ["beta"])
            readline = mock.Mock(get_line_buffer=lambda: context + "b ignored",
                                 get_begidx=lambda: len(context), get_endidx=lambda: len(context) + 1)
            with mock.patch.object(client, "_readline", readline):
                self.assertEqual(client._tab_completer("b", 0), "beta")
                self.assertIsNone(client._tab_completer("b", 1))
        self.cvars.unregister_command("custom")
        self.assertEqual(self.complete("custom ", "b"), [])
