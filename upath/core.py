import pathlib
import re
from typing import IO, Iterator, List
import urllib

from fsspec.registry import (
    get_filesystem_class,
    known_implementations,
    registry,
)
from fsspec.utils import stringify_path

from upath.errors import NotDirectoryError


class _FSSpecAccessor:
    def __init__(self, parsed_url, *args, **kwargs):
        self._url = parsed_url
        cls = get_filesystem_class(self._url.scheme)
        url_kwargs = cls._get_kwargs_from_urls(
            urllib.parse.urlunparse(self._url)
        )
        url_kwargs.update(kwargs)
        self._fs = cls(**url_kwargs)

    def transform_args_wrapper(self, func):
        """Modifies the arguments that get passed to the filesystem so that
        the UPath instance gets stripped as the first argument. If a
        path keyword argument is not given, then `UPath.path` is
        formatted for the filesystem and inserted as the first argument.
        If it is, then the path keyword argument is formatted properly for
        the filesystem.
        """

        def wrapper(*args, **kwargs):
            args, kwargs = self._transform_arg_paths(args, kwargs)
            return func(*args, **kwargs)

        return wrapper

    def _transform_arg_paths(self, args, kwargs):
        """Formats the path properly for the filesystem backend."""
        args = list(args)
        first_arg = args.pop(0)
        if not kwargs.get("path"):
            if isinstance(first_arg, UPath):
                first_arg = self._format_path(first_arg.path)
                args.insert(0, first_arg)
            args = tuple(args)
        else:
            kwargs["path"] = self._format_path(kwargs["path"])
        return args, kwargs

    def _format_path(self, s):
        """Placeholder method for subclassed filesystems"""
        return s

    def __getattribute__(self, item):
        class_attrs = ["_url", "_fs", "__class__"]
        if item in class_attrs:
            return object.__getattribute__(self, item)

        class_methods = [
            "__init__",
            "__getattribute__",
            "transform_args_wrapper",
            "_transform_arg_paths",
            "_format_path",
        ]
        if item in class_methods:
            return object.__getattribute__(self, item)

        fs = object.__getattribute__(self, "_fs")
        if fs is not None:
            method = getattr(fs, item, None)
            if method:
                return lambda *args, **kwargs: (
                    self.transform_args_wrapper(method)(*args, **kwargs)
                )  # noqa: E501
            else:
                raise NotImplementedError(
                    f"{fs.protocol} filesystem has no attribute {item}"
                )


class UPath(pathlib.Path):

    _flavour = pathlib._posix_flavour
    __slots__ = ("_url", "_kwargs", "_closed", "fs")

    _default_accessor = _FSSpecAccessor

    def __new__(cls, *args, **kwargs):
        if issubclass(cls, UPath):
            args_list = list(args)
            url = args_list.pop(0)
            url = stringify_path(url)
            parsed_url = urllib.parse.urlparse(url)
            for key in ["scheme", "netloc"]:
                val = kwargs.get(key)
                if val:
                    parsed_url = parsed_url._replace(**{key: val})
            impls = list(registry) + list(known_implementations.keys())
            if parsed_url.scheme and parsed_url.scheme in impls:
                import upath.registry

                cls = upath.registry._registry[parsed_url.scheme]
                kwargs["_url"] = parsed_url
                args_list.insert(0, parsed_url.path)
                args = tuple(args_list)
                self = cls._from_parts_init(args, init=False)
                self._init(*args, **kwargs)
                return self

        # treat as local filesystem, return PosixPath or WindowsPath
        return pathlib.Path(*args, **kwargs)

    def _init(self, *args, template=None, **kwargs):
        self._closed = False
        if not kwargs:
            kwargs = dict(**self._kwargs)
        else:
            self._kwargs = dict(**kwargs)
        self._url = kwargs.pop("_url") if kwargs.get("_url") else None

        if not self._root:
            if not self._parts:
                self._root = "/"
            elif self._parts[0] == "/":
                self._root = self._parts.pop(0)
        if getattr(self, "_str", None):
            delattr(self, "_str")
        if template is not None:
            self._accessor = template._accessor
        else:
            self._accessor = self._default_accessor(self._url, *args, **kwargs)
        self.fs = self._accessor._fs

    def _format_parsed_parts(self, drv: str, root: str, parts: List[str]):
        if parts:
            join_parts = parts[1:] if parts[0] == "/" else parts
        else:
            join_parts = []
        if drv or root:
            path = drv + root + self._flavour.join(join_parts)
        else:
            path = self._flavour.join(join_parts)
        scheme, netloc = self._url.scheme, self._url.netloc
        scheme = scheme + ":"
        netloc = "//" + netloc if netloc else ""
        formatted = scheme + netloc + path
        return formatted

    @property
    def path(self) -> str:
        if self._parts:
            join_parts = (
                self._parts[1:] if self._parts[0] == "/" else self._parts
            )
            path = self._flavour.join(join_parts)
            return self._root + path
        else:
            return "/"

    def open(self, *args, **kwargs) -> IO:
        return self._accessor.open(self, *args, **kwargs)

    def iterdir(self) -> Iterator["UPath"]:
        """Iterate over the files in this directory.  Does not yield any
        result for the special paths '.' and '..'.
        """
        if self._closed:
            self._raise_closed()
        for name in self._accessor.listdir(self):
            # fsspec returns dictionaries
            if isinstance(name, dict):
                name = name.get("name")
            if name in {".", ".."}:
                # Yielding a path object for these makes little sense
                continue
            # only want the path name with iterdir
            name = self._sub_path(name)
            yield self._make_child_relpath(name)
            if self._closed:
                self._raise_closed()

    def glob(self, pattern: str) -> Iterator["UPath"]:
        path = self.joinpath(pattern)
        for name in self._accessor.glob(self, path=path.path):
            name = self._sub_path(name)
            name = name.split(self._flavour.sep)
            yield self._make_child(name)

    def rglob(self, pattern: str) -> Iterator["UPath"]:
        # Not supported by fsspec
        raise NotImplementedError

    def _sub_path(self, name):
        # only want the path name with iterdir
        sp = self.path
        return re.sub(f"^({sp}|{sp[1:]})/", "", name)

    def exists(self) -> bool:
        """
        Whether this path exists.
        """
        if not getattr(self._accessor, "exists"):
            try:
                self._accessor.stat(self)
            except (FileNotFoundError):
                return False
            return True
        else:
            return self._accessor.exists(self)

    def is_dir(self) -> bool:
        info = self._accessor.info(self)
        if info["type"] == "directory":
            return True
        return False

    def is_file(self) -> bool:
        info = self._accessor.info(self)
        if info["type"] == "file":
            return True
        return False

    def chmod(self, mode) -> None:
        raise NotImplementedError

    def lchmod(self, mode) -> None:
        raise NotImplementedError

    def symlink_to(self, target, target_is_directory=False) -> None:
        raise NotImplementedError

    def link_to(self, target, target_is_directory=False) -> None:
        raise NotImplementedError

    def rename(self, target) -> None:
        # can be implemented, but may be tricky
        raise NotImplementedError

    def replace(self, target) -> None:
        raise NotImplementedError

    def touch(self, trunicate=True, **kwargs) -> None:
        self._accessor.touch(self, trunicate=trunicate, **kwargs)

    def unlink(self, missing_ok=False) -> None:
        if not self.exists():
            if not missing_ok:
                raise FileNotFoundError
            else:
                return
        self._accessor.rm(self, recursive=False)

    def rmdir(self, recursive=True) -> None:
        """Add warning if directory not empty
        assert is_dir?
        """
        if not self.is_dir():
            raise NotDirectoryError
        self._accessor.rm(self, recursive=recursive)

    @classmethod
    def cwd(cls) -> "UPath":
        raise NotImplementedError

    @classmethod
    def home(cls) -> "UPath":
        raise NotImplementedError

    def expanduser(self) -> "UPath":
        raise NotImplementedError

    def owner(self) -> str:
        raise NotImplementedError

    def group(self) -> str:
        raise NotImplementedError

    def readlink(self) -> "UPath":
        return self

    def stat(self):
        return self._accessor.stat(self)

    def lstat(self):
        return self.stat()

    def is_mount(self) -> bool:
        return False

    def is_symlink(self) -> bool:
        return False

    def is_socket(self) -> bool:
        return False

    def is_fifo(self) -> bool:
        return False

    def is_block_device(self) -> bool:
        return False

    def is_char_device(self) -> bool:
        return False

    def samefile(self, other_path: "UPath") -> bool:
        return self.stat() == other_path.stat()

    @classmethod
    def _from_parts_init(cls, args, init=False):
        return super()._from_parts(args, init=init)

    def _from_parts(self, args, init=True):
        # We need to call _parse_args on the instance, so as to get the
        # right flavour.
        obj = object.__new__(self.__class__)
        drv, root, parts = self._parse_args(args)
        obj._drv = drv
        obj._root = root
        obj._parts = parts
        if init:
            obj._init(**self._kwargs)
        return obj

    def _from_parsed_parts(self, drv, root, parts, init=True):
        obj = object.__new__(self.__class__)
        obj._drv = drv
        obj._root = root
        obj._parts = parts
        if init:
            obj._init(**self._kwargs)
        return obj

    def __truediv__(self, key):
        # Add `/` root if not present
        if len(self._parts) == 0:
            key = f"{self._root}{key}"

        # Adapted from `PurePath._make_child`
        drv, root, parts = self._parse_args((key,))
        drv, root, parts = self._flavour.join_parsed_parts(
            self._drv, self._root, self._parts, drv, root, parts
        )

        kwargs = self._kwargs.copy()
        kwargs.pop("_url")

        # Create a new object
        out = self.__class__(
            self._format_parsed_parts(drv, root, parts),
            **kwargs,
        )
        return out

    def __setstate__(self, state):
        kwargs = state["_kwargs"].copy()
        kwargs["_url"] = self._url
        self._kwargs = kwargs
        # _init needs to be called again, because when __new__ called _init,
        # the _kwargs were not yet set
        self._init()

    def __reduce__(self):
        kwargs = self._kwargs.copy()
        kwargs.pop("_url", None)

        return (
            self.__class__,
            (self._format_parsed_parts(self._drv, self._root, self._parts),),
            {"_kwargs": kwargs},
        )
