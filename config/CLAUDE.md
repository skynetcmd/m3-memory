# M3 MAX AGENTIC OS: CORE SPECS

## 🧠 Primary Engine: DeepSeek-R1 Distill 70B (MLX)
- **Quantization:** 5-bit | **Context:** 64k-128k
- **MCP Bridges:** local_logic, web_research, grok_intel, custom_pc_tool

## 📜 Operational Protocols (AUTO-LOGGING)
1. **The Reasoning Rule:** Whenever using 'local_logic', you MUST use 'log_activity(category="thought", ...)' to archive the <think> block if the reasoning is complex.
2. **The Hardware Rule:** If you detect a change in system RAM or thermal pressure via 'check_thermal_load', log it to 'hardware'.
3. **The Decision Rule:** Whenever the user agrees to a code change, a file move, or a project direction, log it to 'decision' immediately.
4. **The Search Rule:** Before starting a new task, query the 'project_decisions' table to see if there is relevant history.
5. The Focus Protocol: After every 3 turns of a technical conversation, use 'update_focus' to condense our current trajectory into a 10-word summary for the hardware dashboard.
