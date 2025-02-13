import os
import re

import upath.core


class _S3Accessor(upath.core._FSSpecAccessor):
    def __init__(self, parsed_url, *args, **kwargs):
        super().__init__(parsed_url, *args, **kwargs)

    def _format_path(self, s):
        """If the filesystem backend doesn't have a root_marker, strip the
        leading slash of a path and add the bucket
        """
        s = os.path.join(self._url.netloc, s.lstrip("/"))
        return s


class S3Path(upath.core.UPath):
    _default_accessor = _S3Accessor

    def _init(self, *args, template=None, **kwargs):
        # ensure that the bucket is part of the netloc path
        if kwargs.get("bucket") and kwargs.get("_url"):
            bucket = kwargs.pop("bucket")
            kwargs["_url"] = kwargs["_url"]._replace(netloc=bucket)

        super()._init(*args, template=template, **kwargs)

    def _sub_path(self, name):
        """s3fs returns path as `{bucket}/<path>` with listdir
        and glob, so here we can add the netloc to the sub string
        so it gets subbed out as well
        """
        sp = self.path
        subed = re.sub(f"^({self._url.netloc})?/?({sp}|{sp[1:]})/?", "", name)
        return subed

    def joinpath(self, *args):
        if self._url.netloc:
            return super().joinpath(*args)
        # handles a bucket in the path
        else:
            path = args[0]
            if isinstance(path, list):
                args_list = list(*args)
            else:
                args_list = path.split(self._flavour.sep)
            bucket = args_list.pop(0)
            self._kwargs["bucket"] = bucket
            return super().joinpath(*tuple(args_list))
