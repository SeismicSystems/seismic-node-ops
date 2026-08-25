local limit_req = require "resty.limit.req"

local lim, err = limit_req.new("limit_req_store", RATE_LIMIT_RPS_PLACEHOLDER, RATE_LIMIT_BURST_PLACEHOLDER)
if not lim then
    ngx.log(ngx.ERR, "failed to instantiate limiter: ", err)
    return ngx.exit(500)
end

local key = ngx.var.binary_remote_addr
local delay, err = lim:incoming(key, true)
if not delay then
    if err == "rejected" then
        ngx.status = 429
        ngx.header["Content-Type"] = "application/json"
        ngx.say('{"error": "Too Many Requests"}')
        return ngx.exit(429)
    end
    ngx.log(ngx.ERR, "unexpected error from limiter: ", err)
    return ngx.exit(500)
end

if delay >= 0.001 then
    ngx.sleep(delay)
end
