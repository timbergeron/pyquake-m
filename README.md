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

Use `help` in the console to list commands and `quit` to exit. Settings are saved to `config.cfg` in the current working directory.

Run the regression checks with:

```bash
python3 -m unittest discover -s tests -v
```
