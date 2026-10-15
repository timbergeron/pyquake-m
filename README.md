# PyQuake-M

**Quake on the wire. Python at the console.**

A Python multiplayer console client for NetQuake, built on
[PyQuake](https://github.com/matthewearl/pyquake) with
[QSS-M](https://github.com/timbergeron/QSS-M) integration for competitive Quake
experimentation. Connect to servers, chat, check scores, record demos, and script
commands from your terminal.

**Python 3.10+ · Standard library only · MIT licensed**

[Get connected](#get-connected) · [Console reference](#console-reference) ·
[Record demos](#record-demos) · [Protocol support](#protocol-support) ·
[Development](#development)

## What you can do

- **Keep up with the server:** public and team chat, player lists, live scores,
  pings, ProQuake team scores, and match clocks.
- **Capture the action:** start recording after joining, continue across map
  changes, and replay the demo in QSS-M.
- **Make the console your own:** Quake-style variables, persistent aliases,
  config scripts, command batches, and tab completion when readline is available.
- **Remember familiar names:** keep local player identity history and import
  existing ProQuake or QSS-M IP logs.
- **Experiment with the protocol:** inspect network counters and work with
  parsed entities, stats, and demos in Python.

PyQuake-M runs in a terminal and does not render the game world. The console
client does not need Quake game assets; playing its demos in QSS-M requires the
matching maps and mod assets.

## Get connected

Clone the repository and run from its root. No package installation is needed:

```bash
git clone https://github.com/timbergeron/pyquake-m.git
cd pyquake-m
python3 -m pyquake.client localhost 26000
```

Replace `localhost` with your server's hostname or IPv4 address. The port defaults
to `26000` when omitted. On Windows, use `python` or `py` in place of `python3`.
Direct invocation also works:

```bash
python3 pyquake/client.py localhost 26000
```

Once connected, try:

```text
name "your name"
color 4 13
status
showscores
say hello quake
```

Use `help` to list commands and `quit` to save settings and exit. Commands such as
`ready`, `observe`, and `vote` are forwarded to the server; their behavior depends
on the server's mod.

### Switch servers and reconnect

```text
connect localhost:26000
disconnect
connect last
reconnect
```

The interactive `connect` command uses `host[:port]`. The remembered server is
available during the current session. Use `port 26001` to change the default port
for subsequent connections without an explicit port.

For a password-protected server, set the password before connecting. Numeric and
text passwords are supported:

```text
password "your password"
connect localhost:26000
```

`cl_idle N` enables automatic reconnection to the last server: it retries
immediately when disconnected, then every `N` minutes (`1`–`60`). The setting is
saved across restarts, and changing it rearms the retry timer. `cl_idle 0` disables
automatic retries, including retries already waiting in the command queue.

Manual `disconnect` also triggers reconnection while `cl_idle` is enabled. To stay
offline, use:

```text
cl_idle 0; disconnect
```

## Console reference

Commands are the default input mode, so `ready` reaches the server as a command.
Use `say` or `s` for public chat, and `say_team` or `st` for team chat.

| Command | What it does |
| --- | --- |
| `help` / `cmdlist` | List console commands. |
| `status` / `players` | Request server status or a player list. |
| `showscores` / `scoreboard` | Show the live scoreboard with pings and frags. |
| `name "your name"` | Change your player name. |
| `color 4 13` | Set shirt and pants colors. |
| `s hello` / `st hello team` | Send public or team chat. |
| `tell #1 hello` | Forward a private message to the server. |
| `cmd <command>` | Explicitly forward a server command. |
| `find <text>` | Search commands, variables, and aliases. |
| `cvarlist` / `aliaslist` | Inspect variables or aliases. |
| `clear` / `condump [file]` | Clear the console or save its text. |
| `net_stats` / `net_stats *` | Show network counters for the current connection. |
| `quit` | Disconnect, save settings, and exit. |

The scoreboard understands ProQuake team scores and match clocks, including
pauses, sudden death (`SD`), and negative countdowns. Private messages and other
forwarded commands require support from the server.

### Choose your chat style

The archived `cl_chatmode` variable follows QSS-M's console chat behavior:

| Value | Behavior while connected |
| --- | --- |
| `0` | Commands by default; chat uses `say` or `s`. |
| `1` | Unknown input becomes chat. Known commands, variables, and aliases execute; a leading space forces chat. |
| `2` | Chat by default, with a space prefilled when readline is available. Remove it to enter a known command. |
| `3` | A leading space sends chat; unspaced input executes commands. |

Use `/command` to force a command in any mode, including after a prefilled space.
In modes `2` and `3`, delete the prefilled space to type commands; without readline,
add the space manually. Offline input, scripts, aliases, and queued commands
always execute as commands.

## Record demos

Start a recording when launching the client:

```bash
python3 -m pyquake.client --demo localhost 26000
```

Or start one from the console at any point after joining:

```text
record match
stop
```

`record match` writes `match.dem`; `demo match` is an alias for the same command.
Omit the filename to use `pyquake.dem`, or quote filenames containing spaces:

```text
record "practice session"
```

Recording can begin mid-map and continues across map changes. `stop`, disconnecting,
or quitting saves the file. With `--demo`, reconnecting starts a new file:
`pyquake_2.dem`, `pyquake_3.dem`, and so on.

Copy the demo into QSS-M's demo search path, then run this in its console:

```text
playdemo match
```

Install the matching maps and mod assets in QSS-M before playback.

## Protocol support

PyQuake-M supports NetQuake (`15`), FitzQuake (`666`), and RMQ (`999`). ProQuake
connections automatically negotiate 16-bit input angles when supported.

QSS-M connections negotiate these FTE extensions over all three base protocols:

| Extension | Support |
| --- | --- |
| `REPLACEMENTDELTAS` (`0x08`) | Persistent entity deltas, extended baselines/statics, removals, world resets, and frame acknowledgements. |
| `PREDINFO` (`0x20`) | Input sequence acknowledgements, replacement stats, and server gamedir information. |
| `NEWSIZEENCODING` (`0x40`) | Extended entity size encoding. |

The advertised `PEXT2` mask is `0x68`. The client also parses extended sound,
precache, and particle messages used with replacement deltas. Demo recordings
include the state needed to start playback after joining a map.

Negotiation is automatic. The protocol banner reflects what the server actually
selected: for example, `666` for base FitzQuake or `fte666` with negotiated
extensions. Servers without the negotiated extensions use their base protocol.
CSQC, downloads, voice, and cursor extensions are not advertised.

## Player identity history

`status` replies collect player names and IPv4 prefixes, including masked
addresses. History is stored locally in `iplog.dat` beside the config file and
survives reconnects and restarts. Like QSS-M, it groups addresses by their first
three octets and stores classic 15-byte names.

```text
identify 1                   // player slots start at 1; #1 also works
identify "player name"       // full or partial name, ignoring Quake font styling
identify                     // most recently connected player seen by this client
identify 192.0.2             // search saved history, including while disconnected
ipnames                      // list unique recorded names
ipdump                       // export iplog.txt beside the config file
ipmerge "existing/iplog.dat" // import a ProQuake or QSS-M binary history file
```

`identify` requests `status` when it needs a player's address. `ipdump` also accepts
an output filename. Results reflect names associated with an address prefix;
several players can share a prefix.

## Configs and scripting

`config.cfg` is loaded at startup and saved in the current working directory.
Archived settings and aliases persist between sessions; `writeconfig` saves them
immediately, and `writeconfig practice.cfg` writes a separate config.

Scripts use the same dispatcher as the console, including local commands,
variables, aliases, nested `exec`, semicolon batches, and `wait [frames]`:

```text
alias warmup "ready; wait; status"
warmup
seta cl_chatmode 1
seta practice_server "localhost:26000"
writeconfig
writeconfig practice.cfg
exec "practice.cfg"
```

`wait` defers the rest of the command buffer until the next dispatcher frame;
`wait 5` waits five frames. Quotes preserve semicolons, and `//` starts a comment
outside quotes after whitespace.

Aliases can also take arguments. Use `$1` through `$9` for individual arguments,
or `$*` for all of them:

```text
alias greet "say hello $1"
greet quake
```

Aliases work offline and survive reconnects. `cfg_save_aliases 1` (the default)
includes them in saved configs; set it to `0` to save only archived settings.
Use `seta <variable> <value>` to create a saved custom variable. Nested config
paths are checked from the working directory, then relative to the calling config.

## Diagnostics

Check the UDP connection and reliable command transmission without completing
the full spawn sequence:

```bash
python3 -m pyquake.client test localhost 26000
```

For console diagnostics, try:

```text
version
net_stats
developer 1
con_logcenterprint 1
condump session.txt
```

`developer 1` enables additional informational output. `con_logcenterprint 1`
logs server centerprints, suppressing consecutive duplicates. `net_stats` shows
client-side counters; reliable receive counts are approximations.

Remote console commands use the `rcon_password` variable:

```text
rcon_password "your rcon password"
rcon status
```

## Development

The regression suite uses Python's standard `unittest` runner and requires no
running server or Quake assets:

```bash
python3 -m unittest discover -s tests -v
```

For live interoperability checks, supply a QSS-M executable and a directory
containing your Quake `pak*.pak` files:

```bash
python3 tests/live_pext.py \
  --engine /path/to/quakespasm \
  --paks /path/to/quake/id1 \
  --work-dir /path/to/writable/temp
```

The harness starts local servers and checks sign-on, movement, stats, demo
parsing, and map changes while deliberately dropping three consecutive UDP
packets. It covers protocols `15`, `666`, and `999` with `PEXT2 0x68`, plus protocol
`666` with masks `0x08`, `0x28`, and base-protocol fallback.

Add `--playback` to replay the generated demos in QSS-M using SDL offscreen
rendering. That option requires a working offscreen OpenGL environment. Temporary
server and demo files are removed when each run finishes.

### Find your way around

| File | Responsibility |
| --- | --- |
| [pyquake/client.py](pyquake/client.py) | Async client, entity state, demo recording, and interactive entry point. |
| [pyquake/console.py](pyquake/console.py) | Console commands, connections, and recording lifecycle. |
| [pyquake/commands.py](pyquake/commands.py) | Command parsing, aliases, config execution, and queued commands. |
| [pyquake/proto.py](pyquake/proto.py) | Server message parsing, protocol extensions, and demo reading. |
| [pyquake/aiodgram.py](pyquake/aiodgram.py) | UDP transport, connection handshakes, and reliable delivery. |
| [pyquake/iplog.py](pyquake/iplog.py) | Persistent identity history and binary IP log compatibility. |
| [tests/](tests/) | Regression tests and the live QSS-M validation harness. |

## Credits and license

Built on Matthew Earl's [PyQuake](https://github.com/matthewearl/pyquake), with
multiplayer features and console/protocol behavior aligned with
[QSS-M](https://github.com/timbergeron/QSS-M).

Released under the [MIT License](LICENSE).
