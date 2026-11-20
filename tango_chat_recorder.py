#!/usr/bin/env python3
"""
tango_chat_recorder.py — Record tango.me live chat to SRT subtitle file.

STATUS: experimental, not part of the extractor.  It still points at
`wss://gateway.tango.me/ws`; the chat WebSocket used by the current web
player has not been decoded.  Treat this as a starting point, and verify the
endpoint in a browser before relying on it.

Usage:
    python3 tango_chat_recorder.py <username> [--output chat.srt]

Requires:
    pip install websockets

This captures chat messages from tango.me live streams and writes them
as an SRT subtitle file that can be burned into the video with ffmpeg:
    ffmpeg -i video.mp4 -vf "subtitles=chat.srt" output.mp4

Note: tango.me uses protobuf-encoded messages over WebSocket.
Without authentication, only public stream data is accessible.
For private/authenticated streams, pass browser cookies.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import timedelta

try:
    import websockets
except ImportError:
    print("Error: 'websockets' package required. Install with: pip install websockets")
    sys.exit(1)

# tango.me WebSocket endpoint is dynamically resolved.
# This is the known pattern — actual URL comes from API config.
DEFAULT_WS_URL = "wss://gateway.tango.me/ws"

class TangoChatRecorder:
    def __init__(self, username, output_file="chat.srt", ws_url=None):
        self.username = username
        self.output_file = output_file
        self.ws_url = ws_url or DEFAULT_WS_URL
        self.messages = []
        self.start_time = time.time()

    async def connect(self):
        """Connect to tango.me chat WebSocket."""
        headers = {
            "Origin": "https://tango.me",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        }

        try:
            async with websockets.connect(
                self.ws_url, extra_headers=headers, ping_interval=30
            ) as ws:
                print(f"Connected to: {self.ws_url}")
                await self._listen(ws)
        except websockets.exceptions.InvalidURI:
            print(f"Error: Invalid WebSocket URL: {self.ws_url}")
        except websockets.exceptions.InvalidHandshake as e:
            print(f"Error: WebSocket handshake failed — {e}")
            print("The stream may require authentication. Try with browser cookies.")
        except OSError as e:
            print(f"Error: Connection failed — {e}")
        except asyncio.CancelledError:
            pass

    async def _listen(self, ws):
        """Listen for chat messages."""
        async for raw_message in ws:
            try:
                msg = self._parse_message(raw_message)
                if msg:
                    self.messages.append(msg)
                    elapsed = time.time() - self.start_time
                    ts = str(timedelta(seconds=int(elapsed)))
                    user = msg.get("username", "unknown")
                    text = msg.get("text", "")
                    gift = msg.get("gift", "")
                    line = f"[{ts}] {user}: {text}"
                    if gift:
                        line += f"  [GIFT: {gift}]"
                    print(line)
            except Exception as e:
                print(f"Parse error: {e}")

    def _parse_message(self, raw):
        """Parse a raw WebSocket message into chat dict.

        tango.me uses protobuf (binary) encoding for messages.
        This is a best-effort parser. For full protobuf decoding,
        the .proto schema from the webpack bundle is needed.
        """
        if isinstance(raw, bytes):
            # Try JSON first (some messages may be JSON)
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

            # Binary protobuf — attempt basic field extraction
            # tango chat proto likely has: username(string), text(string), gift(string), timestamp(int64)
            # This extracts visible ASCII strings as best-effort
            text_parts = []
            current = bytearray()
            for b in raw:
                if 32 <= b < 127:  # printable ASCII
                    current.append(b)
                else:
                    if len(current) >= 3:
                        text_parts.append(current.decode("ascii", errors="replace"))
                    current = bytearray()
            if len(current) >= 3:
                text_parts.append(current.decode("ascii", errors="replace"))

            if text_parts:
                return {
                    "username": self.username,
                    "text": " | ".join(text_parts),
                    "timestamp": time.time(),
                }
            return None

        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"text": raw, "timestamp": time.time()}

        return None

    def write_srt(self):
        """Write collected messages as SRT subtitle file."""
        if not self.messages:
            print("No messages captured.")
            return

        with open(self.output_file, "w", encoding="utf-8") as f:
            for i, msg in enumerate(self.messages, 1):
                start = msg.get("timestamp", 0) - self.messages[0].get("timestamp", 0)
                end = start + 5  # Show each message for 5 seconds

                start_srt = self._srt_time(start)
                end_srt = self._srt_time(end)

                user = msg.get("username", "")
                text = msg.get("text", "")
                gift = msg.get("gift", "")

                subtitle = f"{user}: {text}"
                if gift:
                    subtitle += f" [{gift}]"

                f.write(f"{i}\n{start_srt} --> {end_srt}\n{subtitle}\n\n")

        print(f"\nSaved {len(self.messages)} messages to {self.output_file}")

    @staticmethod
    def _srt_time(seconds):
        td = timedelta(seconds=max(0, seconds))
        total_secs = int(td.total_seconds())
        hours, remainder = divmod(total_secs, 3600)
        minutes, secs = divmod(remainder, 60)
        millis = int((td.total_seconds() - total_secs) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


async def main():
    parser = argparse.ArgumentParser(
        description="Record tango.me live chat to SRT subtitle file"
    )
    parser.add_argument("username", help="tango.me username")
    parser.add_argument("--output", "-o", default="chat.srt", help="Output SRT file")
    parser.add_argument(
        "--ws-url",
        default=None,
        help="WebSocket URL (default: auto-detect)",
    )
    args = parser.parse_args()

    recorder = TangoChatRecorder(
        username=args.username,
        output_file=args.output,
        ws_url=args.ws_url,
    )

    print(f"Recording chat for: tango.me/{args.username}")
    print(f"Output: {args.output}")
    print("Press Ctrl+C to stop.\n")

    try:
        await recorder.connect()
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        recorder.write_srt()


if __name__ == "__main__":
    asyncio.run(main())
