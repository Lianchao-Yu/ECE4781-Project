"""
BLE link self-test: scan for BME-S3, connect, capture a few seconds and report.

No PyQt dependency, so it is suitable for CI or a quick check before opening
the GUI:
    python ble_selftest.py [--seconds 3] [--name BME-S3]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import struct
import sys

from bleak import BleakClient, BleakScanner

SERVICE_UUID = "5f9b1c00-3e4a-4d21-9a77-0b1c2d3e4f50"
TX_UUID = "5f9b1c01-3e4a-4d21-9a77-0b1c2d3e4f50"
RX_UUID = "5f9b1c02-3e4a-4d21-9a77-0b1c2d3e4f50"

FRAME = struct.Struct("<BBBBHB10h2h")
assert FRAME.size == 31


def decode_all(chunks: list[bytes]) -> dict:
    """Resynchronise on the magic word and unpack every complete frame."""
    buf = bytearray()
    for c in chunks:
        buf += c

    emg: list[int] = []
    ppg: list[int] = []
    eda: list[int] = []
    seqs: list[int] = []
    flags_seen: set[int] = set()

    while True:
        idx = buf.find(b"\xA5\x5A")
        if idx < 0 or len(buf) - idx < FRAME.size:
            break
        del buf[:idx]
        m0, m1, ver, flags, seq, n, *rest = FRAME.unpack(bytes(buf[: FRAME.size]))
        del buf[: FRAME.size]
        if ver != 1 or n != 10:
            continue
        flags_seen.add(flags)
        seqs.append(seq)
        emg.extend(rest[:10])
        ppg.append(rest[10])
        eda.append(rest[11])

    lost = 0
    for prev, cur in zip(seqs, seqs[1:]):
        gap = (cur - prev) & 0xFFFF
        if 1 < gap < 6000:
            lost += gap - 1

    return {
        "frames": len(seqs),
        "lost": lost,
        "emg": emg,
        "ppg": ppg,
        "eda": eda,
        "flags": flags_seen,
    }


async def run(name: str, seconds: float, address: str | None) -> int:
    if address:
        device = await BleakScanner.find_device_by_address(address, timeout=10.0)
    else:
        print(f"Scanning for {name} ...")
        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: (ad.local_name or d.name or "").startswith(name)
            or SERVICE_UUID in [u.lower() for u in (ad.service_uuids or [])],
            timeout=15.0,
        )
    if device is None:
        print("Device not found: check the firmware, power and that no other app owns it")
        return 1

    print(f"Connecting to {device.name or '?'} ({device.address}) ...")
    chunks: list[bytes] = []
    async with BleakClient(device, timeout=20.0) as client:
        print(f"MTU = {client.mtu_size}")
        if client.mtu_size < 34:
            print("Warning: MTU too small for the 31-byte frame")

        await client.start_notify(TX_UUID, lambda _c, data: chunks.append(bytes(data)))
        try:
            await client.write_gatt_char(RX_UUID, b"\x01", response=False)
        except Exception as exc:
            print(f"Start command failed (data still flows): {exc}")

        print(f"Capturing {seconds:.0f} s ...")
        await asyncio.sleep(seconds)
        await client.stop_notify(TX_UUID)

    stats = decode_all(chunks)
    frames = stats["frames"]
    if frames == 0:
        print("No frames received: check the subscription and the negotiated MTU")
        return 1

    emg, ppg, eda = stats["emg"], stats["ppg"], stats["eda"]
    total = frames + stats["lost"]
    print()
    print(f"frames      {frames} ({frames / seconds:.1f} frames/s)")
    print(f"lost        {stats['lost']} ({100 * stats['lost'] / total:.2f}%)")
    print(f"EMG points  {len(emg)} ({len(emg) / seconds:.0f} Hz)")
    print(f"  min/max   {min(emg)} / {max(emg)}")
    print(f"  std       {statistics.pstdev(emg):.0f}")
    print(f"PPG points  {len(ppg)}, range {min(ppg)} ~ {max(ppg)}")
    print(f"EDA points  {len(eda)}, range {min(eda)} ~ {max(eda)}")
    print(f"flags       {sorted(stats['flags'])} (bit0=bench mode, bit1=saturated)")

    ok = (
        frames >= 0.8 * 100 * seconds
        and len(emg) >= 0.8 * 1000 * seconds
        and statistics.pstdev(emg) > 0
        and max(ppg) > min(ppg)
    )
    print()
    print("Result: " + ("PASS" if ok else "FAIL, data does not look right"))
    return 0 if ok else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--name", default="BME-S3")
    parser.add_argument("--address", default=None, help="Known MAC, skips the scan")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.name, args.seconds, args.address))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
