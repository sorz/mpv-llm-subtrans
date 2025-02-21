#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "openai",
# ]
# ///
import re
import os
import json
import locale
import logging
import argparse
from pathlib import Path
from dataclasses import dataclass
from subprocess import Popen, PIPE
from typing import IO, Any, Iterator, Optional, TextIO, TypedDict

from openai import OpenAI


RE_KEY_OPENAI = r"sk-\w+T3BlbkFJ\w+"  # T3BlbkFJ = base64("OpenAI")
RE_KEY_DEEPSEEK = r"sk-[a-z0-9]{32}"
PROMPT_DEV = """\
User will input content of SubRip (SRT) subtitles, with timestamp lines \
removed to save tokens. You need to translate these dialogues into \
{dest_lang}, and return in the same format, plaintext without markdown. \
Keep formatting tags (e.g. <i>) not touch.

Metadata such as video name may prepend for reference. Do not include them \
in your response.

{extra_prompt}\
"""
PROMPT_USER = """\
Video file name: {video_name}
Subtitle file name: {subtitle_name}
------
{srt_content}
"""


@dataclass
class PlatformOpts:
    model: str
    base_url: Optional[str] = None


OPENAI_DEFAULT = PlatformOpts("gpt-4o-mini")
DEEPSEEK_DEFAULT = PlatformOpts(
    model="deepseek-chat", base_url="https://api.deepseek.com/v1"
)


@dataclass(frozen=True)
class SubtitleLine:
    seq: int
    time_line: str
    text_lines: list[str]

    def format_without_time(self) -> str:
        """SRT dialogus without timestamp line"""
        body = "\n".join(self.text_lines)
        return f"{self.seq}\n{body}"

    def format_full(self) -> str:
        """SRT dialogus with timestamp"""
        body = "\n".join(self.text_lines)
        return f"{self.seq}\n{self.time_line}\n{body}"

    def strip_font_tags(self) -> "SubtitleLine":
        return SubtitleLine(
            seq=self.seq,
            time_line=self.time_line,
            text_lines=[re.sub(r"<font[^>]+>|</font>", "", l) for l in self.text_lines],
        )

    @property
    def timestamp_millis(self) -> tuple[int, int]:
        matches = re.findall(r"((\d\d:){2}\d\d(,\d{1,3}))", self.time_line)
        ts = []
        for match, *_ in matches:
            h, m, s = match.split(":", 3)
            if "," in s:
                s, ms = s.split(",", 2)
            else:
                ms = "0"
            ts.append((((int(h) * 60) + int(m) * 60) + int(s)) * 1000 + int(ms))
        if len(ts) != 2:
            raise ValueError("malformated timestamp (%s)" % self.time_line)
        return ts[0], ts[1]


def parse_subtitle(input: IO[str]) -> Iterator[SubtitleLine]:
    seq = None
    time_line = None
    text_lines = []
    for line in input:
        line = line.strip()
        # parse seq
        if seq is None:
            try:
                seq = int(line)
            except ValueError as err:
                logging.warning("expect seq num, found `%s` (%s)", line, err)
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
            text_lines.append(line)


def extract_subtitle_from_video(
    ffmpeg_bin: str, video_url: str, sub_track_id: int
) -> Iterator[SubtitleLine]:
    args = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
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
        yield from parse_subtitle(proc.stdout)
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError(f"ffmpeg exit with {ret}")


def read_subtitle_from_srt(path: str) -> Iterator[SubtitleLine]:
    with open(path, encoding="utf-8") as f:
        yield from parse_subtitle(f)


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
    prompt_vars: dict[str, Any],
    lines: Iterator[SubtitleLine],
) -> Iterator[SubtitleLine]:
    prompt_dev = PROMPT_DEV.format(**prompt_vars)
    batch_count = 0
    batch = list(iter_take(lines, batch_size))
    while batch:
        translated_count = 0
        items = translate_subtitle_batch(openai, model, prompt_dev, prompt_vars, batch)
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
                raise err
        logging.info(
            "translated %s dialogous in batch#%s", translated_count, batch_count
        )
        if translated_count == 0:
            # prevent infinite loop, just in case
            raise RuntimeError("empty response from model")
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
        return SubtitleLine(seq, src.time_line, lines)

    def __bool__(self):
        return bool(self._line_buf)


def translate_subtitle_batch(
    openai: OpenAI,
    model: str,
    prompt_dev: str,
    prompt_vars: dict[str, Any],
    batch_lines: list[SubtitleLine],
) -> Iterator[SubtitleLine]:
    # send request
    user_prompt = PROMPT_USER.format(
        srt_content="\n\n".join(
            l.strip_font_tags().format_without_time() for l in batch_lines
        ),
        **prompt_vars,
    )
    stream = openai.chat.completions.create(
        model=model,
        stream=True,
        messages=[
            {"role": "developer", "content": prompt_dev},
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


@dataclass
class Args:
    model: str
    base_url: str
    ffmpeg_bin: str
    video_url: str
    sub_track_id: int
    subtitle_url: str
    dest_lang: str
    batch_size: int
    output_path: str
    ipc_path: str
    extra_prompt: str

    def build_openai_client(self) -> tuple[OpenAI, str]:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise KeyError("No OPENAI_API_KEY set")
        if re.fullmatch(RE_KEY_OPENAI, key):
            base_url = OPENAI_DEFAULT.base_url
            model = OPENAI_DEFAULT.model
        elif re.fullmatch(RE_KEY_DEEPSEEK, key):
            base_url = DEEPSEEK_DEFAULT.base_url
            model = DEEPSEEK_DEFAULT.model
        else:
            base_url = None
            model = None
        if self.model:
            model = self.model
        if model is None:
            raise ValueError("No model specified")
        if self.base_url:
            base_url = self.base_url
        return OpenAI(api_key=key, base_url=base_url), model

    @property
    def dest_lang_with_default(self) -> str:
        if not self.dest_lang:
            loc, _ = locale.getlocale()
            if loc is None or loc == "C":
                return "English"
            else:
                return loc
        else:
            return self.dest_lang

    @property
    def prompt_vars(self) -> dict[str, Any]:
        return dict(
            dest_lang=self.dest_lang_with_default,
            video_name=Path(self.video_url).stem,
            subtitle_name=Path(self.subtitle_url).stem,
            extra_prompt=self.extra_prompt,
        )


def get_cli_args() -> Args:
    parser = argparse.ArgumentParser(
        prog="mpv-llm-subtrans",
        description="MPV plugin for translating subtitles with LLM",
    )
    parser.add_argument("--model", default="", help="Model name")
    parser.add_argument("--base-url", default="", help="API base URL")
    parser.add_argument("--ffmpeg-bin", default="", help="ffmpeg execute path")
    parser.add_argument("--video-url", default="", help="video file path")
    parser.add_argument(
        "--sub-track-id",
        default=0,
        type=int,
        help="track id of subtitle, start from 0",
    )
    parser.add_argument(
        "--subtitle-url", default="", help="standalone subtitle file path"
    )
    parser.add_argument("--dest-lang", default="", help="Destination language")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--output-path", required=True)
    parser.add_argument("--ipc-path", required=True)
    parser.add_argument("--extra-prompt", default="")
    return Args(**vars(parser.parse_args()))


class Progress(TypedDict):
    last_seq: int
    last_timestamp_millis: tuple[int, int]


def process(args: Args, ipc: TextIO):
    openai, model = args.build_openai_client()
    logging.info("Target language: %s", args.dest_lang_with_default)
    logging.info("Model: %s", model)

    if args.subtitle_url:
        # Use standalone srt file
        subtitle_lines = read_subtitle_from_srt(args.subtitle_url)
    else:
        # Extract subtitle with ffmpeg (async)
        subtitle_lines = extract_subtitle_from_video(
            args.ffmpeg_bin, args.video_url, args.sub_track_id
        )

    # Translate (async)
    translated = translate_subtitle(
        openai=openai,
        model=model,
        batch_size=args.batch_size,
        prompt_vars=args.prompt_vars,
        lines=subtitle_lines,
    )

    # Write out
    srt_path = Path(args.output_path)
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    logging.info("Write to %s", srt_path.resolve())

    with srt_path.open("w", encoding="utf-8") as srt:
        for line in translated:
            srt.write(line.format_full())
            srt.write("\n\n")
            srt.flush()

            ipc.seek(0)
            json.dump(
                Progress(
                    last_seq=line.seq,
                    last_timestamp_millis=line.timestamp_millis,
                ),
                ipc,
            )
            ipc.truncate()


def main():
    logging.basicConfig(level=logging.INFO)
    args = get_cli_args()

    ipc_path = Path(args.ipc_path)
    ipc_path.parent.mkdir(parents=True, exist_ok=True)
    with ipc_path.open("w") as ipc:
        try:
            process(args, ipc)
        except Exception as err:
            json.dump(dict(panic=f"{err}"), ipc)
            raise err


if __name__ == "__main__":
    main()
