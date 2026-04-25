---
name: m3:install
description: Install or upgrade the m3-memory CLI + system payload.
---

# m3-memory install / upgrade

Step 1 — check current state:

```
!`command -v mcp-memory >/dev/null 2>&1 && mcp-memory --version || echo "not installed"`
```

Step 2 — interpret:

- Output **"not installed"**: tell the user to run the one-line installer in their shell:
  ```
  curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
  ```
  After it completes, they should re-run `/m3:doctor` to verify.

- Output a version like `m3-memory 2026.4.X.X`: it's already installed. Offer to upgrade:
  ```
  !`pipx upgrade m3-memory 2>&1`
  !`mcp-memory update`
  ```

Step 3 — after either path, run `/m3:doctor` to confirm the result.
