#!/usr/bin/env python3
import re
import logging
import argparse
from dataclasses import dataclass
from pathlib import Path
from subprocess import Popen, PIPE
from typing import Iterator, Optional

from openai import OpenAI


RE_KEY_OPENAI = r"sk-\w+T3BlbkFJ\w+"  # T3BlbkFJ = base64("OpenAI")
RE_KEY_DEEPSEEK = r"TODO"
PROMPT = """\
User will input content of SubRip (SRT) subtitles, with timestamp lines \
removed to save tokens. You need to translate these dialogues into \
{dest_lang}, and return in the same format, plaintext without markdown. \
Keep formatting tags (e.g. <i>, <font>) not touch.

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


def iter_take[T](iter: Iterator[T], num: int) -> Iterator[T]:
    count = 0
    for item in iter:
        yield item
        count += 1
        if count >= num:
            return


def translate_subtitle(
    openai: OpenAI,
    model: str,
    batch_size: int,
    dest_lang: str,
    filename: str,
    lines: Iterator[SubtitleLine],
) -> Iterator[SubtitleLine]:
    dev_prompt = PROMPT.format(
        dest_lang=dest_lang,
        filename=filename,
    )
    batch_count = 0
    batch = list(iter_take(lines, batch_size))
    while batch:
        translated_count = 0
        items = translate_subtitle_batch(openai, model, dev_prompt, batch)
        try:
            for item in items:
                translated_count += 1
                yield item
        except Exception as err:
            logging.warning(
                "translate error at batch#%s line#%s: %s",
                batch_count,
                translated_count,
                err,
            )
            if translated_count == 0:
                logging.error("translate failure")
                break
        logging.info(
            "translated %s dialogous in batch#%s", translated_count, batch_count
        )
        batch = batch[translated_count:]
        batch.extend(iter_take(lines, batch_size - len(batch)))


class RespBuf:
    def __init__(self):
        # received but not yet parsed lines, end with "\n"
        self._line_buf: list[str] = []

    def put(self, raw: str):
        lines = raw.replace("\r", "").splitlines(keepends=True)
        if not self._line_buf or self._line_buf[-1].endswith("\n"):
            # new line(s) received
            self._line_buf.extend(lines)
        else:
            # last line cont'd
            self._line_buf[-1] += lines[0]
            self._line_buf.extend(lines[1:])
        # skip leading empty lines (just in case)
        if self._line_buf == ["\n"] or self._line_buf == [""]:
            self._line_buf.pop()

    def endswith_empty_line(self) -> bool:
        return bool(self._line_buf) and self._line_buf[-1] == "\n"

    def reset(self):
        self._line_buf = []

    def parse(self, src: SubtitleLine) -> SubtitleLine:
        # parse & check seq num
        try:
            seq = int(self._line_buf[0].strip())
        except ValueError as err:
            raise ValueError("failed to parse seq num", err)
        if seq != src.seq:
            raise ValueError(f"seq num mismatched (tx:{src.seq} != rx:{seq})")

        # parse & reformat translated text
        if self.endswith_empty_line():
            self._line_buf.pop()
        lines = [l.strip() for l in self._line_buf[1:]]
        if len(src.text_lines) != len(lines):
            logging.warning("text lines mismatched, formating ignored")
            text_lines = [SubtitleText(l) for l in lines]
        else:
            text_lines = [
                SubtitleText(text, orig.font)
                for orig, text in zip(src.text_lines, lines)
            ]
        return SubtitleLine(seq, src.time_line, text_lines)

    def __bool__(self):
        return bool(self._line_buf)


def translate_subtitle_batch(
    openai: OpenAI,
    model: str,
    dev_prompt: str,
    batch_lines: list[SubtitleLine],
) -> Iterator[SubtitleLine]:
    # send request
    user_prompt = "\n\n".join(l.format_tiny() for l in batch_lines)
    stream = openai.chat.completions.create(
        model=model,
        stream=True,
        messages=[
            {"role": "developer", "content": dev_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    # parse response
    orig = iter(batch_lines)
    buf = RespBuf()
    for chunk in stream:
        choice = chunk.choices[0]
        content = choice.delta.content
        if content:
            buf.put(content)
        match choice.finish_reason:
            case None:
                pass
            case "length":  # truncated response
                return
            case "stop":  # done, parse the last one
                if buf:
                    yield buf.parse(next(orig))
                return
            case _:
                logging.warning("unknown finish reason %s", choice.finish_reason)
                return
        if buf.endswith_empty_line():
            yield buf.parse(next(orig))
            buf.reset()


def main():
    logging.basicConfig(level=logging.INFO)
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
        lines=subtitle_lines,
    )
    for line in translated:
        print(line)  # TODO


if __name__ == "__main__":
    main()
