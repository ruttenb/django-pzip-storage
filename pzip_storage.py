import os
import tempfile

import django.dispatch
import pzip
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import FileSystemStorage
from django.utils.encoding import force_bytes

__version__ = "1.0.0"
__version_info__ = tuple(int(num) for num in __version__.split("."))


needs_rotation = django.dispatch.Signal()
needs_encryption = django.dispatch.Signal()
bad_keys = django.dispatch.Signal()


class IntermediateFile:
    def __init__(self, path):
        self.path = path

    def temporary_file_path(self):
        return self.path


class PZipStorage(FileSystemStorage):
    DEFAULT_EXTENSION = ".pz"

    DEFAULT_NOCOMPRESS = set(
        [
            ".z",
            ".gz",
            ".zip",
            ".tgz",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".sit",
            ".sitx",
            ".7z",
            ".pz",
            ".bz2",
            ".xz",
        ]
    )

    def __init__(self, *args, **kwargs):
        self.keys = kwargs.pop(
            "keys", getattr(settings, "PZIP_STORAGE_KEYS", self.default_keys)
        )
        self.extension = kwargs.pop(
            "extension",
            getattr(settings, "PZIP_STORAGE_EXTENSION", self.DEFAULT_EXTENSION),
        )
        self.nocompress = kwargs.pop(
            "nocompress",
            getattr(settings, "PZIP_STORAGE_NOCOMPRESS", self.DEFAULT_NOCOMPRESS),
        )
        if not self.keys:
            raise ImproperlyConfigured("PZipStorage requires at least one key.")
        super().__init__(*args, **kwargs)

    def default_keys(self):
        yield force_bytes(settings.SECRET_KEY)

    def iter_keys(self):
        keys = self.keys() if callable(self.keys) else self.keys
        yield from keys

    def is_pzip(self, name):
        try:
            pzip.open(self.path(name)).close()
            return True
        except pzip.InvalidFile:
            return False

    def size(self, name):
        try:
            with pzip.open(self.path(name)) as f:
                return f.plaintext_size()
        except pzip.InvalidFile:
            return super().size(name)

    def _open(self, name, mode="rb"):
        if self.is_pzip(name):
            assert mode == "rb"  # TODO: support writable files here?
            for idx, key in enumerate(self.iter_keys()):
                try:
                    f = super()._open(name, mode)
                    return pzip.open(f, mode, key=key, peek=True)
                except pzip.InvalidFile:
                    # Close the underlying fileobj if PZip fails to decode.
                    f.close()
                finally:
                    if idx > 0:
                        # If we opened this file with an old key, broadcast a signal for
                        # callers to do rotation.
                        needs_rotation.send(
                            sender=self.__class__, storage=self, name=name, key=key
                        )
            # If we tried all the keys and haven't returned yet for a PZip file, send a
            # bad_keys signal.
            bad_keys.send(sender=self.__class__, storage=self, name=name)
        else:
            # Send needs_encryption signal for non-pzip files.
            needs_encryption.send(sender=self.__class__, storage=self, name=name)
        return super()._open(name, mode)

    def _save(self, name, content):
        try:
            # Sse the first (most recent) defined key for encryption.
            key = next(self.iter_keys())
        except StopIteration:
            raise ImproperlyConfigured("PZipStorage requires at least one key.")

        # Determine whether we should compress based on the original supplied file
        # extension.
        should_compress = os.path.splitext(name)[1].lower() not in self.nocompress

        # Create a temporary file to do the encryption/compression before handing off.
        fd, path = tempfile.mkstemp(
            suffix=self.extension, dir=settings.FILE_UPLOAD_TEMP_DIR
        )
        with pzip.open(fd, "wb", key=key, compress=should_compress) as f:
            for chunk in content.chunks():
                f.write(chunk)

        # FileSystemStorage will just move the file we wrote, so no need to clean up.
        return super()._save(name + self.extension, IntermediateFile(path))
