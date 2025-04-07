# llm-subtrans

Translate video subtitles with OpenAI(-compatible) language models.
A [mpv player](https://mpv.io/) script.

## Features

- **Fast.** Streaming all the way. Translation began appearing within seconds.
- **Contextual.** Unlike traditional tools that translate sentence by sentence,
  we feed long dialogue histories and metadata of video to leverage the
  contextual understaning capabilities of LLM.
- **Easy.** Few commands to install. Just setup your API key. One shortcut to start.

Tested on Windows & Ubuntu, other platform should work but not get tested yet.

Both internal subtitle in video files and external subtitle files are supported.

**Internal** subtitles rely on ffmpeg and support **both SRT & ASS formats**.
HTTP(S) videos are supported, although it will be downloaded twice (one for
playback and one for extracting subtitles). We stream it too, so no worry if
you got a slow HTTP connection.

**External** subtitles currently only support local SRT files.

### Why you SHOULD NOT use it

This script was made for quick & convenience. If you need tweak the prompt,
manual adjustment or editing, transcription, etc., use dedicated tools.

Some styles of ASS subtitles will be lost.

## Prerequisites

- [FFmpeg](https://www.ffmpeg.org/)
- [OpenAI](https://platform.openai.com/api-keys) or [DeepSeek](https://platform.deepseek.com/api_keys) API key
- [uv](https://github.com/astral-sh/uv); or
  - [Python](https://python.org)
  - [openai-python](https://github.com/openai/openai-python)

## Quick start

### Windows

```powershell
# Before start, install Python & FFmpeg to PATH
py -m pip install openai
$env:OPENAI_API_KEY='sk-******'
mpv --script=.\mpv-llm-subtrans video.mp4
# Select the substitles you want to translate (if not the first one)
# Press Alt-T on mpv window
```

Note: if you have `uv` installed on Windows, make sure mpv is v0.39 or above.

### Ubuntu

```bash
sudo apt install ffmpeg python3-openai
export OPENAI_API_KEY='sk-******'
mpv --script=./mpv-llm-subtrans video.mp4
# Select the substitles you want to translate (if not the first one)
# Press Alt-T on mpv window
```

## Configurtion

See [llm_subtrans.conf](llm_subtrans.conf).

Put this file on `%APPDATA%\mpv\script-opts\` or `~/.config/mpv/script-opts/`.

## Tips

- The default key binding is `Alt+T`, press once to start,
  press again to cancel.
- You can watch while the translation is in progress. As long as the
  translation progess (displayed in the upper left corner) exceeds your
  playback progress, you will not miss a sentence.
- All translated subsitles can be found at
  `%LOCALAPPDATA%\mpv\cache\llm_subtrans_subtitles` or
  `~/.cache/mpv/llm_subtrans_subtitles`.
