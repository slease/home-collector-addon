#!/usr/bin/with-contenv bashio
# with-contenv puts SUPERVISOR_TOKEN in the environment (a plain #!/bin/sh would not see it).
# No bashio log line (1.0.20): collector.py's own first line says it started, and every line it writes starts with the Central time.
exec python3 -I /app/collector.py
