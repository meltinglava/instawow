
from __future__ import annotations

__all__ = ('Manager', 'CliManager', 'WsManager')

import asyncio
import contextvars
from functools import partial
import io
from pathlib import Path
from typing import TYPE_CHECKING
from typing import (Any, Awaitable, Callable, Coroutine, Dict, Iterable, List,
                    NoReturn, Optional, Sequence, Tuple, Type, TypeVar, Union)

from loguru import logger
from send2trash import send2trash
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from . import __db_version__
from .config import Config
from . import exceptions as E
from .models import ModelBase, Pkg, PkgFolder, should_migrate
from .resolvers import *

if TYPE_CHECKING:
    import aiohttp


_UA_STRING = 'instawow (https://github.com/layday/instawow)'

_client = contextvars.ContextVar('_client')


def _prepare_db_session(*, config: Config) -> sessionmaker:
    url = f"sqlite:///{config.config_dir / 'db.sqlite'}"
    engine = create_engine(url)
    ModelBase.metadata.create_all(engine)

    # Attempt to extract the database version without the aid of Alembic
    # to save (processing) time
    if should_migrate(engine, __db_version__):
        from .migrations import make_config, upgrade
        alembic_config = make_config(url)
        logger.info(f'migrating database to {__db_version__}')
        upgrade(alembic_config, __db_version__)

    return sessionmaker(bind=engine)


async def _init_web_client(*, loop: asyncio.AbstractEventLoop,
                           **kwargs) -> aiohttp.ClientSession:
    from aiohttp import ClientSession, TCPConnector
    return ClientSession(loop=loop,
                         connector=TCPConnector(loop=loop, limit_per_host=10),
                         headers={'User-Agent': _UA_STRING},
                         **kwargs)


def trash(paths: Iterable[Path]) -> None:
    for path in paths:
        send2trash(str(path))


class PkgArchive:

    def __init__(self, payload: bytes) -> None:
        from zipfile import ZipFile

        self.archive = ZipFile(io.BytesIO(payload))
        self.root_folders = {Path(p).parts[0] for p in self.archive.namelist()}

    def extract(self, parent_folder: Path, *,
                overwrite: bool=False) -> None:
        "Extract the archive contents under ``parent_folder``."
        if not overwrite:
            conflicts = self.root_folders \
                        & {f.name for f in parent_folder.iterdir()}
            if conflicts:
                raise E.PkgConflictsWithPreexisting(conflicts)
        self.archive.extractall(parent_folder)


class MemberDict(dict):

    def __missing__(self, key: str) -> NoReturn:
        raise E.PkgOriginInvalid


class Manager:

    def __init__(self, *,
                 config: Config, loop: Optional[asyncio.AbstractEventLoop] = None,
                 client_factory: Optional[Callable] = None) -> None:
        self.config = config
        self.loop = loop or asyncio.get_event_loop()
        self.client_factory = partial(client_factory or _init_web_client,
                                      loop=self.loop)
        self.client = _client
        self.resolvers = MemberDict((r.origin, r(manager=self))
                                    for r in (CurseResolver, WowiResolver,
                                              TukuiResolver, InstawowResolver))
        self.db = _prepare_db_session(config=self.config)()

    async def _download_file(self, url: str) -> bytes:
        if url.startswith('file://'):
            from urllib.parse import unquote

            file = Path(unquote(url[7:]))
            return await self.loop.run_in_executor(None,
                                                   lambda: file.read_bytes())
        async with self.client.get()\
                              .get(url) as response:
            return await response.read()

    def get(self, origin: str, id_or_slug: str) -> Pkg:
        "Retrieve a ``Pkg`` from the database."
        return (self.db.query(Pkg)
                .filter(Pkg.origin == origin,
                        (Pkg.id == id_or_slug) | (Pkg.slug == id_or_slug))
                .first())

    async def resolve(self, origin: str, id_or_slug: str, strategy: str) -> Pkg:
        "Resolve an ID or slug into a ``Pkg``."
        return await (self.resolvers[origin]
                      .resolve(id_or_slug, strategy=strategy))

    async def to_install(self, origin: str, id_or_slug: str,
                         strategy: str, overwrite: bool) -> Callable[[], E.PkgInstalled]:
        "Retrieve a package to install."
        async def install():
            archive = PkgArchive(payload)
            pkg.folders = [PkgFolder(path=self.config.addon_dir / f)
                           for f in archive.root_folders]

            conflicts = (self.db.query(PkgFolder)
                         .filter(PkgFolder.path.in_([f.path for f in pkg.folders]))
                         .first())
            if conflicts:
                raise E.PkgConflictsWithInstalled(conflicts.pkg)

            if overwrite:
                trash_ = partial(trash, (f.path for f in pkg.folders
                                         if f.path.exists()))
                await self.loop.run_in_executor(None, trash_)
            extract = partial(archive.extract, self.config.addon_dir,
                              overwrite=overwrite)
            await self.loop.run_in_executor(None, extract)
            self.db.add(pkg)
            self.db.commit()
            return E.PkgInstalled(pkg)

        if self.get(origin, id_or_slug):
            raise E.PkgAlreadyInstalled
        pkg = await self.resolve(origin, id_or_slug, strategy)

        payload = await self._download_file(pkg.download_url)
        return install

    async def to_update(self, origin: str, id_or_slug: str) -> Callable[[], E.PkgUpdated]:
        "Retrieve a package to update."
        async def update():
            archive = PkgArchive(payload)
            new_pkg.folders = [PkgFolder(path=self.config.addon_dir / f)
                               for f in archive.root_folders]

            conflicts = (self.db.query(PkgFolder)
                         .filter(PkgFolder.path.in_([f.path for f in new_pkg.folders]),
                                 PkgFolder.pkg_origin != new_pkg.origin,
                                 PkgFolder.pkg_id != new_pkg.id)
                         .first())
            if conflicts:
                raise E.PkgConflictsWithInstalled(conflicts.pkg)

            try:
                trash_ = partial(trash, (f.path for f in cur_pkg.folders))
                await self.loop.run_in_executor(None, trash_)
                self.db.delete(cur_pkg)
                extract = partial(archive.extract,
                                  parent_folder=self.config.addon_dir)
                await self.loop.run_in_executor(None, extract)
                self.db.add(new_pkg)
            finally:
                self.db.commit()
            return E.PkgUpdated(cur_pkg, new_pkg)

        cur_pkg = self.get(origin, id_or_slug)
        if not cur_pkg:
            raise E.PkgNotInstalled
        new_pkg = await self.resolve(origin, id_or_slug, cur_pkg.options.strategy)
        if cur_pkg.file_id == new_pkg.file_id:
            raise E.PkgUpToDate

        payload = await self._download_file(new_pkg.download_url)
        return update

    async def remove(self, origin: str, id_or_slug: str) -> E.PkgRemoved:
        "Remove a package."
        pkg = self.get(origin, id_or_slug)
        if not pkg:
            raise E.PkgNotInstalled

        trash_ = partial(trash, (f.path for f in pkg.folders))
        await self.loop.run_in_executor(None, trash_)
        self.db.delete(pkg)
        self.db.commit()
        return E.PkgRemoved(pkg)


class Bar:

    def __init__(self, *args, **kwargs) -> None:
        kwargs['position'], self.__reset_position = kwargs['position']
        super().__init__(*args, **{'leave': False, 'ascii': True, **kwargs})    # type: ignore

    def close(self) -> None:
        super().close()     # type: ignore
        self.__reset_position()


async def _init_cli_web_client(*, loop: asyncio.AbstractEventLoop,
                               manager: CliManager) -> aiohttp.ClientSession:
    from cgi import parse_header
    from aiohttp import TraceConfig

    async def do_on_request_end(_session, _ctx,
                                params: aiohttp.TraceRequestEndParams) -> None:
        if params.response.content_type in {
                'application/zip',
                # Curse at it again
                'application/x-amz-json-1.0'}:
            cd = params.response.headers.get('Content-Disposition', '')
            _, cd_params = parse_header(cd)
            filename = cd_params.get('filename') or params.response.url.name

            bar = manager.Bar(total=params.response.content_length,
                              desc=f'  Downloading {filename}',
                              miniters=1, unit='B', unit_scale=True,
                              position=manager.bar_position)

            async def ticker(bar=bar, params=params) -> None:
                while not params.response.content._eof:
                    bar.update(params.response.content._cursor - bar.n)
                    await asyncio.sleep(bar.mininterval)
                bar.close()
            loop.create_task(ticker())

    trace_config = TraceConfig()
    trace_config.on_request_end.append(do_on_request_end)
    trace_config.freeze()
    return await _init_web_client(loop=loop, trace_configs=[trace_config])


class SafeFuture(asyncio.Future):

    def result(self) -> object:
        return self.exception() or super().result()

    async def intercept(self, awaitable: Awaitable) -> SafeFuture:
        try:
            self.set_result(await awaitable)
        except E.ManagerError as error:
            self.set_exception(error)
        except Exception as error:
            logger.exception('internal error')
            self.set_exception(E.InternalError(error=error))
        return self


class CliManager(Manager):

    def __init__(self, *,
                 config: Config, loop: Optional[asyncio.AbstractEventLoop] = None,
                 show_progress: bool = True) -> None:
        super().__init__(config=config, loop=loop,
                         client_factory=(partial(_init_cli_web_client, manager=self)
                                         if show_progress else None))
        self.show_progress = show_progress
        self.bar_positions = [False]

        from tqdm import tqdm
        self.Bar = type('Bar', (Bar, tqdm), {})

    @property
    def bar_position(self) -> Tuple[int, Callable]:
        "Get the first available bar slot."
        try:
            b = self.bar_positions.index(False)
            self.bar_positions[b] = True
        except ValueError:
            b = len(self.bar_positions)
            self.bar_positions.append(True)
        return (b, lambda b=b: self.bar_positions.__setitem__(b, False))

    def run(self, awaitable: Awaitable) -> Any:
        "Run ``awaitable`` inside an explicit context."
        async def runner():
            async with (await self.client_factory()) as client:
                _client.set(client)
                return await awaitable

        context = contextvars.copy_context()
        return context.run(lambda: self.loop.run_until_complete(runner()))

    async def gather(self, it: Iterable, *, desc: Optional[str] = None) -> List[SafeFuture]:
        async def intercept(coro: Coroutine, index: int, bar: Any
                            ) -> Tuple[int, SafeFuture]:
            future = await SafeFuture(loop=self.loop).intercept(coro)
            bar.update(1)
            return index, future

        coros = list(it)
        with self.Bar(total=len(coros), disable=not self.show_progress,
                      position=self.bar_position, desc=desc) as bar:
            futures = [intercept(c, i, bar) for i, c in enumerate(coros)]
            results = [v for _, v in
                       sorted([await r for r in
                               asyncio.as_completed(futures, loop=self.loop)])]

            # Wait for ``ticker``s to complete so all bars get to wipe
            # their pretty faces off the face of the screen
            while len(asyncio.all_tasks(self.loop)) > 1:
                await asyncio.sleep(bar.mininterval)
        return results

    def resolve_many(self, values: Iterable) -> List[Union[E.ManagerResult, Pkg]]:
        async def resolve_many():
            return [r.result() for r in
                    (await self.gather((self.resolve(*a) for a in values),
                                       desc='Resolving'))]

        return self.run(resolve_many())

    def install_many(self, values: Iterable) -> List[E.ManagerResult]:
        async def install_many():
            return [(r if r.exception() else
                     await SafeFuture(loop=self.loop).intercept(r.result()())
                     ).result()
                    for r in (await self.gather((self.to_install(*a) for a in values),
                                                desc='Fetching'))]

        return self.run(install_many())

    def update_many(self, values: Iterable) -> List[E.ManagerResult]:
        async def update_many():
            return [(r if r.exception() else
                     await SafeFuture(loop=self.loop).intercept(r.result()())
                     ).result()
                    for r in (await self.gather((self.to_update(*a) for a in values),
                                                desc='Checking'))]

        return self.run(update_many())


class WsManager(Manager):

    async def poll(self, web_request: aiohttp.web.Request) -> None:
        import aiohttp
        from .api import (ErrorCodes, ApiError,
                          Request, InstallRequest, UpdateRequest, RemoveRequest,
                          SuccessResponse, ErrorResponse,
                          jsonify, parse_request)

        TR = TypeVar('TR', bound=Request)

        async def respond(request: TR, awaitable: Awaitable) -> None:
            try:
                result = await awaitable
            except ApiError as error:
                response = ErrorResponse.from_api_error(error)
            except E.ManagerError as error:
                values = {'id': request.id,
                          'error': {'code': ErrorCodes[type(error).__name__],
                                    'message': error.message}}
                response = ErrorResponse(**values)
            except Exception:
                logger.exception('internal error')
                values = {'id': request.id,
                          'error': {'code': ErrorCodes.INTERNAL_ERROR,
                                    'message': 'encountered an internal error'}}
                response = ErrorResponse(**values)
            else:
                response = request.consume_result(result)
            await websocket.send_json(response, dumps=jsonify)

        async def receiver() -> None:
            async for message in websocket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        request = parse_request(message.data)
                    except ApiError as error:
                        async def schedule_error(error=error) -> NoReturn:
                            raise error
                        self.loop.create_task(respond(None, schedule_error()))  # type: ignore
                    else:
                        if request.__class__ in {InstallRequest, UpdateRequest, RemoveRequest}:
                            # Here we're wrapping the coroutine in a future
                            # that the consumer will `wait_for` to complete therefore
                            # preserving the order of (would-be) synchronous operations
                            future = self.loop.create_future()
                            consumer_queue.put_nowait((request, future))

                            async def schedule(request=request, future=future) -> None:
                                try:
                                    future.set_result(await request.prepare_response(self))
                                except Exception as error:
                                    future.set_exception(error)
                            self.loop.create_task(schedule())
                        else:
                            self.loop.create_task(respond(request,
                                                          request.prepare_response(self)))

        async def consumer() -> None:
            while True:
                request, future = await consumer_queue.get()

                async def consume(request=request, future=future) -> Any:
                    result = await asyncio.wait_for(future, None)
                    if request.__class__ in {InstallRequest, UpdateRequest}:
                        result = await result()
                    return result
                await respond(request, consume())
                consumer_queue.task_done()

        consumer_queue: asyncio.Queue[Tuple[Request,
                                            asyncio.Future]] = asyncio.Queue()
        websocket = aiohttp.web.WebSocketResponse()

        await websocket.prepare(web_request)
        self.loop.create_task(consumer())

        async with (await self.client_factory()) as client:
            _client.set(client)
            await receiver()

    def serve(self, host: str = '127.0.0.1', port: Optional[int] = None) -> None:
        async def runner() -> None:
            import os
            import socket
            from aiohttp import web

            app = web.Application()
            app.router.add_routes([web.get('/', self.poll)])    # type: ignore
            app_runner = web.AppRunner(app)
            await app_runner.setup()

            server = await self.loop.create_server(app_runner.server, host, port,
                                                   family=socket.AF_INET)
            sock = server.sockets[0]
            message = ('{{"address": "ws://{}:{}/"}}\n'
                       .format(*sock.getsockname()).encode())
            try:
                # Try sending message over fd 3 for IPC with Node
                # and if that fails...
                os.write(3, message)
            except OSError:
                # ... write to stdout
                os.write(1, message)

            try:
                await server.serve_forever()
            except (KeyboardInterrupt, SystemExit):
                pass
            finally:
                await app_runner.cleanup()

        context = contextvars.copy_context()
        context.run(lambda: self.loop.run_until_complete(runner()))
