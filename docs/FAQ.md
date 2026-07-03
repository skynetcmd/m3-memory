# M3 Memory FAQ

## Windows Focus-Stealing Issues

### Q: Why do blank command prompt windows keep popping up and stealing focus?
**A:** Older installs registered the background scheduled tasks (like `AgentOS_ObservationDrain` or `AgentOS_ChatlogEmbedSweep`) to run through `cmd.exe`. `cmd.exe` is a console app, so Windows draws a window every time a task fires.

### Q: How do I fix it?
**A:** Run the fix script. It self-elevates — you can start it from a normal terminal and accept the UAC prompt:

```powershell
powershell -ExecutionPolicy Bypass -File bin\fix_scheduled_tasks.ps1
```

It re-registers every `AgentOS_*` task to run with `pythonw.exe` instead. `pythonw.exe` has no console subsystem, so the tasks run completely invisibly. The script prints a before/after summary so you can confirm the switch.

If you prefer to run it yourself in an **Administrator** terminal, the script just wraps this:
```powershell
python bin/install_schedules.py --repair
```

> The older "Hidden" trick (`Set-ScheduledTask ... -Hidden`) does **not** work for this — it only hides the task's row in the Task Scheduler UI, not the console window.

**macOS / Linux:** not affected — cron jobs never draw a window.

## General

### Q: Where are the logs located?
**A:** Logs are stored in the `logs/` directory at the project root.

### Q: My chat history is in the main memory DB instead of a separate chatlog DB. How do I split them?
**A:** This happens after switching from an integrated layout (chatlog sharing
the main DB) to separate files — repointing the path only routes new turns.
Move the existing rows with `bin/split_chatlog_from_core.py` (dry-run by default,
`--commit` to execute). Full steps, including repointing the hooks so it sticks,
are in [docs/CHATLOG.md → Troubleshooting](CHATLOG.md#8-troubleshooting).
