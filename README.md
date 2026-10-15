# PyQuake-M

**Quake on the wire. Python at the console.**

A Python multiplayer console client for NetQuake, built on
[PyQuake](https://github.com/matthewearl/pyquake) with
[QSS-M](https://github.com/timbergeron/QSS-M) integration for competitive Quake
experimentation. Connect to servers, chat, check scores, record demos, and script
commands from your terminal.

**Python 3.10+ · Standard library only · MIT licensed**

[Get connected](#get-connected) · [Console reference](#console-reference) ·
[Record demos](#record-demos) · [Download assets](#download-assets) · [Protocol support](#protocol-support) ·
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

While connected, use `record match` (or `demo match`) to start recording to
`match.dem`, then `stop` to save it. Without a filename, `record` creates
`demos/map_MM-DD-YYYY-HHMMSS.dem`, adding a numeric suffix if needed. Quote
filenames containing spaces. Manual recording can start mid-map and continues
across map changes. Disconnecting or quitting also saves it.
With `--demo`, reconnecting starts a new file (`pyquake_2.dem`, and so on).
Play the saved file in QSS-M with `playdemo match` after copying it into the
game's demo search path; install the corresponding maps and mod assets there too.

Console recordings write packets to a `name.recording-*.dem` recovery file beside
the destination as they arrive, without keeping the whole recording in memory.
`stop` finishes the file and replaces the destination only after a successful
save. A failed write stops capture without disconnecting the client; `stop`
retries saving the captured prefix. After a crash, use
`recoverdemo "name.recording-ABC.dem" "recovered.dem"` to copy complete packets
into a playable file. The recovery command leaves the source untouched.

Mid-map recording restores current scoreboard, lightstyles, stats, view, pause,
fog, info strings, ProQuake teams/clock, and late static entities/sounds. Ordinary
gameplay then follows the incoming server packets.

```text
cl_autodemo 1         // record each map into its own timestamped file
cl_autodemo 2         // start/end on server match announcements
cl_autodemo 3         // automatic maps for multiplayer games only
cl_autodemo 4         // record maps with cuts on reload/change
cl_autodemo 0         // disable automatic recording
cl_demo_format dz    // compress completed recordings as QSS-M-compatible DZip
cl_demo_format dem   // use raw .dem output (default)
markdemo             // add a QSS-M-compatible marker; offline, list saved marks
cl_recordingdemo     // read-only current recording destination
cl_demo_minframes -100 // discard newly finished recordings under 100 records
```

`markdemo` saves history to `demomarks.json` beside the config file. Replacing a
demo clears its old marker entries only after the new file saves successfully.
DZip finalization runs in a worker so receiving and keepalives continue during
compression. Short-demo discarding preserves any existing destination. Positive `cl_demo_minframes`
values retain files; this console client has no demo browser to filter.
Automatic recordings split at map boundaries, including reloads of the same map.
Match automation ignores marked chat messages quoting server announcements.
Protocol negotiation also supports the FTE extensions described below.

Programmatic clients can call `record_demo(path)` for disk recording, then
`stop_recording()` and `publish(path)`. Calling `record_demo()` without a path
retains the existing in-memory API and `dump()` angle-calculator support.

## Download assets

Missing precached maps, models, and sounds download automatically before
`prespawn`. The client checks loose files and `.pak` directories first, tries
the configured web mirrors, then falls back to the connected server when it
advertises QSS-M legacy downloads. The receive loop and connection keepalives
continue during transfers. Missing or refused files are reported and sign-on
continues; this client does not need rendering assets to stay connected.

```text
cl_download_dir "id1"              // destination game directory, relative to cwd
cl_download_searchpath "/path/to/quake/id1" // additional installed game directories
download ultrav.bsp                // try web mirrors, then the connected server
download maps/ultrav.bsp server     // server only
download dm3.loc web               // web only; works offline too
download                          // show an active transfer's progress
stopdownload                      // cancel; partial files are discarded
cl_download_auto 0                 // disable automatic downloads, keep manual ones
allow_download 0                   // disable both automatic and manual downloads
```

`cl_download_dir` defaults to `id1` in the current directory. Set it to the game
directory you want QSS-M to use for playback, or copy downloaded assets there.
`cl_download_searchpath` uses the OS path separator (`:` on Linux/macOS, `;` on
Windows) between additional directories; include a mod's `id1` directory there
to reuse base-game PAKs. Inline models such as `*1` are never requested.

Web mirrors use `cl_web_download_url` and `cl_web_download_url2`, defaulting to
Q1Tools (`q1tools/q1tools.github.io`) and `maps.quakeworld.nu`. Use an HTTP(S)
base URL for a mirror with paths such as `maps/name.bsp`, or a GitHub
`user/repo[/branch]` value for QSS-M's letter-bucket map layout. Set either
variable to `""` to disable that mirror. Only configured mirrors are used;
servers cannot choose a web URL or filesystem destination.

Downloads are limited by `cl_download_maxsize` (50 MiB by default) and
`cl_download_timeout` (15 seconds without progress). Completed server transfers
must match the announced size and CRC. Files publish only after verification,
and existing installed assets are preserved. Download control messages and file
data are excluded from demos. This implements the legacy server path; chunked
download extensions and full mod/package installation are not negotiated.

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
CSQC, chunked download, voice, and cursor extensions are not advertised.
Legacy server downloads work independently of the FTE download extensions.

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

For an opt-in live check against an installed QSS-M engine and Quake assets:

```bash
python3 tests/check_qssm_demos.py --engine /path/to/quakespasm --assets /path/to/id1
```

This starts isolated local servers for protocols 15/666/999 and plays join,
mid-map, DZip, and map-change recordings in QSS-M using SDL's offscreen driver.
Logs and results stay under `.build/demo-parity/`.

The download integration check uses isolated QSS-M and HTTP servers to verify
server transfers, web transfers, fallback, sign-on, and playback of a demo
recorded during downloads:

```bash
python3 tests/check_qssm_downloads.py --engine /path/to/quakespasm --assets /path/to/id1
```

Its files and results stay under `.build/downloads/`.

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
