#!/bin/bash
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1

echo "============================================================"
echo "  Keyword Cleaner - Variant Review GUI"
echo "============================================================"
echo

python3 gui_review.py "$@"
