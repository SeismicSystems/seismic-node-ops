local jwt = require "resty.jwt"

local secret_file, secret_error = io.open("/etc/seismic/openresty-jwt-secret", "r")
if not secret_file then
    ngx.log(ngx.ERR, "could not open JWT secret: ", secret_error)
    return ngx.exit(500)
end
local JWT_SECRET = secret_file:read("*a"):gsub("%s+$", "")
secret_file:close()
if JWT_SECRET == "" then
    ngx.log(ngx.ERR, "JWT secret is empty")
    return ngx.exit(500)
end

local auth_header = ngx.req.get_headers()["Authorization"]
if not auth_header then
    ngx.status = 401
    ngx.header["Content-Type"] = "application/json"
    ngx.say('{"error": "Missing Authorization header"}')
    return ngx.exit(401)
end

local token = auth_header:match("Bearer (.+)")
if not token then
    ngx.status = 401
    ngx.header["Content-Type"] = "application/json"
    ngx.say('{"error": "Invalid Authorization format, expected: Bearer <token>"}')
    return ngx.exit(401)
end

local verified = jwt:verify(JWT_SECRET, token)
if not verified.verified then
    ngx.status = 401
    ngx.header["Content-Type"] = "application/json"
    ngx.say('{"error": "Invalid token: ' .. (verified.reason or "unknown") .. '"}')
    return ngx.exit(401)
end
