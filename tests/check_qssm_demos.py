"""Opt-in live server and playback check; requires QSS-M and Quake pak files.

python3 tests/check_qssm_demos.py --engine /path/to/quakespasm --assets /path/to/id1
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))
from pyquake import client, proto
from pyquake.console import ConsoleSession


async def finish_process(process):
    if process.poll() is None:
        try:
            process.stdin.write(b"quit\n")
            process.stdin.flush()
            await asyncio.to_thread(process.wait, 5)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            process.terminate()
            await asyncio.to_thread(process.wait, 5)


async def record(engine, root, version):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    active = session = None
    paths = []
    with (root / f"server-{version}.log").open("w") as log:
        server = subprocess.Popen(
            [str(engine), "-basedir", str(root), "-nohome", "-dedicated", "4",
             "-port", str(port), "-noipx", "+sv_public", "0", "+sv_protocol", f"Base-{version}",
             "+deathmatch", "1", "+map", "start"],
            stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, cwd=root)
        try:
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError((root / f"server-{version}.log").read_text())
                if "start" in (root / f"server-{version}.log").read_text() and "Server spawned" in (root / f"server-{version}.log").read_text():
                    break
                await asyncio.sleep(.05)
            active = await asyncio.wait_for(client.AsyncClient.connect("127.0.0.1", port), 10)
            initial_path = root / "id1" / f"initial-{version}.dem"
            initial = active.record_demo(initial_path)
            await asyncio.wait_for(active.wait_until_spawn(), 15)
            await asyncio.wait_for(active.wait_for_update(), 5)

            async def frames(count):
                for _ in range(count):
                    if active._read_task.done():
                        active._read_task.result()
                    await active._send_heartbeat()
                    await asyncio.sleep(.05)

            await frames(10)
            initial.stop_recording()
            initial.publish(initial_path)
            paths.append(initial_path)
            mid_path = root / "id1" / f"mid-{version}.dem"
            dz_path = root / "id1" / f"mid-{version}.dz"
            mid, dz = active.record_demo(mid_path), active.record_demo(dz_path)
            mid.mark()
            dz.mark()
            await frames(10)
            for demo, path in ((mid, mid_path), (dz, dz_path)):
                demo.stop_recording()
                demo.publish(path)
                paths.append(path)
            client.CURRENT_CLIENT = active
            session = ConsoleSession(client)
            map_path = root / "id1" / f"mapchange-{version}.dem"
            await session.dispatcher.execute(f'record "{map_path}"')
            previous = active.level_name
            server.stdin.write(b"changelevel e1m1\n")
            server.stdin.flush()
            for _ in range(100):
                if active.level_name != previous:
                    break
                await frames(1)
            assert active.level_name != previous
            await asyncio.wait_for(active.wait_until_spawn(), 15)
            await frames(10)
            server.stdin.write(b"quit\n")
            server.stdin.flush()
            await asyncio.wait_for(asyncio.shield(active._read_task), 10)
            await asyncio.sleep(0)
            assert session.demo is None and map_path.is_file()
            with map_path.open("rb") as stream:
                parsed = list(proto.read_demo_file(stream))
            assert len([m for _, _, m in parsed if m.msg_type == proto.ServerMessageType.SERVERINFO]) == 2
            assert parsed[-1][2].msg_type == proto.ServerMessageType.DISCONNECT
            paths.append(map_path)
        finally:
            if session is not None:
                await session.close()
            elif active is not None:
                await active.disconnect()
            client.CURRENT_CLIENT = None
            await finish_process(server)
    return paths


async def playback(engine, root, path):
    log_path = root / ("playback-" + path.name + ".log")
    environment = dict(os.environ, SDL_VIDEODRIVER="offscreen", SDL_AUDIODRIVER="dummy",
                       LIBGL_ALWAYS_SOFTWARE="1")
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [str(engine), "-basedir", str(root), "-nohome", "-nosound", "-nojoy", "-window",
             "-width", "320", "-height", "240", "+cl_demoreel", "0", "+timedemo", path.name],
            stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, cwd=root, env=environment)
        try:
            for _ in range(400):
                output = log_path.read_text(errors="replace")
                match = re.search(r"(\d+) frames\s+([\d.]+) seconds\s+([\d.]+) fps", output)
                if match:
                    assert int(match[1]) > 1, output
                    assert not re.search(r"Host_Error|Sys_Error|CRC error|demo read failure|malformed", output, re.I), output
                    result = {"demo": path.name, "frames": int(match[1]), "bytes": path.stat().st_size}
                    print(json.dumps(result), flush=True)
                    return result
                if process.poll() is not None:
                    raise RuntimeError(output)
                await asyncio.sleep(.05)
            raise TimeoutError(f"Playback did not finish: {log_path}")
        finally:
            await finish_process(process)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    args = parser.parse_args()
    engine, assets = args.engine.resolve(), args.assets.resolve()
    artifacts = REPOSITORY / ".build" / "demo-parity"
    artifacts.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=artifacts))
    (root / "id1").mkdir()
    for name in ("pak0.pak", "pak1.pak"):
        (root / "id1" / name).symlink_to(assets / name)
    (root / "quakespasm.pak").symlink_to(engine.parent / "quakespasm.pak")
    os.chdir(root)
    client.cvar_system = client.CvarSystem()
    client.cvar_system._config_file = str(root / "config.cfg")
    print(f"Artifacts: {root}", flush=True)
    results = []
    for version in (15, 666, 999):
        paths = await record(engine, root, version)
        for path in paths:
            results.append(await playback(engine, root, path))
    (root / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(f"PASS: {len(results)} QSS-M playbacks", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
