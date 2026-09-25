-- The real council CLI and observer send fixed-length CBOR bodies. Require
-- Content-Length at ingress: ngx.req.socket does not decode chunked requests.
-- Never log data, headers, parser diagnostics, or partial bodies.
local headers, header_error = ngx.req.get_headers(100)
if header_error then
    return ngx.exit(431)
end
if headers["transfer-encoding"] then
    return ngx.exit(400)
end
if headers["content-type"] ~= "application/cbor" then
    return ngx.exit(415)
end
if headers["upgrade"] then
    return ngx.exit(400)
end
local connection = headers["connection"]
if connection and (type(connection) ~= "string" or
    string.find(string.lower(connection), "upgrade", 1, true)) then
    return ngx.exit(400)
end
local length = tonumber(ngx.var.content_length)
if not length then
    return ngx.exit(411)
end
if length < 1 then
    return ngx.exit(400)
end
if length > 65536 then
    return ngx.exit(413)
end

local sock = ngx.req.socket()
if not sock then
    return ngx.exit(400)
end
-- Larger than the maximum body, including the exact-cap case: no disk spill.
ngx.req.init_body(131072)
sock:settimeout(5000)
-- Individual socket read timeouts do not bound the whole upload: completing
-- successive chunks can reset the budget. Race the reader with one total timer.
local reader = ngx.thread.spawn(function()
    local remaining = length
    while remaining > 0 do
        local data, err = sock:receive(math.min(remaining, 4096))
        if not data then
            return err == "timeout" and 408 or 400
        end
        ngx.req.append_body(data)
        remaining = remaining - #data
    end
    return 200
end)
local timer = ngx.thread.spawn(function()
    ngx.sleep(5)
    return 408
end)
local ok, status = ngx.thread.wait(reader, timer)
ngx.thread.kill(reader)
ngx.thread.kill(timer)
if not ok then
    return ngx.exit(500)
end
if status ~= 200 then
    return ngx.exit(status)
end
-- Nothing reaches tiny_http until the complete, bounded body is in memory.
ngx.req.finish_body()
