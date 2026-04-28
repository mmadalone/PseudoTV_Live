# -*- coding: utf-8 -*-
# Copyright: (c) 2019, Dag Wieers (@dagwieers) <dag@wieers.com>
# GNU General Public License v3.0 (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""This file implements the Kodi xbmcvfs module, either using stubs or alternative functionality"""

# pylint: disable=invalid-name,too-few-public-methods

import os
from shutil import copyfile


class _KodiFile:
    """Thin wrapper that maps Kodi xbmcvfs.File semantics onto a Python file object.

    Kodi's xbmcvfs.File.read([bytes]) interprets bytes=0 (and omitted) as "read
    all"; Python's open().read(0) reads zero bytes. Without this wrapper, addon
    code that calls fle.read() (default bytes=0) gets an empty string and the
    caller fails downstream (e.g. getJSON returns {}).
    """

    def __init__(self, f):
        self._f = f

    def read(self, num_bytes=0):
        return self._f.read(-1 if num_bytes <= 0 else num_bytes)

    def readBytes(self, num_bytes=0):
        return self._f.read(-1 if num_bytes <= 0 else num_bytes)

    def write(self, data):
        return self._f.write(data)

    def close(self):
        return self._f.close()

    def seek(self, *args):
        return self._f.seek(*args)

    def tell(self):
        return self._f.tell()

    def __getattr__(self, name):
        return getattr(self._f, name)


def File(path, flags='r'):
    """A reimplementation of the xbmcvfs File() function.

    Kodi's real xbmcvfs.File() auto-resolves special:// paths internally; mirror
    that here so test imports that read addon-data files succeed when the stub
    payload is staged under tests/userdata/. Returns a _KodiFile wrapper so
    read(0)-means-all matches Kodi's behaviour, not Python's.
    """
    assert isinstance(path, str)
    assert isinstance(flags, str)
    return _KodiFile(open(translatePath(path), flags))  # pylint: disable=consider-using-with


def Stat(path):
    """A reimplementation of the xbmcvfs Stat() function"""

    class stat:
        """A reimplementation of the xbmcvfs stat class"""

        def __init__(self, path):
            """The constructor xbmcvfs stat class"""
            assert isinstance(path, str)
            self._stat = os.stat(path)

        def st_mtime(self):
            """The xbmcvfs stat class st_mtime method"""
            return self._stat.st_mtime

    return stat(path)


def copy(src, dst):
    """A reimplementation of the xbmcvfs mkdir() function"""
    assert isinstance(src, str)
    assert isinstance(dst, str)
    return copyfile(src, dst) == dst


def delete(path):
    """A reimplementation of the xbmcvfs delete() function"""
    assert isinstance(path, str)
    try:
        os.remove(path)
    except OSError:
        pass


def exists(path):
    """A reimplementation of the xbmcvfs exists() function"""
    assert isinstance(path, str)
    return os.path.exists(path)


def listdir(path):
    """A reimplementation of the xbmcvfs listdir() function"""
    assert isinstance(path, str)
    files = []
    dirs = []
    if not exists(path):
        return dirs, files
    for filename in os.listdir(path):
        fullname = os.path.join(path, filename)
        if os.path.isfile(fullname):
            files.append(filename)
        if os.path.isdir(fullname):
            dirs.append(filename)
    return dirs, files


def mkdir(path):
    """A reimplementation of the xbmcvfs mkdir() function"""
    assert isinstance(path, str)
    return os.mkdir(path)


def mkdirs(path):
    """A reimplementation of the xbmcvfs mkdirs() function"""
    assert isinstance(path, str)
    return os.makedirs(path)


def rmdir(path):
    """A reimplementation of the xbmcvfs rmdir() function"""
    assert isinstance(path, str)
    return os.rmdir(path)


def translatePath(path):
    """A stub implementation of the xbmc translatePath() function"""
    assert isinstance(path, str)
    if path.startswith('special://home'):
        return path.replace('special://home', os.path.join(os.getcwd(), 'tests'))
    if path.startswith('special://masterprofile'):
        return path.replace('special://masterprofile', os.path.join(os.getcwd(), 'tests', 'userdata'))
    if path.startswith('special://profile'):
        return path.replace('special://profile', os.path.join(os.getcwd(), 'tests', 'userdata'))
    if path.startswith('special://userdata'):
        return path.replace('special://userdata', os.path.join(os.getcwd(), 'tests', 'userdata'))
    return path
