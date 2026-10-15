"""Live QSS-M server downloads, HTTP fallback, signon, and demo playback.

python3 tests/check_qssm_downloads.py --engine /path/to/quakespasm --assets /path/to/id1
"""

import argparse
import asyncio
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))
from pyquake import client, proto
from check_qssm_demos import finish_process, playback


def extract(pak, name):
    with pak.open("rb") as source:
        magic, offset, size = struct.unpack("<4sII", source.read(12))
        assert magic == b"PACK"
        source.seek(offset)
        entries = source.read(size)
        for index in range(0, size, 64):
            entry, start, length = struct.unpack_from("<56sII", entries, index)
            if entry.split(b"\0", 1)[0].decode() == name:
                source.seek(start)
                return source.read(length)
    raise ValueError(f"{name} missing from {pak}")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    args = parser.parse_args()
    engine, assets = args.engine.resolve(), args.assets.resolve()
    artifacts = REPOSITORY / ".build" / "downloads"
    artifacts.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=artifacts))
    server_root, client_root, web_root = root / "server", root / "client", root / "web"
    for game in (server_root / "id1", client_root / "id1"):
        game.mkdir(parents=True)
        for name in ("pak0.pak", "pak1.pak"):
            (game / name).symlink_to(assets / name)
    (server_root / "quakespasm.pak").symlink_to(engine.parent / "quakespasm.pak")
    (server_root / "id1/maps").mkdir()
    (server_root / "id1/locs").mkdir()
    (web_root / "maps").mkdir(parents=True)
    (web_root / "locs").mkdir()
    bsp = extract(assets / "pak0.pak", "maps/start.bsp")
    for name in ("server_probe", "web_probe", "fallback_probe"):
        (server_root / f"id1/maps/{name}.bsp").write_bytes(bsp)
    (web_root / "maps/web_probe.bsp").write_bytes(bsp)
    payload = b"0 0 0 download_probe\n" * 2048
    (server_root / "id1/locs/probe.loc").write_bytes(payload)
    (web_root / "locs/offline.loc").write_bytes(payload)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    http_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(web_root)))
    thread = threading.Thread(target=http_server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    active = pump = server = None
    results = []
    print(f"Artifacts: {root}", flush=True)
    os.chdir(client_root)
    client.cvar_system = client.CvarSystem()
    client.register_download_cvars(client)
    settings = client.cvar_system
    settings._config_file = str(client_root / "config.cfg")
    settings.set("cl_download_dir", str(client_root / "id1"))
    settings.set("cl_web_download_url", "")
    settings.set("cl_web_download_url2", "")
    settings.set("cl_download_timeout", "5")
    try:
        with (root / "server.log").open("w") as log:
            server = subprocess.Popen(
                [str(engine), "-basedir", str(server_root), "-nohome", "-dedicated", "4",
                 "-port", str(port), "-noipx", "+sv_public", "0", "+sv_protocol", "Base-666",
                 "+sys_ticrate", "0.001", "+allow_download", "2", "+deathmatch", "1", "+map", "start"],
                cwd=server_root, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
            await asyncio.sleep(2)
            active = await asyncio.wait_for(client.AsyncClient.connect("127.0.0.1", port), 10)

            async def heartbeat():
                while True:
                    if active._read_task.done():
                        active._read_task.result()
                    await active._send_heartbeat()
                    await asyncio.sleep(.025)
            pump = asyncio.create_task(heartbeat())
            await asyncio.wait_for(active.wait_until_spawn(), 15)
            assert active.downloads.server_available
            demo_path = server_root / "id1/downloads.dem"
            demo = active.record_demo(demo_path)
            await asyncio.wait_for(active.downloads.start("probe.loc", "server"), 20)
            assert (client_root / "id1/locs/probe.loc").read_bytes() == payload
            results.append({"case": "manual-server", "bytes": len(payload)})
            print("PASS manual server transfer", flush=True)
            for name, source in (("server_probe", "server"), ("web_probe", "web"), ("fallback_probe", "fallback")):
                settings.set("cl_web_download_url", "" if source == "server" else f"http://127.0.0.1:{http_server.server_port}")
                server.stdin.write(f"changelevel {name}\n".encode())
                server.stdin.flush()
                for _ in range(200):
                    if active.models and active.models[0] == f"maps/{name}.bsp":
                        break
                    if active._read_task.done():
                        active._read_task.result()
                    await asyncio.sleep(.025)
                assert active.models[0] == f"maps/{name}.bsp"
                await asyncio.wait_for(active.wait_until_spawn(), 40)
                downloaded = client_root / f"id1/maps/{name}.bsp"
                assert downloaded.read_bytes() == bsp
                server_log = (root / "server.log").read_text()
                if source == "web":
                    assert f"downloading maps/{name}.bsp to" not in server_log
                else:
                    assert f"downloading maps/{name}.bsp to" in server_log
                results.append({"case": "automatic-" + source, "bytes": len(bsp),
                                "sha256": hashlib.sha256(downloaded.read_bytes()).hexdigest()})
                print(f"PASS automatic {source} transfer + spawn ({len(bsp)} bytes)", flush=True)
                await asyncio.sleep(.2)
            demo.stop_recording()
            demo.publish(demo_path)
            with demo_path.open("rb") as recorded:
                messages = [m for _, _, m in proto.read_demo_file(recorded)]
            assert all(m.msg_type != proto.ServerMessageType.DOWNLOADDATA for m in messages)
            assert len([m for m in messages if m.msg_type == proto.ServerMessageType.SERVERINFO]) == 4
            assert b"cl_downloadbegin" not in demo_path.read_bytes()
            await active.disconnect()
            active = None
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            pump = None
            offline = client.DownloadManager(None, settings, print)
            await asyncio.wait_for(offline.start("offline.loc", "web"), 5)
            assert (client_root / "id1/locs/offline.loc").read_bytes() == payload
            results.append({"case": "offline-web", "bytes": len(payload)})
            await finish_process(server)
            results.append(await playback(engine, server_root, demo_path))
            (root / "results.json").write_text(json.dumps(results, indent=2) + "\n")
            print("PASS: server, web, fallback, offline downloads, and QSS-M demo playback", flush=True)
    finally:
        if pump is not None:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        if active is not None:
            await active.disconnect()
        if server is not None:
            await finish_process(server)
        await asyncio.to_thread(http_server.shutdown)
        http_server.server_close()
        thread.join()


if __name__ == "__main__":
    asyncio.run(main())
