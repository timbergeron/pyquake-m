"""Asset lookup, HTTP mirrors, and QSS-M legacy server downloads."""

import asyncio
import binascii
import http.client
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import struct
import tempfile
import threading
import time
import urllib.parse
import urllib.error
import urllib.request


class DownloadError(Exception):
    pass


def register_cvars(api, cvars=None):
    cvars = cvars if cvars is not None else api.cvar_system
    for name, value, kind, minimum, maximum, description in (
        ("allow_download", "2", api.CvarType.INT, 0, 3, "0=disable downloads; nonzero=allow"),
        ("cl_download_auto", "1", api.CvarType.BOOL, 0, 1, "Fetch missing precached assets before prespawn"),
        ("cl_download_dir", "id1", api.CvarType.STRING, None, None, "Game directory for downloaded assets"),
        ("cl_download_searchpath", "", api.CvarType.STRING, None, None, "Additional game directories separated by the OS path separator"),
        ("cl_web_download_url", "q1tools/q1tools.github.io", api.CvarType.STRING, None, None, "Primary HTTP mirror or GitHub user/repo[/branch]"),
        ("cl_web_download_url2", "maps.quakeworld.nu", api.CvarType.STRING, None, None, "Secondary HTTP mirror"),
        ("cl_download_maxsize", "50", api.CvarType.INT, 1, 1024, "Maximum asset size in MiB"),
        ("cl_download_timeout", "15", api.CvarType.INT, 1, 120, "Download inactivity timeout in seconds"),
    ):
        if cvars.find(name) is None:
            cvars.register(api.Cvar(name, value, api.CvarFlags.ARCHIVE,
                                     cvar_type=kind, minimum=minimum, maximum=maximum,
                                     description=description))


_ASSET_TYPES = {
    "maps": {".bsp", ".lit", ".lit2", ".lux", ".ent"},
    "progs": {".mdl", ".md3", ".iqm", ".spr", ".spr32"},
    "models": {".mdl", ".md3", ".iqm", ".spr", ".spr32", ".bsp"},
    "sound": {".wav", ".ogg"},
    "locs": {".loc"},
    "gfx": {".tga", ".png", ".jpg", ".jpeg", ".dds", ".lmp"},
    "textures": {".tga", ".png", ".jpg", ".jpeg", ".dds", ".wad"},
}


def asset_name(name, *, manual=False):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_+./ -]{1,240}", name):
        raise DownloadError("unsupported asset path")
    if any(not part or part.startswith(".") or part.endswith((" ", ".")) or
           re.fullmatch(r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
           for part in name.split("/")) or name.startswith("/"):
        raise DownloadError("asset paths must stay inside the game directory")
    path = PurePosixPath(name)
    if manual and len(path.parts) == 1:
        prefix = {".bsp": "maps", ".lit": "maps", ".ent": "maps", ".loc": "locs",
                  ".mdl": "progs", ".md3": "progs", ".spr": "progs", ".wav": "sound", ".ogg": "sound"}.get(path.suffix.lower())
        if prefix:
            name = prefix + "/" + name
            path = PurePosixPath(name)
    if len(path.parts) < 2 or path.suffix.lower() not in _ASSET_TYPES.get(path.parts[0].lower(), set()):
        raise DownloadError("unsupported asset type or directory")
    return name


def mirror_url(base, name):
    """Match QSS-M's GitHub, Q1Tools, QW map, and generic mirror layouts."""
    base = base.strip().rstrip("/")
    if not base:
        raise DownloadError("empty web mirror")
    filename = PurePosixPath(name).name
    bucket = "0-9" if filename[0].isdigit() else filename[0].upper()
    if "://" not in base and re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?", base):
        parts = base.split("/", 2)
        branch = parts[2] if len(parts) == 3 else "main"
        relative = f"maps/{bucket}/{filename}" if name.startswith("maps/") else name
        return "https://raw.githubusercontent.com/" + urllib.parse.quote(f"{parts[0]}/{parts[1]}/{branch}/{relative}", safe="/")
    if "://" not in base:
        base = "https://" + base
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        raise DownloadError("web mirrors must be HTTP or HTTPS URLs")
    if parsed.hostname == "maps.quakeworld.nu":
        relative = name if name.startswith("locs/") else "all/" + filename
        base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    elif parsed.hostname.endswith(".github.io") and name.startswith("maps/"):
        relative = f"maps/{bucket}/{filename}"
    else:
        relative = name
    return base + "/" + urllib.parse.quote(relative, safe="/")


class AssetStore:
    def __init__(self, directory, search=()):
        self.root = Path(directory).expanduser().resolve()
        self.search = [self.root, *(Path(p).expanduser().resolve() for p in search if p)]
        self._pak_cache = {}
        self.cleanup_warning = None

    def destination(self, name):
        name = asset_name(name)
        path = self.root.joinpath(*PurePosixPath(name).parts)
        if not path.resolve().is_relative_to(self.root):
            raise DownloadError("asset path crosses a symlink outside the game directory")
        return path

    def exists(self, name):
        self.destination(name)
        for root in self.search:
            if (root / name).is_file():
                return True
            for pak in sorted(root.glob("*.pak")):
                try:
                    stat = pak.stat()
                    key = (stat.st_mtime_ns, stat.st_size)
                    cached = self._pak_cache.get(pak)
                    if cached is None or cached[0] != key:
                        names = set()
                        with pak.open("rb") as source:
                            header = source.read(12)
                            magic, offset, length = struct.unpack("<4sII", header)
                            if magic != b"PACK" or length % 64 or length > 64 * 65536 or offset + length > stat.st_size:
                                continue
                            source.seek(offset)
                            for _ in range(length // 64):
                                raw, start, size = struct.unpack("<56sII", source.read(64))
                                if start + size <= stat.st_size:
                                    names.add(raw.split(b"\0", 1)[0].decode("latin-1"))
                        self._pak_cache[pak] = (key, names)
                    if name in self._pak_cache[pak][1]:
                        return True
                except (OSError, struct.error):
                    continue
        return False

    def stage(self, name):
        path = self.destination(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.destination(name)
        output = tempfile.NamedTemporaryFile(mode="w+b", dir=path.parent, prefix=".download-", delete=False)
        return output, Path(output.name)

    def publish(self, name, temporary):
        destination = self.destination(name)
        # Linking publishes the complete file atomically without replacing a
        # file installed by another download or the user while we were fetching.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if not destination.is_file():
                raise DownloadError(f"asset destination is not a file: {destination}")
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            self.cleanup_warning = f"Downloaded file is installed; could not remove {temporary}: {exc}"
        return destination


def validate_content(name, path):
    with path.open("rb") as stream:
        prefix = stream.read(256)
    if prefix.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise DownloadError("mirror returned an HTML page")
    suffix = PurePosixPath(name).suffix.lower()
    signatures = {".bsp": (struct.pack("<I", 29), b"BSP2", b"2PSB"), ".lit": (b"QLIT",),
                  ".mdl": (b"IDPO",), ".md3": (b"IDP3",), ".spr": (b"IDSP",),
                  ".ogg": (b"OggS",), ".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8",),
                  ".jpeg": (b"\xff\xd8",), ".wav": (b"RIFF",)}
    if suffix in signatures and not prefix.startswith(signatures[suffix]):
        raise DownloadError(f"invalid {suffix} file header")
    if suffix == ".wav" and prefix[8:12] != b"WAVE":
        raise DownloadError("invalid WAV file header")
    if suffix == ".bsp":
        size = path.stat().st_size
        if len(prefix) < 124:
            raise DownloadError("truncated BSP header")
        for offset, length in struct.iter_unpack("<ii", prefix[4:124]):
            if offset < 0 or length < 0 or offset > size or length > size - offset:
                raise DownloadError("BSP lump outside the downloaded file")


class _HttpRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme not in ("http", "https"):
            raise DownloadError("unsupported download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class ServerTransfer:
    name: str
    store: AssetStore
    future: asyncio.Future
    maximum: int
    temporary: Path = None
    output: object = None
    size: int = 0
    received: int = 0
    progress_at: float = 0
    ignore_stop: bool = False

    def cleanup(self):
        try:
            if self.output is not None:
                self.output.close()
        finally:
            if self.temporary is not None:
                self.temporary.unlink(missing_ok=True)


class DownloadManager:
    def __init__(self, client, cvars, output):
        self.client, self.cvars, self.output = client, cvars, output
        self.server_available = False
        self.task = None
        self.server = None
        self.name = None
        self.source = None
        self.received = self.total = 0
        self._generation = 0
        self._cancelled_server = False
        self._last_print = 0
        self._signon_pending = False
        self._tasks = set()
        self._cancelling = set()

    @property
    def busy(self):
        return ((self.task is not None and not self.task.done()) or
                any(not task.done() for task in self._tasks))

    def _launch(self, coroutine):
        self.task = asyncio.create_task(coroutine)
        self._tasks.add(self.task)
        self.task.add_done_callback(self._finished)
        return self.task

    def store(self):
        directory = self.cvars.get_string("cl_download_dir", "id1")
        if not directory.strip():
            raise DownloadError("cl_download_dir must name a game directory")
        extra = self.cvars.get_string("cl_download_searchpath").split(os.pathsep)
        return AssetStore(directory, extra)

    def _progress(self, name, source, received, total):
        self.name, self.source, self.received, self.total = name, source, received, total
        now = time.monotonic()
        if now - self._last_print >= 1:
            percent = f" ({100 * received // total}%)" if total else ""
            self.output(f"Downloading {name} from {source}: {received} bytes{percent}")
            self._last_print = now

    def start(self, name, source="auto"):
        name = asset_name(name, manual=True)
        if source not in ("auto", "web", "server"):
            raise DownloadError("source must be auto, web, or server")
        if not self.cvars.get_value("allow_download"):
            raise DownloadError("downloads disabled (allow_download 0)")
        if self.busy:
            raise DownloadError("a download is already active; use stopdownload first")
        store = self.store()
        if store.exists(name):
            self.output(f"Asset already installed: {name}")
            return None
        return self._launch(self._run([name], store, source, self._generation))

    def signon(self, models, sounds):
        if self._signon_pending and self.busy:
            return True
        if not self.cvars.get_value("allow_download") or not self.cvars.get_bool("cl_download_auto", True):
            return False
        try:
            store = self.store()
            names = []
            for name in [*(models or []), *("sound/" + sound for sound in (sounds or []))]:
                if name.startswith("*"):
                    continue
                try:
                    name = asset_name(name)
                    if name not in names and not store.exists(name):
                        names.append(name)
                except DownloadError:
                    continue
        except (DownloadError, OSError, ValueError, RuntimeError) as exc:
            self.output(f"Automatic downloads unavailable: {exc}")
            return False
        if not names:
            return False
        self._signon_pending = True
        self._launch(self._run(names, store, "auto", self._generation, signon=True))
        return True

    def _finished(self, task):
        self._tasks.discard(task)
        self._cancelling.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.output(f"Download stopped: {task.exception()}")

    async def _run(self, names, store, source, generation, *, signon=False):
        try:
            for name in names:
                try:
                    await self._fetch(name, store, source)
                except (DownloadError, OSError, ValueError, http.client.HTTPException) as exc:
                    self.output(f"Download failed for {name}: {exc}")
                if store.cleanup_warning is not None:
                    self.output(store.cleanup_warning)
                    store.cleanup_warning = None
        except asyncio.CancelledError:
            self._cancelling.add(asyncio.current_task())
            self.output("Download cancelled.")
        finally:
            if generation == self._generation:
                self.name = None
                if signon:
                    await self._resume_signon(generation)

    async def _resume_signon(self, generation):
        if generation == self._generation and self._signon_pending:
            self._signon_pending = False
            if self.client is not None and self.client._alive and not self.client.disconnected:
                await self.client.send_command("prespawn")

    async def _fetch(self, name, store, source):
        self._progress(name, source, 0, 0)
        maximum = int(self.cvars.get_value("cl_download_maxsize", 50)) * 1024 * 1024
        timeout = max(1, self.cvars.get_value("cl_download_timeout", 15))
        failures = []
        if source in ("auto", "web"):
            for key in ("cl_web_download_url", "cl_web_download_url2"):
                base = self.cvars.get_string(key)
                if not base:
                    continue
                try:
                    await self._web(name, store, mirror_url(base, name), maximum, timeout)
                    self.output(f"Downloaded {name} from web")
                    return
                except (OSError, ValueError, DownloadError, http.client.HTTPException) as exc:
                    failures.append(str(exc))
        if source in ("auto", "server") and self.client is not None and self.server_available:
            await self._server(name, store, maximum, timeout)
            self.output(f"Downloaded {name} from server")
            return
        raise DownloadError("; ".join(failures) or "no available download source")

    async def _web(self, name, store, url, maximum, timeout):
        cancelled = threading.Event()
        temporary = None
        loop = asyncio.get_running_loop()
        generation = self._generation

        def fetch():
            if cancelled.is_set():
                raise DownloadError("cancelled")
            output, path = store.stage(name)
            try:
                opener = urllib.request.build_opener(_HttpRedirect())
                request = urllib.request.Request(url, headers={"User-Agent": "PyQuake-M", "Accept-Encoding": "identity"})
                with output, opener.open(request, timeout=timeout) as response:
                    if response.status != 200:
                        raise DownloadError(f"unexpected HTTP status {response.status}")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise DownloadError("unsupported HTTP content encoding")
                    if response.headers.get_content_type() == "text/html":
                        raise DownloadError("mirror returned an HTML page")
                    length = response.headers.get("Content-Length")
                    total = int(length) if length is not None else 0
                    if total < 0 or total > maximum:
                        raise DownloadError("asset exceeds download size limit")
                    received = 0
                    while not cancelled.is_set():
                        data = response.read1(65536)
                        if not data:
                            break
                        received += len(data)
                        if received > maximum:
                            raise DownloadError("asset exceeds download size limit")
                        if output.write(data) != len(data):
                            raise OSError("short download write")
                        def progress(received=received, total=total):
                            if not cancelled.is_set() and generation == self._generation:
                                self._progress(name, "web", received, total)
                        loop.call_soon_threadsafe(progress)
                    if cancelled.is_set():
                        raise DownloadError("cancelled")
                    if total and received != total:
                        raise DownloadError("truncated HTTP response")
                    output.flush()
                    os.fsync(output.fileno())
                validate_content(name, path)
                return path
            except BaseException as exc:
                try:
                    if isinstance(exc, urllib.error.HTTPError):
                        exc.close()
                finally:
                    try:
                        output.close()
                    finally:
                        path.unlink(missing_ok=True)
                raise

        def fetch_result():
            # Return expected failures as values so cancelling a shielded HTTP
            # worker does not log its eventual error as an unhandled exception.
            try:
                return fetch(), None
            except (DownloadError, OSError, ValueError, http.client.HTTPException) as exc:
                return None, exc

        # An executor Future is not independently cancelled by all_tasks()
        # during shutdown; its owning job can wait for cleanup to finish.
        worker = loop.run_in_executor(None, fetch_result)
        try:
            temporary, error = await asyncio.shield(worker)
            if error is not None:
                raise error
            store.publish(name, temporary)
        except asyncio.CancelledError:
            self._cancelling.add(asyncio.current_task())
            cancelled.set()
            temporary, _ = await asyncio.shield(worker)
            raise
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    self.output(f"Could not remove partial download {temporary}: {exc}")

    async def _server(self, name, store, maximum, timeout):
        future = asyncio.get_running_loop().create_future()
        transfer = ServerTransfer(name, store, future, maximum, progress_at=time.monotonic(),
                                  ignore_stop=self._cancelled_server)
        self._cancelled_server = False
        self.server = transfer
        try:
            await self.client.send_command(f'download "{name}"')
            while not future.done():
                remaining = timeout - (time.monotonic() - transfer.progress_at)
                if remaining <= 0:
                    raise DownloadError("server download timed out")
                await asyncio.wait({future}, timeout=min(1, remaining))
            future.result()
        finally:
            if not future.done():
                if self.server is transfer:
                    self._cancelled_server = True
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Observe errors even if the owning task was cancelled.
            try:
                transfer.cleanup()
            except OSError as exc:
                self.output(f"Could not remove partial download {transfer.temporary}: {exc}")
            finally:
                if self.server is transfer:
                    self.server = None

    def _fail_server(self, message):
        transfer = self.server
        if transfer is not None and not transfer.future.done():
            transfer.future.set_exception(DownloadError(message))

    async def filter_stufftext(self, text):
        """Remove download commands while retaining quoted aliases and comments."""
        commands = []
        start = 0
        quoted = escaped = comment = False
        for index, char in enumerate(text):
            if char in "\r\n" or (char == ";" and not quoted and not comment):
                commands.append(text[start:index])
                start = index + 1
                quoted = escaped = comment = False
            elif comment:
                continue
            elif escaped:
                escaped = False
            elif char == "\\" and quoted:
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif (not quoted and text[index:index + 2] == "//" and
                  (index == start or text[index - 1].isspace())):
                comment = True
        commands.append(text[start:])
        retained, consumed = [], False
        for command in commands:
            if await self.command(command):
                consumed = True
            else:
                retained.append(command)
        if not consumed:
            return text, False
        retained = [command for command in retained if command.strip()]
        return "\n".join(retained) + ("\n" if retained else ""), True

    async def command(self, line):
        """Consume only recognized server download controls, never local commands."""
        try:
            tokens = shlex.split(line.strip().removeprefix("//"))
        except ValueError:
            return False
        if not tokens:
            return False
        command, args = tokens[0].lower(), tokens[1:]
        if command == "cl_serverextension_download":
            try:
                self.server_available = len(args) == 1 and int(args[0]) > 0
            except ValueError:
                self.server_available = False
            return True
        if command not in ("cl_downloadbegin", "cl_downloadfinished", "cl_downloadsequence", "stopdownload"):
            return False
        transfer = self.server
        if transfer is None or transfer.future.done():
            return True
        try:
            if command == "stopdownload":
                if transfer.ignore_stop and transfer.output is None:
                    transfer.ignore_stop = False
                else:
                    self._fail_server("server refused or stopped the download")
            elif command == "cl_downloadbegin":
                if len(args) != 2 or args[1] != transfer.name or transfer.output is not None:
                    raise DownloadError("unexpected download begin")
                size = int(args[0])
                if not 0 <= size <= transfer.maximum:
                    raise DownloadError("server asset exceeds download size limit")
                transfer.size = size
                transfer.output, transfer.temporary = transfer.store.stage(transfer.name)
                transfer.progress_at = time.monotonic()
                transfer.ignore_stop = False
                await self.client.send_command("sv_startdownload")
            elif command == "cl_downloadfinished":
                if len(args) not in (2, 3) or (len(args) == 3 and args[2] != transfer.name):
                    raise DownloadError("unexpected download finish")
                size, expected = int(args[0]), int(args[1])
                if transfer.output is None or size != transfer.size or transfer.received != size:
                    raise DownloadError("incomplete server download")
                transfer.output.flush()
                transfer.output.seek(0, os.SEEK_END)
                if transfer.output.tell() != size:
                    raise DownloadError("server download length mismatch")
                transfer.output.seek(0)
                crc = 0xffff
                while data := transfer.output.read(65536):
                    crc = binascii.crc_hqx(data, crc)
                if crc != expected:
                    raise DownloadError("server download CRC mismatch")
                os.fsync(transfer.output.fileno())
                transfer.output.close()
                validate_content(transfer.name, transfer.temporary)
                transfer.store.publish(transfer.name, transfer.temporary)
                transfer.future.set_result(True)
        except (ValueError, OSError, DownloadError) as exc:
            self._fail_server(str(exc))
        return True

    async def data(self, offset, data):
        transfer = self.server
        if transfer is None or transfer.future.done() or transfer.output is None:
            return
        try:
            if offset > transfer.size or len(data) > transfer.size - offset:
                raise DownloadError("server data outside the requested file")
            end = offset + len(data)
            if offset <= transfer.received and end > transfer.received:
                transfer.output.seek(transfer.received)
                pending = data[transfer.received - offset:]
                if transfer.output.write(pending) != len(pending):
                    raise OSError("short server download write")
                transfer.received = end
                transfer.progress_at = time.monotonic()
                self._progress(transfer.name, "server", end, transfer.size)
            # Acknowledging a gap makes QSS-M rewind to its last contiguous ACK.
            await self.client.send_reliable_raw(b"\x33" + struct.pack("<IH", offset, len(data)))
        except (OSError, DownloadError) as exc:
            self._fail_server(str(exc))

    def cancel(self, *, resume_signon=False):
        if not resume_signon:
            self._generation += 1
            self._signon_pending = False
        tasks = self._tasks | ({self.task} if self.task is not None else set())
        for task in tasks:
            if not task.done() and task not in self._cancelling:
                self._cancelling.add(task)
                task.cancel()
        if resume_signon and self._signon_pending:
            asyncio.create_task(self._resume_signon(self._generation))

    async def close(self):
        tasks = self._tasks | ({self.task} if self.task is not None else set())
        self.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
