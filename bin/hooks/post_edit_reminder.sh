#!/bin/bash
# Fires after any Edit or Write tool call (PostToolUse hook).
# Enforces the Decision Rule by prompting Claude to log immediately.
echo "[DECISION RULE TRIGGERED] A file was just modified. If the user agreed to this change, call log_activity(category='decision', detail_a=<file/component>, detail_b=<what and why>, detail_c=<root cause>) via custom_pc_tool RIGHT NOW before continuing."
