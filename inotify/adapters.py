import logging
import select
import os
import struct
import collections
import bisect

from errno import EINTR

import inotify.constants
import inotify.calls

# Constants.

_DEFAULT_EPOLL_BLOCK_DURATION_S = 1
_HEADER_STRUCT_FORMAT = 'iIII'

# Globals.

_LOGGER = logging.getLogger(__name__)

_INOTIFY_EVENT = collections.namedtuple(
                    '_INOTIFY_EVENT',
                    [
                        'wd',
                        'mask',
                        'cookie',
                        'len',
                    ])

_STRUCT_HEADER_LENGTH = struct.calcsize(_HEADER_STRUCT_FORMAT)
_IS_DEBUG = bool(int(os.environ.get('DEBUG', '0')))


class Inotify(object):
    def __init__(self, paths=[], block_duration_s=_DEFAULT_EPOLL_BLOCK_DURATION_S):
        self.__block_duration = block_duration_s
        self.__watches = {}
        self.__watches_l = collections.deque()
        self.__watches_r = {}
        self.__buffer = b''

        self.__inotify_fd = inotify.calls.inotify_init()
        _LOGGER.debug("Inotify handle is (%d).", self.__inotify_fd)

        self.__epoll = select.epoll()

        self.__epoll.register(self.__inotify_fd, select.POLLIN)

        for path in paths:
            self.add_watch(path)

    def __get_block_duration(self):
        """Allow the block-duration to be an integer or a function-call."""

        try:
            return self.__block_duration()
        except TypeError:
            # A scalar value describing seconds.
            return self.__block_duration

    def __del__(self):
        _LOGGER.debug("Cleaning-up inotify.")
        os.close(self.__inotify_fd)

    def add_watch(self, path, mask=inotify.constants.IN_ALL_EVENTS):
        _LOGGER.debug("Adding watch: [%s]", path)

        try:
            wd = inotify.calls.inotify_add_watch(self.__inotify_fd, path, mask)
        except inotify.calls.InotifyError as e:
            e.args = (e.args[0] + " path=(%s)" % (path,),)
            raise
        _LOGGER.debug("Added watch (%d): [%s]", wd, path)

        if path not in self.__watches:
            idx = bisect.bisect(self.__watches_l, path)
            self.__watches_l.insert(idx, path)

        self.__watches[path] = wd
        self.__watches_r[wd] = path

    def remove_watch(self, path, superficial=False):
        """Remove our tracking information and call inotify to stop watching
        the given path. When a directory is removed, we'll just have to remove
        our tracking since inotify already cleans-up the watch.
        """

        wd = self.__watches.get(path)
        if wd is None:
            return

        idx = bisect.bisect_left(self.__watches_l, path)
        del self.__watches_l[idx]

        del self.__watches[path]
        del self.__watches_r[wd]

        if superficial is False:
            _LOGGER.debug("Removing watch for watch-handle (%d): [%s]",
                          wd, path)

            inotify.calls.inotify_rm_watch(self.__inotify_fd, wd)

    def remove_watch_tree(self, tree, superficial=False):
        if self.__watches.get(tree) is None:
            return

        count = len(self.__watches_l)
        idx = bisect.bisect_left(self.__watches_l, tree)
        prefix = tree + b'/'

        while count > idx:
            path = self.__watches_l[idx]
            if not path == tree and not path.startswith(prefix):
                break
            count -= 1
            wd = self.__watches.get(path)

            del self.__watches_l[idx]
            del self.__watches[path]
            del self.__watches_r[wd]

            if superficial is False:
                _LOGGER.debug("Removing watch for watch-handle (%d): [%s]",
                              wd, path)

                inotify.calls.inotify_rm_watch(self.__inotify_fd, wd)

    def __get_event_names(self, event_type):
        names = []
        for bit, name in inotify.constants.MASK_LOOKUP.items():
            if event_type & bit:
                names.append(name)
                event_type -= bit

                if event_type == 0:
                    break

        assert event_type == 0, \
               "We could not resolve all event-types: (%d)" % (event_type,)

        return names

    def __handle_inotify_event(self, wd, event_type):
        """Handle a series of events coming-in from inotify."""

        names = self.__get_event_names(event_type)

        b = os.read(wd, 1024)
        if not b:
            return

        self.__buffer += b

        while 1:
            length = len(self.__buffer)

            if length < _STRUCT_HEADER_LENGTH:
                _LOGGER.debug("Not enough bytes for a header.")
                return

            # We have, at least, a whole-header in the buffer.

            peek_slice = self.__buffer[:_STRUCT_HEADER_LENGTH]

            header_raw = struct.unpack(
                            _HEADER_STRUCT_FORMAT,
                            peek_slice)

            header = _INOTIFY_EVENT(*header_raw)
            type_names = self.__get_event_names(header.mask)

            event_length = (_STRUCT_HEADER_LENGTH + header.len)
            if length < event_length:
                return

            filename = self.__buffer[_STRUCT_HEADER_LENGTH:event_length]

            # Our filename is 16-byte aligned and right-padded with NULs.
            filename = filename.rstrip(b'\0')

            self.__buffer = self.__buffer[event_length:]

            path = self.__watches_r.get(header.wd)
            if path is not None:
                yield (header, type_names, path, filename)

# TODO(dustin): !! For renames, we should drop the entry and re-add with the new name.
# TODO(dustin): !! The add_watch() call should return the handle. We should be
#                  able to remove the watches using the handle (not just with
#                  the path).

            buffer_length = len(self.__buffer)
            if buffer_length < _STRUCT_HEADER_LENGTH:
                break

    def event_gen(self):
        while True:
            block_duration_s = self.__get_block_duration()

            # Poll, but manage signal-related errors.

            try:
                events = self.__epoll.poll(block_duration_s)
            except IOError as e:
                if e.errno != EINTR:
                    raise

                continue

            # Process events.

            for fd, event_type in events:
                # (fd) looks to always match the inotify FD.

                for (header, type_names, path, filename) \
                        in self.__handle_inotify_event(fd, event_type):
                    yield (header, type_names, path, filename)

            yield None

class BaseTree(object):

    def __init__(self, mask=inotify.constants.IN_ALL_EVENTS,
                 block_duration_s=_DEFAULT_EPOLL_BLOCK_DURATION_S):

        # No matter what we actually received as the mask, make sure we have
        # the minimum that we require to curate our list of watches.
        self._mask = mask | \
                        inotify.constants.IN_ISDIR | \
                        inotify.constants.IN_CREATE | \
                        inotify.constants.IN_DELETE | \
                        inotify.constants.IN_MOVED_FROM | \
                        inotify.constants.IN_MOVED_TO

        self._i = Inotify(block_duration_s=block_duration_s)

    def event_gen(self):
        """This is a secondary generator that wraps the principal one, and
        adds/removes watches as directories are added/removed.
        """

        for event in self._i.event_gen():
            if event is not None:
                (header, type_names, path, filename) = event

                if header.mask & inotify.constants.IN_ISDIR:
                    full_path = os.path.join(path, filename)

                    if header.mask & inotify.constants.IN_CREATE:
                        _LOGGER.debug("A directory has been created. We're "
                                      "adding a watch on it (because we're "
                                      "being recursive): [%s]", full_path)

                        self._load_tree(full_path)
                    elif header.mask & inotify.constants.IN_MOVED_TO:
                        _LOGGER.debug("A directory has been moved to. We're "
                                      "adding a watch on it (because we're "
                                      "being recursive): [%s]", full_path)

                        self._load_tree(full_path)
                    elif header.mask & inotify.constants.IN_DELETE:
                        _LOGGER.debug("A directory has been removed. We're "
                                      "being recursive, but it would have "
                                      "automatically been deregistered: [%s]",
                                      full_path)

                        # The watch would've already been cleaned-up internally.
                        self._i.remove_watch(full_path, superficial=True)
                    elif header.mask & inotify.constants.IN_MOVED_FROM:
                        _LOGGER.debug("A directory has been moved from. We're "
                                      "being recursive, but it would have "
                                      "automatically been deregistered: [%s]",
                                      full_path)

                        # MOVED_FROM event does not trigger for sub-directories.
                        # The watch would've already been cleaned-up internally.
                        self._i.remove_watch_tree(full_path, superficial=False)

            yield event

    def _load_tree(self, path):
        q = [path]
        while q:
            current_path = q[0]
            del q[0]

            self._i.add_watch(current_path, self._mask)

            for filename in os.listdir(current_path):
                entry_filepath = os.path.join(current_path, filename)
                if os.path.isdir(entry_filepath) is False:
                    continue

                q.append(entry_filepath)


class InotifyTree(BaseTree):
    def __init__(self, path, mask=inotify.constants.IN_ALL_EVENTS,
                 block_duration_s=_DEFAULT_EPOLL_BLOCK_DURATION_S):
        super(InotifyTree, self).__init__(mask=mask, block_duration_s=block_duration_s)

        self.__root_path = path

        self.__load_tree(path)

    def __load_tree(self, path):
        _LOGGER.debug("Adding initial watches on tree: [%s]", path)
        self._load_tree(path)


class InotifyTrees(BaseTree):

    def __init__(self, paths, mask=inotify.constants.IN_ALL_EVENTS,
                 block_duration_s=_DEFAULT_EPOLL_BLOCK_DURATION_S):
        super(InotifyTrees, self).__init__(mask=mask, block_duration_s=block_duration_s)

        self.__load_trees(paths)

    def __load_trees(self, paths):
        _LOGGER.debug("Adding initial watches on trees: [%s]", ",".join(map(str, paths)))
        for path in paths:
            self._load_tree(path)
