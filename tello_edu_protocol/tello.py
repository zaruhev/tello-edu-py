from typing import (
    AsyncContextManager,
    AsyncGenerator,
    TypeAlias,
    Awaitable,
    Callable,
    Tuple,
)
import contextlib
import asyncio
import av


from .state import DroneState


DEFAULT_TELLO_IP = '192.168.10.1'
DEFAULT_LOCALHOST = '0.0.0.0'
DEFAULT_TIMEOUT = 5.0
CONTROL_PORT = 8889
STATE_PORT = 8890
VIDEO_PORT = 11111
VIDEO_RESOLUTION = (640, 480)


class Drone:
    '''The user api for interacting with the drone.'''
    '''TODO (Sarah): Translate the sdk commands into Drone methods'''
    
    SendFn: TypeAlias = Callable[[str, float], Awaitable[str]]
    StateFn: TypeAlias = Callable[[float], Awaitable[DroneState]]
    Address: TypeAlias = Tuple[str, int]

    __slots__ = ('addr', 'send', 'state')

    def __init__(self, addr: Address, send: SendFn, state: StateFn) -> None:
        '''
        The `send` and `state` methods are both generated by a drone
        connection manager to avoid coupling and ensure encapsulation.

        Funny how a more functional style solves problems that OOP makes
        for itself. ¯\_(ツ)_/¯
        '''

        self.addr = addr
        self.send = send
        self.state = state

    async def video_feed(self) -> AsyncGenerator:
        '''
        TODO (Carter): The drone sometimes decides not to stream video
                       determine if this is software or hardware
        '''

        # Probably hardware

        await self.send('streamon')

        try:
            with av.open('udp://@0.0.0.0:11111') as container:
                for frame in container.decode(video=0):
                    yield frame.to_ndarray(format='bgr24')              

        finally:
            await self.send('streamoff')


class Protocol(asyncio.DatagramProtocol):
    '''The `low level` drone communication protocol'''

    Value: TypeAlias = str | DroneState
    Queue: TypeAlias = asyncio.Queue[Value]
    DatagramHandlerFn: TypeAlias = Callable[[str], Value]

    __slots__ = ('queue', 'datagram_handler')
    
    def __init__(self, datagram_handler: DatagramHandlerFn) -> None:
        self.queue = asyncio.Queue()
        self.datagram_handler = datagram_handler

    def datagram_received(self, data: bytes, _) -> None:
        self.queue.put_nowait(self.datagram_handler(data))


def cmd_datagram_handler(data: bytes) -> str:
    decoded = data.decode(encoding='ASCII').strip()
    
    if decoded.starts_with('unknown command: '):
        cmd = decoded.lstrip('unknown command: ')
        raise ValueError(f'Unknown command: {cmd}')
    
    elif decoded.starts_with('error'):
        raise RuntimeError('Drone reported an error')
    
    return decoded

def state_datagram_handler(data: bytes) -> DroneState:
    return DroneState.from_raw(data.decode().strip())


def keepalive(drone: Drone) -> Callable[[], Awaitable[None]]:
    '''
    A background runner for keeping an active connection with
    the drone whilst the connection is active.

    Returns a function that will end the background task.
    '''

    async def task() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                await drone.send('time?')
                await asyncio.sleep(10)

    keepalive_task = asyncio.create_task(task())

    async def stop() -> None:
        keepalive_task.cancel()
        await asyncio.wait({keepalive_task})

    return stop


@contextlib.asynccontextmanager
async def conn(ip: str = DEFAULT_TELLO_IP) -> AsyncContextManager[Drone]:
    '''
    The context manager for a drone connection.
    '''
    
    loop = asyncio.get_running_loop()
    addr = (ip, CONTROL_PORT)

    cmd_transport, cmd_protocol = await loop.create_datagram_endpoint(
        lambda: Protocol(cmd_datagram_handler),
        remote_addr=(addr),
        local_addr=((DEFAULT_LOCALHOST, CONTROL_PORT)),
    )

    state_transport, state_protocol = await loop.create_datagram_endpoint(
        lambda: Protocol(state_datagram_handler),
        local_addr=((DEFAULT_LOCALHOST, STATE_PORT)),
    )

    # The generated `send method` for the Drone class
    async def send(*command: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        async with asyncio.timeout(timeout):
            cmd_transport.sendto(command.encode(format='utf_8'))
            return await cmd_protocol.queue.get()
    
    # The generated `state method` for the Drone class
    async def state(*, timeout: float = DEFAULT_TIMEOUT) -> DroneState:
        async with asyncio.timeout(timeout):
            return await state_protocol.queue.get()

    try:
        drone = Drone(addr, send, state)
        keepalive_stop = keepalive(drone) 
        await drone.send('command')

        yield drone

    finally:
        await keepalive_stop()

        cmd_transport.close()
        state_transport.close()
