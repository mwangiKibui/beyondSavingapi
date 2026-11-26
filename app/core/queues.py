# Single source of truth for Redis queue names - the statement-upload
# API (ab-37, producer) and the parse-job worker (ab-38, consumer) both
# import this rather than each hardcoding the same string.
PARSE_JOBS_QUEUE = "parse_jobs"

# ab-336: AI category lookups for an import's business payees. The worker
# takes them alongside parse jobs; AI_PENDING_KEY marks one queued or
# running (the review shows "Looking up payees..." and refetches).
AI_CATEGORY_JOBS_QUEUE = "ai_category_jobs"
AI_PENDING_KEY = "ai_category_pending:{import_id}"
AI_PENDING_TTL_SECONDS = 600
# Set when a lookup finishes, so reopening the review doesn't queue it
# again straight away (names it couldn't answer wait for this to expire).
AI_DONE_KEY = "ai_category_done:{import_id}"
AI_DONE_TTL_SECONDS = 6 * 3600
