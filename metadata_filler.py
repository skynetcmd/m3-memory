import sqlite3
import json
import re
import os

DB_PATH = "memory/agent_memory.db"

def extract_metadata(title, content, type_str):
    # Base structure
    meta = {
        "tags": []
    }
    
    # 1. Tags from Type
    if type_str == "local_device":
        meta["tags"].append("device")
        meta["tags"].append("inventory")
    elif type_str == "network_config":
        meta["tags"].append("network")
        meta["tags"].append("config")
    elif type_str == "infrastructure":
        meta["tags"].append("infrastructure")
    elif type_str == "home_automation":
        meta["tags"].append("iot")
        meta["tags"].append("smarthome")
        
    # 2. Tags from Content/Title Context
    content_lower = content.lower()
    title_lower = title.lower()
    
    if "proxmox" in content_lower or "proxmox" in title_lower:
        meta["tags"].append("proxmox")
    if "unifi" in content_lower or "unifi" in title_lower or "usw" in title_lower or "uxg" in title_lower:
        meta["tags"].append("unifi")
        meta["tags"].append("ubiquiti")
    if "ceph" in content_lower or "ceph" in title_lower:
        meta["tags"].append("ceph")
    if "homekit" in content_lower or "homekit" in title_lower:
        meta["tags"].append("homekit")
    if "eufy" in content_lower or "eufy" in title_lower:
        meta["tags"].append("eufy")
    if "hubitat" in content_lower or "hubitat" in title_lower:
        meta["tags"].append("hubitat")
    if "hue" in content_lower or "hue" in title_lower:
        meta["tags"].append("hue")

    # Deduplicate tags
    meta["tags"] = list(set(meta["tags"]))
        
    # 3. IP Address Extraction
    ip_match = re.search(r'\bIP:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})\b', content, re.IGNORECASE)
    if not ip_match:
        ip_match = re.search(r'\b(?:at |IP )([0-9]{1,3}(?:\.[0-9]{1,3}){3})\b', content)
    if ip_match:
        meta["ip_address"] = ip_match.group(1)

    # 4. MAC Address Extraction
    mac_match = re.search(r'\bMAC:\s*([0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2})\b', content, re.IGNORECASE)
    if mac_match:
        meta["mac_address"] = mac_match.group(1).lower()

    # 5. Model/Firmware Extraction
    model_match = re.search(r'\bModel:\s*([^\s\|]+)\b', content)
    if model_match:
        meta["model"] = model_match.group(1)
        
    fw_match = re.search(r'\bFirmware:\s*([^\|\n]+)', content)
    if fw_match:
        meta["firmware"] = fw_match.group(1).strip()
        
    # 6. VLAN Extraction
    vlan_match = re.search(r'\bVLAN:\s*([^\|\n]+)', content)
    if vlan_match:
        meta["vlan"] = vlan_match.group(1).strip()

    return meta

def main():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, type, title, content, metadata_json FROM memory_items WHERE type NOT IN ('conversation', 'message', 'thought') AND is_deleted = 0")
    rows = cursor.fetchall()
    
    updated_count = 0
    
    for row in rows:
        item_id, item_type, title, content, existing_meta_str = row
        title = title or ""
        content = content or ""
        
        # Load existing meta
        try:
            existing_meta = json.loads(existing_meta_str) if existing_meta_str else {}
        except:
            existing_meta = {}
            
        # Ensure it's a dict
        if not isinstance(existing_meta, dict):
            existing_meta = {}
            
        # Parse new info
        new_meta = extract_metadata(title, content, item_type)
        
        # Merge tags safely
        existing_tags = existing_meta.get("tags", [])
        if not isinstance(existing_tags, list):
            existing_tags = []
        combined_tags = list(set(existing_tags + new_meta.pop("tags", [])))
        
        # Merge dictionaries (new info overwrites old, but preserves existing keys)
        updated_meta = {**existing_meta, **new_meta}
        updated_meta["tags"] = combined_tags
        
        # Serialize back
        final_meta_str = json.dumps(updated_meta)
        
        if final_meta_str != existing_meta_str:
            cursor.execute("UPDATE memory_items SET metadata_json = ? WHERE id = ?", (final_meta_str, item_id))
            updated_count += 1

    conn.commit()
    conn.close()
    
    print(f"Successfully processed and updated metadata for {updated_count} items.")

if __name__ == '__main__':
    main()
