"""Manual smoke test for a running voicebox.

    python demo_client.py --token TOKEN [--url ws://localhost:8100/ws]
        [--wav speech.wav] [--say "text to synthesize"] [--out out.wav]

With --wav: streams the file as mic audio (real-time pacing) and prints the
VAD/transcript events. With --say: requests one TTS utterance and writes the
returned audio to --out. Both by default (--say has a default sentence).
WAV input must be 16 kHz mono PCM16 (sox: `sox in.wav -r 16000 -c 1 -b 16 out.wav`).
"""
import argparse
import asyncio
import json
import struct
import sys
import wave

import websockets

MIC_FRAME = b"\x01"


async def run(args) -> int:
    got_tts: list[bytes] = []
    tts_done = asyncio.Event()
    transcript_done = asyncio.Event()

    async with websockets.connect(
            args.url, additional_headers={"Authorization": f"Bearer {args.token}"},
            max_size=2 ** 22) as ws:

        async def reader():
            async for msg in ws:
                if isinstance(msg, bytes):
                    if msg[:1] == b"\x02":
                        (tts_id,) = struct.unpack("<I", msg[1:5])
                        got_tts.append(msg[5:])
                        print(f"<- tts audio id={tts_id} {len(msg) - 5} bytes")
                    continue
                ev = json.loads(msg)
                print(f"<- {ev}")
                if ev.get("type") == "transcript":
                    transcript_done.set()
                if ev.get("type") == "tts_done":
                    tts_done.set()

        rtask = asyncio.create_task(reader())

        if args.wav:
            with wave.open(args.wav, "rb") as w:
                assert w.getframerate() == 16000 and w.getnchannels() == 1 \
                    and w.getsampwidth() == 2, "need 16 kHz mono PCM16"
                pcm = w.readframes(w.getnframes())
            print(f"-> streaming {args.wav} ({len(pcm) // 32} ms)")
            step = 1920                       # 60 ms per frame, like the browser
            for off in range(0, len(pcm), step):
                await ws.send(MIC_FRAME + pcm[off:off + step])
                await asyncio.sleep(0.06)
            # trailing silence so the VAD hangover can fire
            for _ in range(20):
                await ws.send(MIC_FRAME + b"\x00" * step)
                await asyncio.sleep(0.06)
            await asyncio.wait_for(transcript_done.wait(), 30)

        if args.say:
            print(f"-> tts: {args.say!r}")
            await ws.send(json.dumps({"type": "tts", "id": 1, "text": args.say}))
            await asyncio.wait_for(tts_done.wait(), 60)
            pcm = b"".join(got_tts)
            with wave.open(args.out, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(pcm)
            print(f"wrote {args.out} ({len(pcm) // 48} ms)")

        rtask.cancel()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://localhost:8100/ws")
    p.add_argument("--token", required=True)
    p.add_argument("--wav", help="16 kHz mono PCM16 wav to stream as mic input")
    p.add_argument("--say", default="Hello, this is the voicebox speaking.")
    p.add_argument("--out", default="out.wav")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
