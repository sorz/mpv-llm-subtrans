#!/usr/bin/env python3
import re
import logging
import argparse
import itertools
from dataclasses import dataclass
from pathlib import Path
from subprocess import Popen, PIPE
from typing import Iterator, Optional

from openai import OpenAI


RE_KEY_OPENAI = r"sk-\w+T3BlbkFJ\w+"  # T3BlbkFJ = base64("OpenAI")
RE_KEY_DEEPSEEK = r"TODO"
PROMPT = """
User will input content of SubRip (SRT) subtitles, with timestamp lines \
removed to save tokens. You need to translate these dialogues into \
{dest_lang}, and return in the same SRT-like format.

For reference, filename of this video is "{filename}".
"""


@dataclass
class PlatformOpts:
    model: str
    base_url: Optional[str] = None


OPENAI_DEFAULT = PlatformOpts("gpt-4o-mini")
DEEPSEEK_DEFAULT = PlatformOpts(
    model="deepseek-chat", base_url="https://api.deepseek.com/v1"
)


@dataclass
class Args:
    key: str
    model: str
    base_url: str
    ffmpeg_bin: str
    video_url: str
    sub_track_id: int
    dest_lang: str
    batch_size: int


@dataclass
class SubtitleText:
    """Separate text & font from each subtitle text line.
    <font> are commonly wrapped around every lines for ASS-converted SubRip.
    Trimming out them saves a lot of tokens.
    """
    text: str
    font: Optional[str] = None

    @staticmethod
    def parse(raw_text: str) -> "SubtitleText":
        # Only handle the most common case (one <font> for the entire line).
        # Others won't eat many tokens if they are rare anyway.
        match = re.match(r"<font ([^>]+)>(.+)</font>", raw_text)
        if match is None:
            return SubtitleText(raw_text)
        return SubtitleText(font=match.group(1), text=match.group(2))

    def __str__(self) -> str:
        if self.font is None:
            return self.text
        else:
            return f"<font {self.font}>{self.text}</font>"


@dataclass
class SubtitleLine:
    seq: int
    time_line: str
    text_lines: list[SubtitleText]
            
    def format_tiny(self) -> str:
        """SRT dialogus without timestamp line or font tag"""
        body = "\n".join(l.text for l in self.text_lines)
        return f"{self.seq}\n{body}"
    
    def format_full(self) -> str:
        """SRT dialogus with timestamp and font tag"""
        body = "\n".join(f"{l}" for l in self.text_lines)
        return f"{self.seq}\n{self.time_line}\n{body}"


def extract_subtitle(
    ffmpeg_bin: str, video_url: str, sub_track_id: int
) -> Iterator[SubtitleLine]:
    args = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        video_url,
        "-map",
        f"0:s:{sub_track_id}",
        "-f",
        "srt",
        "-",
    ]
    logging.info("Execute %s", " ".join(args))
    with Popen(args, stdout=PIPE, encoding="utf-8") as proc:
        assert proc.stdout is not None
        seq = None
        time_line = None
        text_lines = []
        for line in proc.stdout:
            line = line.strip()
            # parse seq
            if seq is None:
                if line.isdigit():
                    seq = int(line)
                else:
                    logging.warning("expect seq num, found `%s`", line)
                continue
            # parse time line
            if time_line is None:
                if "-->" in line:
                    time_line = line
                else:
                    logging.warning("expect time, found `%s`", line)
                continue
            # parse text lines
            if not line:
                # dialogue end
                yield SubtitleLine(seq, time_line, text_lines)
                seq = None
                time_line = None
                text_lines = []
            else:
                text_lines.append(SubtitleText.parse(line))


def translate_subtitle(
    openai: OpenAI,
    model: str,
    batch_size: int,
    dest_lang: str,
    filename: str,
    lines: Iterator[SubtitleLine],
) -> Iterator[SubtitleLine]:
    pass  # TODO

def main():
    parser = argparse.ArgumentParser(
        prog="mpv-llm-subtrans",
        description="MPV plugin for translating subtitles with LLM",
    )
    parser.add_argument("--key", required=True, help="API key")
    parser.add_argument("--model", required=True, help="Model name")
    parser.add_argument("--base-url", required=True, help="API base URL")
    parser.add_argument("--ffmpeg-bin", required=True, help="ffmpeg execute path")
    parser.add_argument("--video-url", required=True, help="video file path")
    parser.add_argument(
        "--sub-track-id",
        required=True,
        type=int,
        help="track id of subtitle, start from 0",
    )
    parser.add_argument("--dest-lang", required=True, help="Destination language")
    parser.add_argument("--batch-size", type=int, default=100)
    args = Args(**vars(parser.parse_args()))

    # Extract subtitle with ffmpeg (async)
    subtitle_lines = extract_subtitle(
        args.ffmpeg_bin, args.video_url, args.sub_track_id
    )

    # Build OpenAI client
    if re.fullmatch(RE_KEY_OPENAI, args.key):
        base_url = OPENAI_DEFAULT.base_url
        model = OPENAI_DEFAULT.model
    elif re.fullmatch(RE_KEY_DEEPSEEK, args.key):
        base_url = DEEPSEEK_DEFAULT.base_url
        model = DEEPSEEK_DEFAULT.model
    else:
        base_url = None
        model = None
    if args.model:
        model = args.model
    if args.base_url:
        base_url = args.base_url
    if model is None:
        raise ValueError("No model specified")
    openai = OpenAI(api_key=args.key, base_url=base_url)

    translated = translate_subtitle(
        openai=openai,
        model=model, 
        batch_size=args.batch_size,
        dest_lang=args.dest_lang,
        filename=Path(args.video_url).stem,
        lines=subtitle_lines
    )
    for line in translated:
        print(line)


if __name__ == "__main__":
    main()
