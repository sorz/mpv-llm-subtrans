local utils = require 'mp.utils'

local options = {
    model = "",  -- leave empty to use default model (gpt-4o-mini or deepseek-chat)
    key = "", -- leave empty to read from environment variable OPENAI_API_KEY
    base_url = "", -- leave empty to guess from key (OpenAI or DeekSeek)
    python_bin = "", -- path to python, leave empty to find `python3` & `py` from PATH
    ffmpeg_bin = "ffmpeg", -- path to ffmpeg execute
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
        print(python_bin, "-m openai --version:", ret.status)
        return false
    end
    print("openai found:", ret.stdout:gsub("%s+$", ""))
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
        print(bin, "exit with", ret.status)
        return false
    end
    print("ffmpeg found:", ret.stdout:match("^([^-]+)"))
    return true
end

function llm_subtrans_translate()
    print("Start subtitle tranlsate")

    -- check python
    local python_bin = options.python_bin
    if python_bin == "" then
        -- try to find on PATH
        for _, bin in ipairs({"python3", "py"}) do
            local ok, _ = check_python_version(bin)
            if ok then
                print("Python found as", bin)
                python_bin = bin
                break
            end
        end
        if python_bin == "" then
            mp.osd_message("Python not found")
            return
        end
    else
        local ok, err = check_python_version(python_bin)
        if not ok then
            mp.osd_message("Python not working: " .. err)
        end
    end

    -- check python-openai
    local ok, _ = check_python_openai(python_bin)
    if not ok then
        mp.osd_message("Python module `openai` not found")
        return
    end

    -- check ffmpeg
    if not check_ffmpeg(options.ffmpeg_bin) then
        mp.osd_message("`ffmpeg` not found")
        return
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
        mp.osd_message("API key not found")
        return
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
        mp.osd_message("No source substitle found")
        return
    end
    print("Select substitle track#" .. sub_track.id, sub_track.title)

    -- gather metadata
    -- TODO: check video url protocol
    local video_url = mp.get_property("path")

    -- execute subtrans.py
    local script_dir = mp.get_script_directory()
    if script_dir == nil then
        mp.osd_message("Script not install as directory")
        return
    end
    local py_script = script_dir .. "subtrans.py"
    local args = {
        python_bin, py_script,
        "--key", key:sub(1, -32) .. "********", -- reset after being log
        "--model", options.model,
        "--base-url", options.base_url,
        "--ffmpeg-bin", options.ffmpeg_bin,
        "--video-url", video_url,
        "--sub-track-id", sub_track.id - 1 .. "",
    }
    print("Execute", utils.format_json(args))
    args[4] = key
    local ret = mp.command_native({
        name="subprocess",
        args=args,
        playback_only=false,
    })
    print(utils.format_json(ret))

end



require "mp.options".read_options(options, "llm_subtrans")
mp.add_key_binding('alt+t', llm_subtrans_translate)

