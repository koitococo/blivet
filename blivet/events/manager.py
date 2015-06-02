# events/manager.py
# Event management classes.
#
# Copyright (C) 2015-2016  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU Lesser General Public License v.2, or (at your option) any later
# version. This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY expressed or implied, including the implied
# warranties of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See
# the GNU Lesser General Public License for more details.  You should have
# received a copy of the GNU Lesser General Public License along with this
# program; if not, write to the Free Software Foundation, Inc., 51 Franklin
# Street, Fifth Floor, Boston, MA 02110-1301, USA.  Any Red Hat trademarks
# that are incorporated in the source code or documentation are not subject
# to the GNU Lesser General Public License and may only be used or
# replicated with the express permission of Red Hat, Inc.
#
# Red Hat Author(s): David Lehman <dlehman@redhat.com>
#

import abc
from threading import RLock, Thread
import pyudev
import time

from .. import udev
from .. import util
from ..errors import EventManagerError, EventParamError
from ..flags import flags
from ..threads import blivet_lock

import logging
event_log = logging.getLogger("blivet.event")


#
# Event
#
class Event(util.ObjectID):
    """ An external event. """
    def __init__(self, action, device, info=None):
        """
            :param str action: a string describing the type of event
            :param str device: (friendly) basename of device event operated on
            :param info: information about the device
        """
        self.initialized = time.time()
        self.action = action
        self.device = device
        self.info = info

    def __str__(self):
        return "%s %s [%d]" % (self.action, self.device, self.id)


class EventMask(util.ObjectID):
    """ Specification of events to ignore. """
    def __init__(self, device=None, action=None, partitions=False):
        """
            :keyword str device: basename of device to mask events on
            :keyword str action: action type to mask events of
            :keyword bool partitions: also match events on child partitions
        """
        self.device = device
        self.action = action
        self._partitions = partitions

    def _device_match(self, event):
        if self.device is None:
            return True

        if self.device == event.device:
            return True

        if (not self._partitions or
                not (udev.device_is_partition(event.info) or udev.device_is_dm_partition(event.info))):
            return False

        disk = udev.device_get_partition_disk(event.info)
        return disk and self.device == disk

    def _action_match(self, event):
        return self.action is None or self.action == event.action

    def match(self, event):
        """ Return True if this mask applies to the specified event.

            ..note::

                A mask whose device is a partitioned disk will match events
                on its partitions.
        """
        return self._device_match(event) and self._action_match(event)


#
# EventManager
#
class EventManager(object, metaclass=abc.ABCMeta):
    def __init__(self, handler_cb=None, notify_cb=None):
        self._handler_cb = None
        self._notify_cb = None

        if handler_cb is not None:
            self.handler_cb = handler_cb

        if notify_cb is not None:
            self.notify_cb = notify_cb

        self._mask_list = list()
        """List of masks specifying events that should be ignored."""

        self._lock = RLock()
        """Re-entrant lock to serialize access to mask list."""

    @property
    def handler_cb(self):
        """ the main event handler """
        return self._handler_cb

    @handler_cb.setter
    def handler_cb(self, cb):
        if not callable(cb):
            raise EventParamError("handler must be callable")

        self._handler_cb = cb

    @property
    def notify_cb(self):
        """ notification handler that runs after the main event handler """
        return self._notify_cb

    @notify_cb.setter
    def notify_cb(self, cb):
        if not callable(cb) or cb.func_code.argcount < 1:
            raise EventParamError("callback function must accept at least one arg")

        self._notify_cb = cb

    @abc.abstractproperty
    def enabled(self):
        return False

    @abc.abstractmethod
    def enable(self):
        """ Enable monitoring and handling of events.

            :raises: :class:`~.errors.EventManagerError` if no callback defined
        """
        if self.handler_cb is None:
            raise EventManagerError("cannot enable handler with no callback")

        event_log.info("enabling event handling")

    @abc.abstractmethod
    def disable(self):
        """ Disable monitoring and handling of events. """
        event_log.info("disabling event handling")

    def _mask_event(self, event):
        """ Return True if this event should be ignored """
        with self._lock:
            return next((m for m in self._mask_list if m.match(event)), None) is not None

    def add_mask(self, device=None, action=None, partitions=False):
        """ Add an event mask and return the new :class:`EventMask`.

            :keyword str device: ignore events on the named device
            :keyword str action: ignore events of the specified type
            :keyword bool partitions: also match events on child partitions

            device of None means mask events on all devices
            action of None means mask all event types
        """
        em = EventMask(device=device, action=action, partitions=partitions)
        with self._lock:
            self._mask_list.append(em)
        return em

    def remove_mask(self, mask):
        try:
            with self._lock:
                self._mask_list.remove(mask)
        except ValueError:
            pass

    @abc.abstractmethod
    def _create_event(self, *args, **kwargs):
        pass

    def handle_event(self, *args, **kwargs):
        """ Handle an event by running the registered handler.

            Currently the handler is run in a separate thread. This removes any
            threading-related expectations about the behavior of whatever is
            telling us about the events.
        """
        event = self._create_event(*args, **kwargs)
        event_log.debug("new event: %s", event)

        if self._mask_event(event):
            event_log.debug("ignoring masked event %s", event)
            return

        t = Thread(target=self.handler_cb,
                   name="event%d" % event.id,
                   kwargs={"event": event},
                   daemon=True)
        t.start()


class UdevEventManager(EventManager):
    def __init__(self, handler_cb=None, notify_cb=None):
        super().__init__(handler_cb=handler_cb, notify_cb=notify_cb)
        self._pyudev_observer = None

    @property
    def enabled(self):
        return self._pyudev_observer and self._pyudev_observer.monitor.started

    def enable(self):
        """ Enable monitoring and handling of block device uevents. """
        super().enable()
        monitor = pyudev.Monitor.from_netlink(udev.global_udev)
        monitor.filter_by("block")
        self._pyudev_observer = pyudev.MonitorObserver(monitor,
                                                       callback=self.handle_event,
                                                       name="monitor")
        self._pyudev_observer.start()
        with blivet_lock:
            flags.uevents = True

    def disable(self):
        """ Disable monitoring and handling of block device uevents. """
        super().disable()
        if self.enabled:
            self._pyudev_observer.stop()

        self._pyudev_observer = None
        with blivet_lock:
            flags.uevents = False

    def __call__(self, *args, **kwargs):
        return self

    def _create_event(self, *args, **kwargs):
        return Event(args[0].action, udev.device_get_name(args[0]), args[0])


event_manager = UdevEventManager()
