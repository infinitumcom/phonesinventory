#!/bin/bash
# Thin wrapper: restart only the API server (no test gate — revival path)
exec bash "$(dirname "$0")/start_bot.sh" --skip-tests --only-api
