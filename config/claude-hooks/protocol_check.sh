#!/bin/bash
# Injected as context before every user message (UserPromptSubmit hook).
# Keeps Claude aware of all 5 mandatory protocols regardless of session origin.
echo "[ACTIVE PROTOCOLS] (1) Decision Rule: call log_activity(category=decision) via custom_pc_tool after every agreed change — immediately, not batched. (2) Focus Protocol: call update_focus every 3 technical turns. (3) Search Rule: query project_decisions in agent_memory.db before any new task. (4) Reasoning Rule: archive DeepSeek-R1 think blocks via log_activity(category=thought). (5) Hardware Rule: log thermal/RAM changes to hardware category."
