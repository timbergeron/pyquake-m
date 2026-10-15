"""Verify PEXT against a local QSS-M engine and installed Quake assets.

PYTHONDONTWRITEBYTECODE=1 python3 tests/live_pext.py --engine /path/to/quakespasm \
    --paks /path/to/quake/id1 --work-dir /path/with/free/space
"""
import argparse
import asyncio
import io
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyquake import client, proto


async def verify(args, version, mask):
    with tempfile.TemporaryDirectory(prefix='pyquake-pext-', dir=args.work_dir) as root:
        root = Path(root)
        (root / 'id1').mkdir()
        for pak in Path(args.paks).glob('pak*.pak'):
            (root / 'id1' / pak.name).symlink_to(pak.resolve())
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        log_path = root / 'server.log'
        with log_path.open('w') as log:
            server = subprocess.Popen([
                str(Path(args.engine).resolve()), '-dedicated', '4', '-nohome',
                '-basedir', str(root), '-ip', '127.0.0.1', '-port', str(port),
                '+sv_public', '0', '+developer', '1', '+sv_protocol',
                ('FTE' if mask else 'Base') + str(version), '+map', 'start',
            ], stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, cwd=root)
            active = kickstart = None
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError(log_path.read_text())
                    if 'Publicly listening' in log_path.read_text(errors='replace'):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise RuntimeError('Server did not start')
                command = f'pext {proto.PROTOCOL_FTE_PEXT1} 0 {proto.PROTOCOL_FTE_PEXT2} {mask}'
                with mock.patch.object(proto, 'pext_command', return_value=command):
                    active = await client.AsyncClient.connect('127.0.0.1', port)
                    kickstart = asyncio.create_task(client._kickstart_handshake(active))
                    await asyncio.wait_for(active.wait_until_spawn(), 10)
                assert active.protocol.pext2 == mask, active.protocol
                assert int(active.protocol.version) == version, active.protocol
                for _ in range(20):
                    await active._send_heartbeat()
                    await asyncio.wait_for(active.wait_for_update(), 2)
                    if active.view_entity in active.entities and active.stats.get(0) == 100:
                        break
                else:
                    raise AssertionError('No player state/health after spawn')
                before = active.player_entity.origin
                demo = active.record_demo()
                recvfrom = active._conn._udp.recvfrom
                dropped = []

                async def lossy_recvfrom():
                    while True:
                        data, address = await recvfrom()
                        if len(dropped) < 3 and data[:2] == b'\x00\x10':
                            dropped.append(struct.unpack_from('>I', data, 4)[0])
                            continue
                        return data, address

                active._conn._udp.recvfrom = lossy_recvfrom
                for _ in range(15):
                    active.move(0., 0., 0., 200, 0, 0, 0)
                    await asyncio.wait_for(active.wait_for_update(), 2)
                active._conn._udp.recvfrom = recvfrom
                assert len(dropped) == 3, dropped
                assert active.player_entity.origin != before, (before, active.player_entity.origin)
                assert active.stats[0] == 100
                if mask & proto.PEXT2_PREDINFO:
                    assert active.input_ack is not None
                    assert active.view_height is not None
                demo.stop_recording()
                output = io.BytesIO()
                demo.dump(output)
                (root / 'id1' / 'pext.dem').write_bytes(output.getvalue())
                output.seek(0)
                messages = [message for _, _, message in proto.read_demo_file(output)]
                assert messages[0].msg_type in (proto.ServerMessageType.SERVERINFO, proto.ServerMessageType.PRINT)
                assert messages[-1].msg_type == proto.ServerMessageType.DISCONNECT
                if mask:
                    frames = [m for m in messages if m.msg_type == proto.ServerMessageType.UPDATEENTITIES]
                    assert frames and frames[0].updates == [(0, None)]
                # Exercise sign-on and state resets on the same connection.
                old_spawn = active._spawned_fut
                server.stdin.write(b'changelevel start\n')
                server.stdin.flush()
                for _ in range(100):
                    if active._spawned_fut is not old_spawn and active._spawned_fut.done():
                        break
                    if active.disconnected:
                        raise AssertionError('Disconnected during map change')
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError('Map change did not finish sign-on')
                await active._send_heartbeat()
                await asyncio.wait_for(active.wait_for_update(), 2)
                assert active.protocol.pext2 == mask
                assert active.view_entity in active.entities
                server_output = log_path.read_text(errors='replace').lower()
                for error in ('unknown command char', 'tried to read past', 'host_error'):
                    assert error not in server_output, server_output
                if args.playback:
                    await playback(args, root)
                checks = 'spawn, movement, packet loss, stats, demo, map change'
                if args.playback:
                    checks += ', QSS-M playback'
                print(f'protocol={version} pext2={mask:#x}: {checks} passed', flush=True)
            except BaseException:
                print(log_path.read_text(errors='replace'), file=sys.stderr)
                if active and active._read_task.done() and not active._read_task.cancelled():
                    print('Reader result:', active._read_task.exception(), file=sys.stderr)
                raise
            finally:
                if kickstart:
                    kickstart.cancel()
                    await asyncio.gather(kickstart, return_exceptions=True)
                if active:
                    await active.disconnect()
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                server.stdin.close()


async def playback(args, root):
    log_path = root / 'playback.log'
    env = dict(os.environ, SDL_VIDEODRIVER='offscreen', SDL_AUDIODRIVER='dummy',
               LIBGL_ALWAYS_SOFTWARE='1')
    with log_path.open('w') as log:
        player = subprocess.Popen([
            str(Path(args.engine).resolve()), '-nohome', '-basedir', str(root),
            '-nosound', '-window', '-width', '320', '-height', '240',
            '+developer', '1', '+timedemo', 'pext',
        ], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=root, env=env)
        try:
            for _ in range(200):
                output = log_path.read_text(errors='replace')
                if ' fps' in output:
                    assert 'Host_Error' not in output and 'Illegible' not in output, output
                    return
                if player.poll() is not None:
                    break
                await asyncio.sleep(0.05)
            raise AssertionError('QSS-M demo playback failed:\n' + log_path.read_text(errors='replace'))
        finally:
            player.terminate()
            try:
                player.wait(timeout=5)
            except subprocess.TimeoutExpired:
                player.kill()
                player.wait(timeout=5)


async def main(args):
    client.CON = mock.Mock()
    with mock.patch.object(client, '_autosave_config'):
        for version, mask in ((15, 104), (666, 104), (999, 104), (666, 8), (666, 40), (666, 0)):
            await verify(args, version, mask)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', required=True)
    parser.add_argument('--paks', required=True)
    parser.add_argument('--work-dir', default=None)
    parser.add_argument('--playback', action='store_true', help='Also replay demos with SDL offscreen rendering')
    asyncio.run(main(parser.parse_args()))
