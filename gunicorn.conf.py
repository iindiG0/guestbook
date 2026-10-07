# gunicorn reads this file automatically when it starts.
import os

# Listen on every network interface, on the port Epinio gives us (default 8080).
# Without this, gunicorn listens on 127.0.0.1:8000 and Kubernetes can't reach it.
bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"

# 2 worker processes = 2 requests handled at the same time.
workers = 2

# Send access logs and error logs to the console, so they show in `kubectl logs`.
accesslog = "-"
errorlog = "-"
