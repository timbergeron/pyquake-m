"""Console commands and connection lifecycle shared by every command source."""

import asyncio
import inspect
import re
import shlex
import time

if __package__:
    from .commands import CommandDispatcher, alias_key, define_alias, quote_arg, split_commands
else:
    from commands import CommandDispatcher, alias_key, define_alias, quote_arg, split_commands


SERVER_COMMANDS = frozenset({
    "status", "players", "kill", "ping", "pings", "impulse", "say", "say_team",
    "ready", "unready", "notready", "observe", "observer", "spectate", "join",
    "team", "vote", "yes", "no", "pause", "identify", "setinfo", "serverinfo",
})


class ConsoleSession:
    def __init__(self, api):
        self.api = api
        self.cvars = api.cvar_system
        self.aliases = self.cvars.command_aliases
        self.last_connect_spec = None
        self.tasks = ()
        self.demo = None
        self.enable_demo = False
        self.connecting = False
        for name, value, kind, description in (
            ("cl_chatmode", "0", api.CvarType.INT, "0=commands, 1=auto chat, 2=chat-first (prefilled space), 3=leading-space chat"),
            ("cfg_save_aliases", "1", api.CvarType.BOOL, "Save command aliases in config.cfg"),
            ("cl_aliasoverlap", "0", api.CvarType.BOOL, "Allow aliases to overlap cvar names"),
        ):
            if self.cvars.find(name) is None:
                self.cvars.register(api.Cvar(name, value, api.CvarFlags.ARCHIVE,
                                             cvar_type=kind, minimum=0,
                                             maximum=3 if name == "cl_chatmode" else 1,
                                             description=description))
        self.dispatcher = CommandDispatcher(
            aliases=self.aliases, handle=self.handle, forward=self.forward,
            is_command=self.is_command, chat_mode=self.chat_mode,
            wait_frame=api._wait_next_frame, output=api.CON.write,
            reserved=lambda name: name in api._RESERVED_CMDS or name in self.cvars._commands,
            max_depth=api.ALIAS_MAX_DEPTH)

    @property
    def client(self):
        return self.api.CURRENT_CLIENT

    def chat_mode(self):
        # QSS-M only converts console input to chat while connected.
        active = self.client
        if active is None or not active._alive or active.disconnected:
            return 0
        return int(self.cvars.get_value("cl_chatmode"))

    def is_command(self, text):
        commands = split_commands(text)
        if not commands:
            return False
        tokens = commands[0].split(None, 1)
        name = tokens[0].lower()
        return (name in self.api._RESERVED_CMDS or name in SERVER_COMMANDS
                or name in self.cvars._commands or self.cvars.find(name) is not None
                or alias_key(self.aliases, name) is not None)

    async def forward(self, text):
        if self.client and self.client._alive and not self.client.disconnected:
            await self.client.send_command(text)
        else:
            self.api.CON.write(f"(offline) {text}")

    async def disconnect(self, *, save=True):
        q = self.api
        if save:
            q._autosave_config(silent=True)
        await q._stop_tasks(*self.tasks)
        self.tasks = ()
        old = self.client
        q._set_current_client(None, reset_idle=not self.connecting)
        if old is not None:
            await old.disconnect()

    async def connect(self, spec, *, spawn_timeout=20):
        if self.connecting:
            return False
        self.connecting = True
        try:
            return await self._connect(spec, spawn_timeout=spawn_timeout)
        finally:
            self.connecting = False

    async def _connect(self, spec, *, spawn_timeout):
        q = self.api
        target = q._parse_hostport_spec(self.last_connect_spec if spec.lower() == "last" else spec)
        if target is None:
            q.CON.write("No previous server recorded." if spec.lower() == "last"
                        else "Usage: connect host[:port] (port must be between 1 and 65535)")
            return False
        host, port = target
        await self.disconnect(save=self.client is not None)
        self.last_connect_spec = f"{host}:{port}"
        q.CON.write(f"Connecting to {host}:{port}...")
        try:
            active = await q.AsyncClient.connect(host, port)
        except Exception as exc:
            q.CON.write(f"Connection failed: {exc}")
            return False
        active.aliases = self.aliases
        active._bind_cvar_subscriptions()
        q._set_current_client(active)
        if self.enable_demo and self.demo is None:
            self.demo = active.record_demo()
        self.tasks = tuple(asyncio.create_task(fn(active)) for fn in (
            q._simple_keep_alive, q._watch_chatinfo, q._kickstart_handshake, q._connection_guard))
        try:
            await asyncio.wait_for(active.wait_until_spawn(), spawn_timeout)
        except asyncio.TimeoutError:
            q.CON.write("Connected (spawn pending).")
        except asyncio.CancelledError:
            if not active.disconnected:
                raise
            q.CON.write("Disconnected before spawn.")
            return False
        return True

    async def handle(self, raw):
        q = self.api
        previous = q.IN_CFG_EXEC
        current = self.dispatcher.current
        q.IN_CFG_EXEC = bool(current and (current.source == "config" or current.files))
        try:
            return await self._handle(raw)
        finally:
            q.IN_CFG_EXEC = previous

    async def _handle(self, raw):
        q = self.api
        tokens = shlex.split(raw)
        if not tokens:
            return True
        name, args = tokens[0].lower(), tokens[1:]
        if name in ("quit", "exit", "q"):
            self.dispatcher.stop()
        elif name in ("help", "cmdlist"):
            q._print_cmdlist()
        elif name == "connect":
            if len(args) != 1:
                q.CON.write("Usage: connect host[:port]|last")
            else:
                startup = self.dispatcher.current and self.dispatcher.current.source == "startup"
                await self.connect(args[0], spawn_timeout=30 if startup else 20)
        elif name == "reconnect":
            await self.connect("last")
        elif name == "disconnect":
            await self.disconnect()
            q.CON.write("Disconnected. Use: connect host[:port] or connect last")
        elif name in ("exec", "execconfig"):
            if len(args) > 1 or (name == "exec" and not args):
                q.CON.write("usage: exec <filename>")
            else:
                filename = args[0] if args else self.cvars._config_file
                fallback = q.DEFAULT_CFG if filename == "default.cfg" else None
                if self.dispatcher.exec_file(filename, fallback=fallback):
                    if self.cvars.get_bool("cmd_warncmd", True):
                        q.CON.write(f"execing {filename}")
        elif name == "writeconfig":
            if len(args) > 1:
                q.CON.write("usage: writeconfig [filename]")
            else:
                filename = args[0] if args else self.cvars._config_file
                if self.cvars.write_variables(filename):
                    q.CON.write(f"wrote {filename}")
        elif name == "cvarlist":
            rows = self.cvars.list_cvars(" ".join(args))
            for mark, key, value in rows:
                q.CON.write(f"{mark} {key} : {value}")
            if not rows:
                q.CON.write("(no cvars)")
        elif name in ("set", "seta", "unset", "toggle"):
            self._cvar_command(name, args)
        elif name in ("alias", "aliaslist", "unalias", "unaliasall"):
            self._alias_command(name, args)
        elif name == "clear":
            q.CON.clear()
        elif name == "condump":
            if len(args) > 1:
                q.CON.write("usage: condump [filename]")
            else:
                q.CON.dump(args[0] if args else "condump.txt")
        elif name == "demo":
            if self.demo is not None:
                q.CON.write("Demo already recording")
            elif self.client is not None and not self.client.disconnected:
                self.demo = self.client.record_demo()
                q.CON.write("Demo recording started")
            else:
                q.CON.write("Not connected.")
        elif name in ("showscores", "scoreboard"):
            if self.client is None or self.client.disconnected:
                q.CON.write("Not connected.")
            else:
                await self.showscores()
        elif name == "cmd":
            command = raw.split(None, 1)[1] if args else ""
            if args and args[0].lower() == "protocols":
                command = "protocols 999 666 15"
            elif args and args[0].lower() == "pext":
                command = "pext 0 0 0 0"
            if command:
                await self.forward(command)
            else:
                q.CON.write("usage: cmd <server command>")
        elif name == "color":
            if not args:
                q.CON.write(f'"color" is "{int(self.cvars.get_value("topcolor"))} {int(self.cvars.get_value("bottomcolor"))}"')
            elif len(args) in (1, 2):
                top = q._clamp_color(int(args[0]))
                bottom = q._clamp_color(int(args[-1]))
                self.cvars.set("topcolor", str(top))
                self.cvars.set("bottomcolor", str(bottom))
            else:
                q.CON.write("usage: color [top [bottom]]")
        elif name in ("tell", "dm", "pm"):
            if len(args) < 2:
                q.CON.write("usage: tell <name|#slot> <message>")
            else:
                who = f"#{int(args[0].lstrip('#'))}" if re.fullmatch(r"#?\d+", args[0]) else args[0]
                await self.forward(f"tell {quote_arg(who)} {' '.join(args[1:])}")
        elif name == "like":
            active = self.client
            if active is None or active.disconnected:
                q.CON.write("Not connected.")
            elif time.monotonic() - active._last_like_at >= 1:
                text = active.last_chat_msg.replace("\r", " ").replace("\n", " ").strip()
                if text and not re.search(r"\blikes\b", text):
                    await self.forward(("say_team " if active.last_chat_is_team else "say ") + "likes " + text)
                else:
                    q.CON.write("nothing to like")
                active._last_like_at = time.monotonic()
        elif name in self.cvars._commands:
            result = self.cvars._commands[name](args)
            if inspect.isawaitable(result):
                await result
        elif await q._handle_local_command(raw):
            pass
        elif self.cvars.command(tokens):
            pass
        else:
            return False
        return True

    def _cvar_command(self, command, args):
        q = self.api
        if not args:
            q.CON.write(f"usage: {command} <variable> [value]")
            return
        name, values = args[0], args[1:]
        cv = self.cvars.find(name)
        if command in ("set", "seta"):
            if not values and cv is not None:
                self.cvars.command([name])
                return
            flags = q.CvarFlags.USERDEFINED
            if command == "seta":
                flags |= q.CvarFlags.ARCHIVE | q.CvarFlags.SETA
            if cv is None:
                if name.lower() in q._RESERVED_CMDS:
                    q.CON.write(f"{name} is a command")
                    return
                cv = self.cvars.create(name, " ".join(values), flags=flags, cvar_type=q.CvarType.STRING)
            if cv is None:
                q.CON.write(f"couldn't create {name}")
                return
            cv.flags |= flags & (q.CvarFlags.ARCHIVE | q.CvarFlags.SETA)
            self.cvars.set(cv.name, " ".join(values))
        elif command == "unset":
            self.cvars.reset(name)
        else:
            if cv is None:
                cv = self.cvars.create(name, "0", cvar_type=q.CvarType.BOOL)
            if cv is None:
                q.CON.write(f"couldn't create {name}")
            elif values:
                try:
                    index = values.index(cv.string)
                except ValueError:
                    index = -1
                self.cvars.set(name, values[(index + 1) % len(values)])
            else:
                try:
                    enabled = float(cv.string) != 0
                except ValueError:
                    enabled = cv.string.strip().lower() in ("true", "on", "yes")
                self.cvars.set(name, "0" if enabled else "1")

    def _alias_command(self, command, args):
        q = self.api
        if command == "unaliasall":
            self.aliases.clear()
        elif command == "unalias":
            if len(args) != 1:
                q.CON.write("usage: unalias <name>")
            else:
                key = alias_key(self.aliases, args[0]) or alias_key(self.aliases, args[0] + "_a")
                if key is None:
                    q.CON.write(f"No alias named {args[0]}")
                else:
                    del self.aliases[key]
        elif command == "aliaslist" or len(args) < 2:
            query = args[0].casefold() if args else ""
            for name, body in sorted(self.aliases.items(), key=lambda pair: pair[0].casefold()):
                if not query or query in name.casefold() or query in body.casefold():
                    q.CON.write(f"   {name}: {body.rstrip()}")
            if not self.aliases:
                q.CON.write("no alias commands found")
        else:
            name, body = args[0], " ".join(args[1:])
            key = define_alias(
                self.aliases, name, body,
                reserved=lambda key: key in q._RESERVED_CMDS or key in self.cvars._commands,
                is_cvar=lambda key: self.cvars.find(key) is not None,
                allow_overlap=self.cvars.get_bool("cl_aliasoverlap"))
            if key.casefold() != name.casefold():
                q.CON.write(f"Alias renamed to {key} due to command/cvar conflict")

    def read_line(self, prompt):
        q = self.api
        prefill = (int(self.cvars.get_value("cl_chatmode")) in (2, 3)
                   and q._readline is not None and not q._EDIT_PREFILL)
        if prefill:
            q._readline.set_startup_hook(lambda: q._readline.insert_text(" "))
        try:
            return input(prompt)
        finally:
            if prefill:
                q._readline.set_startup_hook(None)
            q._EDIT_PREFILL = False

    async def close(self):
        q = self.api
        await self.dispatcher.close()
        await self.disconnect()
        await q._stop_tasks(q._IDLE_RECONNECT._task)
        if self.demo is not None:
            self.demo.stop_recording()
            with open("pyquake.dem", "wb") as output:
                self.demo.dump(output)
            q.CON.write("Demo saved to pyquake.dem")

    async def showscores(self):
        q = self.api
        active = self.client
        # Build rows keyed by canonical name to avoid duplicates (case/format diffs)
        rows_by_key: dict[str, tuple[int,int,str,str,str]] = {}
        # Ensure we have recent ping data
        if not any(isinstance(r.get("ping"), int) for r in active._score_state.values()):
            try:
                await active.refresh_pings_via_ping(timeout=1.0)
            except Exception:
                pass

        # 1) Start with active slots that have a resolved ping
        for idx in sorted(active._score_state.keys()):
            r = active._score_state[idx]
            if not r.get("active"):
                continue
            nm = r.get("name")
            pg = r.get("ping")
            if not (nm and isinstance(pg, int)):
                continue
            fr = int(r.get("frags", 0))
            st = q._status_from_names(nm, r.get("name_full", ""))
            q._merge_score_row(rows_by_key, fr, int(pg), st, nm, q._name_key(nm))

        # 2) Merge in any pings we learned from the silent sweep for names
        #    that aren't present in _score_state yet (covers "us" edge case).
        for nm, pg in active._latest_ping_by_name.items():
            fr = 0
            st = ""
            nm_k = q._name_key(nm)
            # if we already know frags/status via a slot with same canonical name, use that
            for r in active._score_state.values():
                if r.get("active") and q._name_key(r.get("name","")) == nm_k:
                    fr = int(r.get("frags", 0))
                    st = q._status_from_names(r.get("name", ""), r.get("name_full", ""))
                    break
            q._merge_score_row(rows_by_key, fr, int(pg), st, nm, nm_k)

        rows = list(rows_by_key.values())

        if not rows:
            STATUS_W = 15  # status tail can use the full 15 chars
            hdr = f'{"ping":>5}  {"frags":>5}  {"name":<15}  {"status":<{STATUS_W}}'
            width = len(hdr)
            print("\n" + q._rule_with_title("Scoreboard", width))
            print("  (no players with ping yet)")
            print("=" * width + "\n")
            return

        # Sort & render
        rows.sort(key=lambda t: (t[0], -t[1]), reverse=True)  # frags desc, then lower ping
        me = q._sanitize_player_name(q.cvar_system.get_string("name", "pyquake-m"))
        me_k = q._name_key(me)
        STATUS_W = 15
        hdr = f'{"ping":>5}      {"frags":>5}  {"name":<15}  {"status":<{STATUS_W}}'
        width = len(hdr)
        # Title/rule spans the *exact* header width
        print("\n" + q._rule_with_title("Scoreboard", width))
        print(hdr)
        for fr, pg, st, nm, nm_k in rows:
            # Resolve pants color and emoji
            bottom_val = q._bottom_for_name_key(active._score_state, nm_k)
            # Fallback to our own CVAR color for our row if still unknown
            if bottom_val is None:
                bottom_val = q._maybe_self_bottom(active, nm_k)
            pants_emoji = q._pants_color_emoji(bottom_val)
            # Put the arrow right after the name (inside the name column),
            # and leave TWO spaces between emoji and name.
            name_disp = nm + (" ◀" if nm_k == me_k else "")
            print(
                f"{pg:>5}   {fr:>5} {pants_emoji}  "
                f"{name_disp:<15}  {st[:STATUS_W]:<{STATUS_W}}"
            )
        # Add ProQuake timer/team footer
        q.CON.write("")  # blank line before the footer
        try:
            line = active.format_showscores_footer(width)
            if line:
                q.CON.write(line)
        except Exception:
            pass
        print("=" * width + "\n")
        return
