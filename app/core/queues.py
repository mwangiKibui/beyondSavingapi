# Single source of truth for Redis queue names - the statement-upload
# API (ab-37, producer) and the parse-job worker (ab-38, consumer) both
# import this rather than each hardcoding the same string.
PARSE_JOBS_QUEUE = "parse_jobs"
