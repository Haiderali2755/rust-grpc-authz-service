-- Sliding-window rate limiter, evaluated atomically inside Redis.
--
-- Doing this as one script rather than GET-then-SET matters: with separate
-- commands, N concurrent requests can each read the same count and all decide
-- they are under the limit, letting the caller exceed it. Redis runs scripts
-- atomically, so the increment and the decision cannot interleave.
--
-- KEYS[1] = counter key (per subject+route)
-- ARGV[1] = window length in ms
-- ARGV[2] = max units per window
-- ARGV[3] = units to consume
--
-- Returns: {allowed (0|1), remaining, pttl_ms}

local window = tonumber(ARGV[1])
local limit  = tonumber(ARGV[2])
local cost   = tonumber(ARGV[3])

local current = tonumber(redis.call('GET', KEYS[1])) or 0

if current + cost > limit then
  local ttl = redis.call('PTTL', KEYS[1])
  if ttl < 0 then ttl = window end
  return {0, limit - current, ttl}
end

local newval = redis.call('INCRBY', KEYS[1], cost)

-- Set the expiry only when the window opens, so the window is fixed from the
-- first request rather than sliding forward on every hit.
if newval == cost then
  redis.call('PEXPIRE', KEYS[1], window)
end

local ttl = redis.call('PTTL', KEYS[1])
if ttl < 0 then ttl = window end

return {1, limit - newval, ttl}
