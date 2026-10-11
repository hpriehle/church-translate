"""TCP proxy that simulates a phone losing Wi-Fi: on SIGUSR1 it resets every open connection
and refuses new ones for --outage seconds. Used by scripts/browser_check.mjs --proxy."""
import argparse
import asyncio
import signal
import time

ap = argparse.ArgumentParser()
ap.add_argument("--listen", type=int, required=True)
ap.add_argument("--target", type=int, required=True)
ap.add_argument("--outage", type=float, default=10)
a = ap.parse_args()
conns: set = set()
down_until = 0.0


async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except Exception:
        pass
    finally:
        w.close()


async def handle(cr, cw):
    if time.monotonic() < down_until:
        cw.transport.abort()
        return
    try:
        sr, sw = await asyncio.open_connection("127.0.0.1", a.target)
    except OSError:
        cw.transport.abort()
        return
    pair = (cw, sw)
    conns.add(pair)
    await asyncio.gather(pipe(cr, sw), pipe(sr, cw))
    conns.discard(pair)


def outage():
    global down_until
    down_until = time.monotonic() + a.outage
    for cw, sw in list(conns):
        cw.transport.abort()
        sw.transport.abort()
    print(f"outage: dropped {len(conns)} connections for {a.outage}s", flush=True)


async def main():
    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, outage)
    srv = await asyncio.start_server(handle, "127.0.0.1", a.listen)
    async with srv:
        await srv.serve_forever()

asyncio.run(main())
