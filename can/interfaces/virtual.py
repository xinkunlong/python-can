"""
This module implements an OS and hardware independent
virtual CAN interface for testing purposes.

Any VirtualBus instances connecting to the same channel
and reside in the same process will receive the same messages.
"""

from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from copy import deepcopy
import logging
import time
import queue
from threading import RLock
from random import randint

from can import CanOperationError
from can.bus import BusABC
from can.message import Message
from can.typechecking import AutoDetectedConfig

logger = logging.getLogger(__name__)


# Channels are lists of queues, one for each connection
if TYPE_CHECKING:
    # https://mypy.readthedocs.io/en/stable/runtime_troubles.html#using-classes-that-are-generic-in-stubs-but-not-at-runtime
    channels: Dict[Optional[Any], List[queue.Queue[Message]]] = {}
else:
    channels = {}
channels_lock = RLock()


class VirtualBus(BusABC):
    """
    A virtual CAN bus using an internal message queue. It can be used for example for testing.

    In this interface, a channel is an arbitrary object used as
    an identifier for connected buses.

    Implements :meth:`can.BusABC._detect_available_configs`; see
    :meth:`_detect_available_configs` for how it
    behaves here.

    .. note::
        The timeout when sending a message applies to each receiver
        individually. This means that sending can block up to 5 seconds
        if a message is sent to 5 receivers with the timeout set to 1.0.

    .. warning::
        This interface guarantees reliable delivery and message ordering, but does *not* implement rate
        limiting or ID arbitration/prioritization under high loads. Please refer to the section
        :ref:`virtual_interfaces_doc` for more information on this and a comparison to alternatives.
    """

    def __init__(
        self,
        channel: Any = None,
        receive_own_messages: bool = False,
        rx_queue_size: int = 0,
        preserve_timestamps: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            channel=channel, receive_own_messages=receive_own_messages, **kwargs
        )

        # the channel identifier may be an arbitrary object
        self.channel_id = channel
        self.channel_info = f"Virtual bus channel {self.channel_id}"
        self.receive_own_messages = receive_own_messages
        self.preserve_timestamps = preserve_timestamps
        self._open = True

        with channels_lock:
            # Create a new channel if one does not exist
            if self.channel_id not in channels:
                channels[self.channel_id] = []
            self.channel = channels[self.channel_id]

            self.queue: queue.Queue[Message] = queue.Queue(rx_queue_size)
            self.channel.append(self.queue)

    def _check_if_open(self) -> None:
        """Raises :exc:`~can.exceptions.CanOperationError` if the bus is not open.

        Has to be called in every method that accesses the bus.
        """
        if not self._open:
            raise CanOperationError("Cannot operate on a closed bus")

    def _recv_internal(
        self, timeout: Optional[float]
    ) -> Tuple[Optional[Message], bool]:
        self._check_if_open()
        try:
            msg = self.queue.get(block=True, timeout=timeout)
        except queue.Empty:
            return None, False
        else:
            return msg, False

    def send(self, msg: Message, timeout: Optional[float] = None) -> None:
        self._check_if_open()

        timestamp = msg.timestamp if self.preserve_timestamps else time.time()
        # Add message to all listening on this channel
        all_sent = True
        for bus_queue in self.channel:
            if bus_queue is self.queue and not self.receive_own_messages:
                continue
            msg_copy = deepcopy(msg)
            msg_copy.timestamp = timestamp
            msg_copy.channel = self.channel_id
            msg_copy.is_rx = bus_queue is not self.queue
            try:
                bus_queue.put(msg_copy, block=True, timeout=timeout)
            except queue.Full:
                all_sent = False

        if not all_sent:
            raise CanOperationError("Could not send message to one or more recipients")

    def shutdown(self) -> None:
        super().shutdown()
        if self._open:
            self._open = False

            with channels_lock:
                self.channel.remove(self.queue)

                # remove if empty
                if not self.channel:
                    del channels[self.channel_id]

    @staticmethod
    def _detect_available_configs() -> List[AutoDetectedConfig]:
        """
        Returns all currently used channels as well as
        one other currently unused channel.

        .. note::

            This method will run into problems if thousands of
            autodetected buses are used at once.

        """
        with channels_lock:
            available_channels = list(channels.keys())

        # find a currently unused channel
        def get_extra():
            return f"channel-{randint(0, 9999)}"

        extra = get_extra()
        while extra in available_channels:
            extra = get_extra()

        available_channels += [extra]

        return [
            {"interface": "virtual", "channel": channel}
            for channel in available_channels
        ]
