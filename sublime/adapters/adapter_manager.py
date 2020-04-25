import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Generic,
    Optional,
    Sequence,
    Set,
    Type,
    TypeVar,
    Union,
)

from sublime.config import AppConfiguration

from .adapter_base import Adapter, CacheMissError, CachingAdapter
from .api_objects import Playlist, PlaylistDetails
from .filesystem import FilesystemAdapter
from .subsonic import SubsonicAdapter

T = TypeVar("T")


class Result(Generic[T]):
    """
    A result from a :class:`AdapterManager` function. This is effectively a wrapper
    around a :class:`concurrent.futures.Future`, but it resolves immediately if the data
    already exists.
    """

    _data: Optional[T] = None
    _future: Optional[Future] = None
    on_cancel: Optional[Callable[[], None]] = None

    def __init__(self, data_resolver: Union[T, Callable[[], T]]):
        """
        Creates a :class:`Result` object.

        :param data_resolver: the actual data, or a function that will return the actual
            data. If the latter, the function will be executed by the thread pool.
        """
        if callable(data_resolver):
            self._future = AdapterManager.executor.submit(data_resolver)
            self._future.add_done_callback(self._on_future_complete)
        else:
            self._data = data_resolver

    def _on_future_complete(self, future: Future):
        self._data = future.result()

    def result(self) -> T:
        """
        Retrieve the actual data. If the data exists already, then return it, otherwise,
        blocking-wait on the future's result.
        """
        if self._data is not None:
            return self._data
        if self._future is not None:
            return self._future.result()

        raise Exception("AdapterManager.Result had neither _data nor _future member!")

    def add_done_callback(self, fn: Callable, *args):
        """Attaches the callable ``fn`` to the future."""
        if self._future is not None:
            self._future.add_done_callback(fn, *args)
        else:
            # Run the function immediately if it's not a future.
            fn(self, *args)

    def cancel(self) -> bool:
        """Cancel the future, or do nothing if the data already exists."""
        if self._future is not None:
            return self._future.cancel()
        return True

    @property
    def data_is_available(self) -> bool:
        """
        Whether or not the data is available at the current moment. This can be used to
        determine whether or not the UI needs to put the callback into a
        :class:`GLib.idle_add` call.
        """
        return self._data is not None


class AdapterManager:
    available_adapters: Set[Any] = {FilesystemAdapter, SubsonicAdapter}
    executor: ThreadPoolExecutor = ThreadPoolExecutor()
    is_shutting_down: bool = False

    @dataclass
    class _AdapterManagerInternal:
        ground_truth_adapter: Adapter
        caching_adapter: Optional[CachingAdapter] = None

        def shutdown(self):
            self.ground_truth_adapter.shutdown()
            if self.caching_adapter:
                self.caching_adapter.shutdown()

    _instance: Optional[_AdapterManagerInternal] = None

    @staticmethod
    def register_adapter(adapter_class: Type):
        if not issubclass(adapter_class, Adapter):
            raise TypeError("Attempting to register a class that is not an adapter.")
        AdapterManager.available_adapters.add(adapter_class)

    def __init__(self):
        """
        This should not ever be called. You should only ever use the static methods on
        this class.
        """
        raise Exception(
            "Do not instantiate the AdapterManager. "
            "Only use the static methods on the class."
        )

    @staticmethod
    def shutdown():
        logging.info("AdapterManager shutdown start")
        AdapterManager.is_shutting_down = True
        AdapterManager.executor.shutdown()
        if AdapterManager._instance:
            AdapterManager._instance.shutdown()

        logging.info("AdapterManager shutdown complete")

    @staticmethod
    def reset(config: AppConfiguration):
        # First, shutdown the current one...
        if AdapterManager._instance:
            AdapterManager._instance.shutdown()

        # TODO: actually do stuff with the config to determine which adapters
        # to create, etc.
        assert config.server is not None
        source_data_dir = Path(config.cache_location, config.server.strhash())
        source_data_dir.joinpath("g").mkdir(parents=True, exist_ok=True)
        source_data_dir.joinpath("c").mkdir(parents=True, exist_ok=True)

        ground_truth_adapter_type = SubsonicAdapter
        ground_truth_adapter = ground_truth_adapter_type(
            {
                key: getattr(config.server, key)
                for key in ground_truth_adapter_type.get_config_parameters()
            },
            source_data_dir.joinpath("g"),
        )

        caching_adapter_type = FilesystemAdapter
        caching_adapter = None
        if caching_adapter_type and ground_truth_adapter_type.can_be_cached:
            caching_adapter = caching_adapter_type(
                {
                    key: getattr(config.server, key)
                    for key in caching_adapter_type.get_config_parameters()
                },
                source_data_dir.joinpath("c"),
                is_cache=True,
            )

        AdapterManager._instance = AdapterManager._AdapterManagerInternal(
            ground_truth_adapter, caching_adapter=caching_adapter,
        )

    @staticmethod
    def _can_do(action_name: str):
        # It only matters that the ground truth one can service the request.
        return (
            AdapterManager._instance is not None
            and AdapterManager._instance.ground_truth_adapter.can_service_requests
            and getattr(
                AdapterManager._instance.ground_truth_adapter, f"can_{action_name}"
            )
        )

    @staticmethod
    def can_get_playlists() -> bool:
        return AdapterManager._can_do("get_playlists")

    @staticmethod
    def get_playlists(
        before_download: Callable[[], None] = lambda: None,
        force: bool = False,  # TODO: rename to use_ground_truth_adapter?
    ) -> Result[Sequence[Playlist]]:
        assert AdapterManager._instance
        if (
            not force
            and AdapterManager._instance.caching_adapter
            and AdapterManager._instance.caching_adapter.can_service_requests
            and AdapterManager._instance.caching_adapter.can_get_playlists
        ):
            try:
                return Result(AdapterManager._instance.caching_adapter.get_playlists())
            except CacheMissError:
                logging.debug(f'Cache Miss on {"get_playlists"}.')
            except Exception:
                logging.exception(f'Error on {"get_playlists"} retrieving from cache.')

        if (
            AdapterManager._instance.ground_truth_adapter
            and not AdapterManager._instance.ground_truth_adapter.can_service_requests
            and not AdapterManager._instance.ground_truth_adapter.can_get_playlists
        ):
            raise Exception(f'No adapters can service {"get_playlists"} at the moment.')

        def future_fn() -> Sequence[Playlist]:
            assert AdapterManager._instance
            if before_download:
                before_download()
            return AdapterManager._instance.ground_truth_adapter.get_playlists()

        future: Result[Sequence[Playlist]] = Result(future_fn)

        if AdapterManager._instance.caching_adapter:

            def future_finished(f: Future):
                assert AdapterManager._instance
                assert AdapterManager._instance.caching_adapter
                AdapterManager._instance.caching_adapter.ingest_new_data(
                    CachingAdapter.FunctionNames.GET_PLAYLISTS, (), f.result(),
                )

            future.add_done_callback(future_finished)

        return future

    @staticmethod
    def can_get_playlist_details() -> bool:
        return AdapterManager._can_do("get_playlist_details")

    @staticmethod
    def get_playlist_details(
        playlist_id: str,
        before_download: Callable[[], None] = lambda: None,
        force: bool = False,  # TODO: rename to use_ground_truth_adapter?
    ) -> Result[PlaylistDetails]:
        assert AdapterManager._instance
        partial_playlist_data = None
        if (
            not force
            and AdapterManager._instance.caching_adapter
            and AdapterManager._instance.caching_adapter.can_service_requests
            and AdapterManager._instance.caching_adapter.can_get_playlist_details
        ):
            try:
                return Result(
                    AdapterManager._instance.caching_adapter.get_playlist_details(
                        playlist_id
                    )
                )
            except CacheMissError as e:
                partial_playlist_data = e.partial_data
                logging.debug(f'Cache Miss on {"get_playlist_details"}.')
            except Exception:
                logging.exception(
                    f'Error on {"get_playlist_details"} retrieving from cache.'
                )

        if (
            AdapterManager._instance.ground_truth_adapter
            and not AdapterManager._instance.ground_truth_adapter.can_service_requests
            and not (
                AdapterManager._instance.ground_truth_adapter.can_get_playlist_details
            )
        ):
            if partial_playlist_data:
                # TODO do something here
                pass
            raise Exception(
                f'No adapters can service {"get_playlist_details"} at the moment.'
            )

        def future_fn() -> PlaylistDetails:
            assert AdapterManager._instance
            if before_download:
                before_download()
            return AdapterManager._instance.ground_truth_adapter.get_playlist_details(
                playlist_id
            )

        future: Result[PlaylistDetails] = Result(future_fn)

        if AdapterManager._instance.caching_adapter:

            def future_finished(f: Future):
                assert AdapterManager._instance
                assert AdapterManager._instance.caching_adapter
                AdapterManager._instance.caching_adapter.ingest_new_data(
                    CachingAdapter.FunctionNames.GET_PLAYLIST_DETAILS,
                    (playlist_id,),
                    f.result(),
                )

            future.add_done_callback(future_finished)

        return future
