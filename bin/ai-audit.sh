#!/bin/bash
set -euo pipefail
# Local Audit Script
pbpaste | lms prompt --model deepseek-r1-70b "Review this logic for bugs. You are running on an M3 Max with 128GB RAM. Be thorough."
