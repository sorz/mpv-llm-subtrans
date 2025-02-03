local utils = require 'mp.utils'
local msg = require 'mp.msg'

local options = {
    dest_lang = "English", -- the language you want
    key = "", -- default to read from environment variable OPENAI_API_KEY
    model = "",  -- default to use default model (gpt-4o-mini or deepseek-chat)
    base_url = "", -- default to guess from key (OpenAI or DeekSeek)
    python_bin = "", -- path to python, default to find `python3` & `py` from PATH
    ffmpeg_bin = "ffmpeg", -- path to ffmpeg execute
    batch_size = 50, -- number of dialogous send in one translate request
    output_dir = "", -- where to put translated srt files, default to "<SCRIPT_DIR>/translated/"
    -- TODO: use ~~cache/llm_subtrans_subtitles/
}

local function check_python_version(bin)
    local ret = mp.command_native({
        name="subprocess",
        args={bin, "-V"},
        playback_only=false,
        capture_stdout=true,
    })
    if ret.status ~= 0 then
        if ret.error_string == "init" then
            return false, "cannot execute " .. bin
        else
            return false, bin .. " exit with error code " .. ret.status
        end
    end
    local ver1, ver2 = ret.stdout:match("^Python (%d+)%.(%d+)")
    if ver1 ~= "3" or tonumber(ver2) < 8 then
        return false, "Python version " .. ver1 .. "." .. ver2 .. " not supported"
    end
    return true
end

local function check_python_openai(python_bin)
    local ret = mp.command_native({
        name="subprocess",
        args={python_bin, "-m", "openai", "--version"},
        playback_only=false,
        capture_stdout=true,
    })
    if ret.status ~= 0 then
        msg.warn(python_bin, "-m openai --version:", ret.status)
        return false
    end
    msg.info("openai found:", ret.stdout:gsub("%s+$", ""))
    return true
end

local function check_ffmpeg(bin)
    local ret = mp.command_native({
        name="subprocess",
        args={bin, "-version"},
        playback_only=false,
        capture_stdout=true,
    })
    if ret.status ~= 0 then
        msg.warn(bin, "exit with", ret.status)
        return false
    end
    msg.info("ffmpeg found:", ret.stdout:match("^([^-]+)"))
    return true
end

local running = false
local py_handle = nil

function llm_subtrans_translate()
    -- check running
    if running then
        if py_handle ~= nil then
            msg.info("kill python script (user reuqest)")
            mp.abort_async_command(py_handle)
        else
            msg.info("already running")
        end
        return
    end
    msg.info("Start subtitle tranlsate")
    running = true

    -- show osd
    local ov = mp.create_osd_overlay("ass-events")
    ov.data = "{\\b1}{\\fs28}Subtitle translating…"
    ov:update()

    -- function to reset state
    local timer = nil
    local function abort(error)
        if error ~= nil then
            msg.warn("Translate abort:", error)
            mp.osd_message("Translate failed: " .. error)
        end
        ov:remove()
        running = false
        py_handle = nil
        if timer ~= nil then
            timer:kill()
            timer = nil
        end
    end

    -- check python
    local python_bin = options.python_bin
    if python_bin == "" then
        -- try to find on PATH
        for _, bin in ipairs({"python3", "py"}) do
            local ok, _ = check_python_version(bin)
            if ok then
                msg.info("Python found as", bin)
                python_bin = bin
                break
            end
        end
        if python_bin == "" then
            return abort("Python not found")
        end
    else
        local ok, err = check_python_version(python_bin)
        if not ok then
            return abort("Python not working: " .. err)
        end
    end

    -- check python-openai
    local ok, _ = check_python_openai(python_bin)
    if not ok then
        return abort("Python module `openai` not found")
    end

    -- check ffmpeg
    if not check_ffmpeg(options.ffmpeg_bin) then
        return abort("`ffmpeg` not found")
    end

    -- check key, the only required option
    local key = options.key
    if key == "" then
        local env = utils.get_env_list()
        for _, kv in ipairs(env) do
            local v = kv:match("^OPENAI_API_KEY=([%w%-]+)$")
            if v ~= nil then
                key = v
                break
            end
        end
    end
    if key == "" then
        return abort("API key not found")
    end

    -- check dest_lang
    if options.dest_lang == "" then
        return abort("dest_lang cannot be empty")
    end

    -- select subtitle track
    local sub_track = mp.get_property_native("current-tracks/sub")
    if sub_track == nil then
        -- find first subtitle track
        local tracks = mp.get_property_native("track-list")
        for _, track in ipairs(tracks) do
            if track.type == "sub" then
                sub_track = track
                break
            end
        end
    end
    if sub_track == nil then
        return abort("no source substitle found")
    end
    msg.info("Select substitle track#" .. sub_track.id, sub_track.title)

    -- gather metadata
    -- TODO: check video url protocol
    local video_url = mp.get_property("path")

    -- set output path
    local output_path = options.output_dir
    if output_path == "" then
        output_path = mp.get_script_directory() .. "translated"
    end
    output_path = output_path .. "/" .. mp.get_property("filename/no-ext") .. ".srt"
    msg.info("Save file to", output_path)

    -- execute subtrans.py
    local script_dir = mp.get_script_directory()
    if script_dir == nil then
        return abort("script not install as directory")
    end
    local py_script = script_dir .. "subtrans.py"
    local args = {
        python_bin, "-u", py_script,
        "--key", key:sub(1, -32) .. "********", -- reset after being log
        "--model", options.model,
        "--base-url", options.base_url,
        "--ffmpeg-bin", options.ffmpeg_bin,
        "--video-url", video_url,
        "--sub-track-id", sub_track.id - 1 .. "",
        "--batch-size", options.batch_size .. "",
        "--dest-lang", options.dest_lang,
        "--output-path", output_path,
    }
    msg.debug("Execute", utils.format_json(args))
    args[5] = key
    py_handle = mp.command_native_async({
        name="subprocess",
        args=args,
        playback_only=false,
    }, function (success, result, error)
        msg.debug("Python script exit:", utils.format_json(result))
        if not success then
            return abort("failed to execute command: " .. error)
        end
        if result.killed_by_us then
            mp.osd_message("Translate cancelled")
            return abort()
        end
        if result.status ~= 0 then
            return abort("script exit with " .. result.status .. " " .. result.error_string)
        end
        mp.osd_message("Substitle translate done")
        abort()
    end)

    -- monitor output file
    local output_file_size = 0
    timer = mp.add_periodic_timer(5, function ()
        local stats = utils.file_info(output_path)
        if stats == nil then
            return
        end
        if stats.size > output_file_size then
            msg.info("File updated:", stats.size, "bytes")
            if output_file_size == 0 then
                msg.info("Set tranlsated substitles")
                mp.command_native({
                    name="sub-add",
                    url=output_path,
                    title="Translated",
                })
            else
                msg.info("Reload translated subtitles")
                mp.command_native({name="sub-reload"})
                -- FIXME: broken when mpv find appending on srt
            end
            output_file_size = stats.size
        end
    end)

end

require "mp.options".read_options(options, "llm_subtrans")
mp.add_key_binding('alt+t', llm_subtrans_translate)

