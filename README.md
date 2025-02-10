# llm-subtrans

Translate video subtitles with OpenAI or DeepSeek's language model.
A [mpv player](https://mpv.io/) script.

## Features

- **Fast.** Streaming all the way. Translation began appearing within seconds.
- **Contextual.** Unlike traditional tools that translate sentence by sentence,
  we feed long dialogue histories and metadata of video to leverage the
  contextual understaning capabilities of LLM.
- **Easy.** Few commands to install. Just setup your API key. One shortcut to start.

Tested on Windows, other platform should work but not get tested yet.

Both internal subtitle in video files and external subtitle files are supported.

**Internal** subtitles rely on ffmpeg and support **both SRT & ASS formats**.
HTTP(S) videos are supported, although it will be downloaded twice (one for
playback and one for extracting subtitles). We stream it too, so no worry if
you got a slow HTTP connection.

**External** subtitles currently only support local SRT files.

### Why you SHOULD NOT use it

This script was made for quick & convenience. If you need tweak the prompt,
manual adjustment or editing, speech recognition, etc., use dedicated tools.

Some styles of ASS subtitles will be lost.

## Prerequisites

- [FFmpeg](https://www.ffmpeg.org/)
- [Python](https://python.org)
- [openai-python](https://github.com/openai/openai-python)
- [OpenAI](https://platform.openai.com/api-keys) or [DeepSeek](https://platform.deepseek.com/api_keys) API key

## Quick start

### Windows

```powershell
# Install Python & FFmpeg to PATH
py -m pip install openai
$env:OPENAI_API_KEY='sk-******'
mpv --script=.\mpv-llm-subtrans video.mp4
# Press Alt-T on mpv window
```

### Ubuntu

```bash
sudo apt install ffmpeg python3-openai
export OPENAI_API_KEY='sk-******'
mpv --script=./mpv-llm-subtrans video.mp4
# Press Alt-T on mpv window
```

## Configurtion

See [llm_subtrans.conf](llm_subtrans.conf)
