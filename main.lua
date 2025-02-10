local utils = require 'mp.utils'
local msg = require 'mp.msg'

local options = {
    dest_lang = "", -- the language you want, default to guess with system's language
    key = "", -- default to read from environment variable OPENAI_API_KEY
    model = "",  -- default to use default model (gpt-4o-mini or deepseek-chat)
    base_url = "", -- default to guess from key (OpenAI or DeekSeek)
    python_bin = "", -- path to python, default to find `python3` & `py` from PATH
    ffmpeg_bin = "ffmpeg", -- path to ffmpeg execute
    batch_size = 50, -- number of dialogous send in one translate request
    output_dir = "~~cache/llm_subtrans_subtitles", -- where to put translated srt files
    skip_env_check = false, -- fast start, skip prerequisites checking
}

local ASS_COLOR_RED = "{\\c&H8899FF&}"
local ASS_COLOR_GREEN = "{\\c&H99FF88&}"
local IS_WINDODWS = mp.get_property("vo-mmcss-profile") ~= nil  -- Windows only property

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

local function get_api_key()
    local key = options.key
    if key == "" then
        local env = utils.get_env_list()
        for _, kv in ipairs(env) do
            local v = kv:match("^OPENAI_API_KEY=([%w%-]+)$")
            if v ~= nil then
                return v
            end
        end
        return nil
    else
        return key
    end
end

local function find_python_bin()
    local bins = {"python3", "python"}
    if IS_WINDODWS then
        table.insert(bins, 1, "py")
    end
    for _, bin in ipairs(bins) do
        local ok, _ = check_python_version(bin)
        if ok then
            msg.info("Python found as", bin)
            return bin
        end
    end
    return nil
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
    local function show(msg)
        ov.data = "{\\b1}{\\fs32}LLM SubTrans{\\b0} - " .. msg
        ov:update()
    end
    local function remove_ov(delay_secs)
        if delay_secs == nil or delay_secs == 0 then
            ov:remove()
        else
            mp.add_timeout(delay_secs, function ()
                ov:remove()
            end)
        end
    end
    show("checking")

    -- function to reset state
    local timer = nil
    local rpc_file = nil
    local function abort(error)
        if error ~= nil then
            msg.warn("Translate abort:", error)
            show(ASS_COLOR_RED .. error)
            remove_ov(5)
        else
            remove_ov(3)
        end
        running = false
        py_handle = nil
        if timer ~= nil then
            timer:kill()
            timer = nil
        end
        if rpc_file ~= nil then
            rpc_file:close()
        end
    end

    -- check python
    ---@type string|nil
    local python_bin= options.python_bin
    if python_bin == "" then
        if options.skip_env_check then
            -- just guess without checking
            if IS_WINDODWS then
                python_bin = "py"
            else
                python_bin = "python3"
            end
        else
            -- guess & checking
            python_bin = find_python_bin()
            if python_bin == nil then
                return abort("Python not found")
            end
        end
    elseif not options.skip_env_check then
        local ok, err = check_python_version(python_bin)
        if not ok then
            return abort("Python not working: " .. err)
        end
    end

    if not options.skip_env_check then
        -- check python-openai
        local ok, _ = check_python_openai(python_bin)
        if not ok then
            return abort("Python module `openai` not found")
        end

        -- check ffmpeg
        if not check_ffmpeg(options.ffmpeg_bin) then
            return abort("`ffmpeg` not found")
        end
    end

    -- check key, the only required option
    local key = get_api_key()
    if key == nil then
        return abort("API key not found")
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

    local ext_sub_url = ""
    if sub_track["external"] then
        ext_sub_url = sub_track["external-filename"]
        msg.info("External substitle " .. ext_sub_url)
        if not ext_sub_url:match("%.srt$") then
            -- TODO: support ass subtitle?
            return abort("only support SubRip (.srt) for external subtitles")
        end
        if ext_sub_url:match("^https?://") then
            -- TODO: support http substitle?
            return abort("only support external subtitles from local file")
        end
    end

    -- gather metadata
    -- TODO: check video url protocol
    local video_url = mp.get_property("path")

    -- set file path
    show("initializing")
    local output_dir = mp.command_native({"expand-path", options.output_dir})
    local srt_path = output_dir .. "/" .. mp.get_property("filename/no-ext") .. ".srt"
    msg.info("Save file to", srt_path)

    -- set ipc file
    local ipc_path = output_dir .. "/.progress"
    os.remove(ipc_path)
    local function read_panic_msg()
        -- read {panic: "msg"} from ipc file
        local ipc = io.open(ipc_path, "r");
        if ipc == nil then return nil end
        local state = utils.parse_json(ipc:read("*a"))
        if state == nil then return nil end
        return state["panic"]
    end

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
        "--subtitle-url", ext_sub_url,
        "--sub-track-id", sub_track.id - 1 .. "",
        "--batch-size", options.batch_size .. "",
        "--dest-lang", options.dest_lang,
        "--output-path", srt_path,
        "--ipc-path", ipc_path,
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
            show(ASS_COLOR_RED .. "cancelled")
            return abort()
        end
        if result.status ~= 0 then
            local panic = read_panic_msg()
            if panic ~= nil then
                return abort(panic)
            else
                return abort("script exit with " .. result.status .. " " .. result.error_string)
            end
        end
        mp.command_native({name="sub-reload"})
        show(ASS_COLOR_GREEN .. "all done")
        abort()
    end)

    -- monitor output file
    local CHECK_INTERVAL_SECS = 3
    local last_progress = nil
    timer = mp.add_periodic_timer(CHECK_INTERVAL_SECS, function ()
        -- open rpc file
        if rpc_file == nil then
            rpc_file = io.open(ipc_path, "r")
            if rpc_file == nil then return end
            show("waiting")
        end
        -- read progress from rpc file
        rpc_file:seek("set")
        local progress = utils.parse_json(rpc_file:read("*a"))
        if progress == nil then return end -- ignore parse error
        -- check if progress got updated
        if last_progress ~= nil and
            last_progress["last_seq"] >= progress["last_seq"]
        then return end
        msg.info("Progress: " .. utils.format_json(progress))

        -- set/reload subtitle
        if last_progress == nil then
            -- first update, active substitles now
            msg.info("Set tranlsated substitles")
            mp.command_native({
                name="sub-add",
                url=srt_path,
                title="Translated",
            })
            last_progress = progress
        else
            -- only reload when necessary
            local old_sub_end_pos = last_progress["last_timestamp_millis"][2]
            local new_sub_start_pos = progress["last_timestamp_millis"][1]
            local pos = mp.get_property_native("time-pos", 0) * 1000
            -- condition 1/2: run out of dialogous
            if old_sub_end_pos - pos < CHECK_INTERVAL_SECS * 2 * 1000 then
                -- condition 2/2: new file coverd current play position
                if new_sub_start_pos > pos then
                    msg.info("Reload translated subtitles")
                    mp.command_native({name="sub-reload"})
                    last_progress = progress
                end
            end
        end

        -- update progress
        local total_sec = mp.get_property_native("duration/full", nil)
        local pos_sec = progress["last_timestamp_millis"][2] / 1000
        if total_sec == nil then
            show("translating")
        elseif pos_sec >= total_sec then
            show("finishing")
        else
            show(string.format("%d%%", pos_sec / total_sec * 100))
        end
    end)

end

require "mp.options".read_options(options, "llm_subtrans")
mp.add_key_binding('alt+t', llm_subtrans_translate)

