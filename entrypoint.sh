#!/bin/bash
# Run json_receive.py in background
python json_receive.py &

# Run robot.py in foreground — if it crashes, the container exits
# and Docker restarts it automatically (restart: unless-stopped)
exec python robot.py
