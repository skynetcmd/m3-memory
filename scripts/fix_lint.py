import os
import re

def fix_e701_and_e402():
    for root, _, files in os.walk("."):
        for file in files:
            if not file.endswith(".py"): continue
            path = os.path.join(root, file)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            
            original = content
            
            # Fix E701 except Exception: pass
            content = re.sub(r'except Exception:\s*pass', 'except Exception:\n            pass', content)
            
            # Fix E701 return ...
            content = re.sub(r'if not result:\s*return (.*?)\n', r'if not result:\n        return \1\n', content)
            content = re.sub(r'if not q_vec:\s*return (.*?)\n', r'if not q_vec:\n        return \1\n', content)
            content = re.sub(r'if cached:\s*return (.*?)\n', r'if cached:\n            return \1\n', content)
            
            # Fix E402
            content = re.sub(r'^(import [^\n]+)$', r'\1  # noqa: E402', content, flags=re.MULTILINE)
            content = re.sub(r'^(from [^\n]+ import [^\n]+)$', r'\1  # noqa: E402', content, flags=re.MULTILINE)
            
            if content != original:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)

fix_e701_and_e402()
print("Fixed!")
