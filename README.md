# PyQuake-M

PyQuake-M is a fork of [PyQuake](https://github.com/matthewearl/pyquake) adding multiplayer support and integration with [QSS-M](https://github.com/timbergeron/QSS-M) for competitive NetQuake experimentation.

Requires Python 3.10 or newer. No third-party packages are required. Run from the repository root:

```bash
python3 -m pyquake.client denver.quakeone.com 26000
```

The port defaults to 26000 when omitted. Use `python` or `py` in place of `python3` on Windows. Direct invocation also works: `python3 pyquake/client.py localhost 26000`.

Record a demo with `--demo` (saved as `pyquake.dem` on exit), or check a server handshake with `test`:

```bash
python3 -m pyquake.client --demo localhost 26000
python3 -m pyquake.client test localhost 26000
```

Use `help` in the console to list commands and `quit` to exit. Settings and aliases are saved to `config.cfg` in the current working directory.

ProQuake connections automatically negotiate 16-bit input angles when supported. For a password-protected server, set the password before connecting; numeric and text passwords are supported:

```text
password "your password"
connect localhost:26000
```

The scoreboard reads ProQuake team scores and match clocks, including pauses, sudden death (`SD`), and negative countdowns.

Local identity history is saved as `iplog.dat` beside the config file. `status` replies collect player names and IPv4 prefixes, including masked addresses. Like QSS-M, history groups addresses by their first three octets and stores classic 15-byte names.

```text
identify 1                   // player slots start at 1; #1 also works
identify "player name"       // full or partial name, ignoring Quake font styling
identify                     // most recently connected player seen by this client
identify 192.0.2             // search saved history, including while disconnected
ipnames                      // list unique recorded names
ipdump                       // export iplog.txt beside the config file
ipmerge "existing/iplog.dat" // import a ProQuake or QSS-M binary history file
```

`identify` requests `status` when it needs a player's address. History survives reconnects and restarts. `ipdump` also accepts an output filename.

Input defaults to commands (`cl_chatmode 0`), so `ready` reaches the server as a command. Use `say hello` or `s hello` to chat. The archived `cl_chatmode` setting controls typed input:

| Value | Behavior |
| --- | --- |
| `0` | Command input; chat uses `say` or `s`. |
| `1` | Unknown input becomes chat; known commands, cvars, and aliases still execute. A leading space forces chat. |
| `2` | Automatic chat, with a leading space prefilled when readline is available. Remove the space to enter a known command. |
| `3` | A leading space sends chat; unspaced input executes commands. |

These modes follow QSS-M's console chat behavior while connected. Offline input always executes as commands. Use `/command` to force a command in any mode, including after a prefilled space. In modes `2` and `3`, readline prefills a space; delete it to type commands. Without readline, add the space manually.

Scripts, aliases, and queued commands always execute as commands. They share the console's dispatcher, including local commands, cvars, nested `exec`, semicolon batches, and `wait [frames]`. Quotes preserve semicolons; `//` starts a comment outside quotes after whitespace.

```text
alias warmup "ready; wait; status"
warmup
seta cl_chatmode 1
writeconfig
exec "practice.cfg"
```

Aliases work offline and survive reconnects. `cfg_save_aliases 1` (the default) includes them in saved configs; set it to `0` to save only archived settings. Use `seta name value` to create a saved custom variable. Nested config paths are checked from the working directory, then relative to the calling config.

Run the regression checks with:

```bash
python3 -m unittest discover -s tests -v
```
