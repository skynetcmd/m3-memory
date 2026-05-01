#!/usr/bin/env python3
"""
gen_mcp_inventory.py — Generates docs/MCP_TOOLS.md from mcp_tool_catalog and mcp_proxy.
"""
import os
import sys
import ast
import json

# Ensure we can import from bin
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp_tool_catalog

def extract_proxy_tools():
    proxy_path = os.path.join(os.path.dirname(__file__), "mcp_proxy.py")
    with open(proxy_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    protocol_tools = []
    debug_tools = []

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id == "PROTOCOL_TOOLS":
                        protocol_tools = ast.literal_eval(node.value)
                    elif target.id == "DEBUG_TOOLS":
                        debug_tools = ast.literal_eval(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                if node.target.id == "PROTOCOL_TOOLS":
                    protocol_tools = ast.literal_eval(node.value)
                elif node.target.id == "DEBUG_TOOLS":
                    debug_tools = ast.literal_eval(node.value)
    
    return protocol_tools, debug_tools

def get_category_map():
    # Hardcoded mapping based on docs/API_REFERENCE.md
    mapping = {
        # Memory Operations
        "memory_write": "Memory Operations",
        "memory_write_from_file": "Memory Operations",
        "memory_search": "Memory Operations",
        "memory_search_routed": "Memory Operations",
        "memory_suggest": "Memory Operations",
        "memory_get": "Memory Operations",
        "memory_update": "Memory Operations",
        "memory_delete": "Memory Operations",
        "memory_verify": "Memory Operations",
        "memory_feedback": "Memory Operations",

        # Knowledge Graph
        "memory_link": "Knowledge Graph",
        "memory_graph": "Knowledge Graph",
        "memory_history": "Knowledge Graph",
        "entity_search": "Knowledge Graph",
        "entity_get": "Knowledge Graph",
        "extract_pending": "Knowledge Graph",
        "enrich_pending": "Knowledge Graph",
        
        # Conversations
        "conversation_start": "Conversations",
        "conversation_append": "Conversations",
        "conversation_search": "Conversations",
        "conversation_summarize": "Conversations",
        
        # Task Management
        "task_create": "Task Management",
        "task_assign": "Task Management",
        "task_update": "Task Management",
        "task_delete": "Task Management",
        "task_set_result": "Task Management",
        "task_get": "Task Management",
        "task_list": "Task Management",
        "task_tree": "Task Management",
        
        # Agent Registry & Notifications
        "agent_register": "Agent Registry & Notifications",
        "agent_heartbeat": "Agent Registry & Notifications",
        "agent_list": "Agent Registry & Notifications",
        "agent_get": "Agent Registry & Notifications",
        "agent_offline": "Agent Registry & Notifications",
        "notify": "Agent Registry & Notifications",
        "notifications_poll": "Agent Registry & Notifications",
        "notifications_ack": "Agent Registry & Notifications",
        "notifications_ack_all": "Agent Registry & Notifications",
        
        # Multi-Agent Coordination
        "memory_handoff": "Multi-Agent Coordination",
        "memory_inbox": "Multi-Agent Coordination",
        "memory_inbox_ack": "Multi-Agent Coordination",
        "memory_refresh_queue": "Multi-Agent Coordination",
        
        # Chat Log System
        "chatlog_write": "Chat Log System",
        "chatlog_write_bulk": "Chat Log System",
        "chatlog_search": "Chat Log System",
        "chatlog_promote": "Chat Log System",
        "chatlog_list_conversations": "Chat Log System",
        "chatlog_cost_report": "Chat Log System",
        "chatlog_set_redaction": "Chat Log System",
        "chatlog_status": "Chat Log System",
        "chatlog_rescrub": "Chat Log System",
        
        # Lifecycle & Maintenance
        "memory_maintenance": "Lifecycle & Maintenance",
        "memory_dedup": "Lifecycle & Maintenance",
        "memory_consolidate": "Lifecycle & Maintenance",
        "memory_set_retention": "Lifecycle & Maintenance",
        
        # Data Governance
        "gdpr_export": "Data Governance",
        "gdpr_forget": "Data Governance",
        "memory_export": "Data Governance",
        "memory_import": "Data Governance",
        
        # Infrastructure Operations
        "memory_cost_report": "Infrastructure Operations",
        "chroma_sync": "Infrastructure Operations",
        
        # Proxy tools
        "log_activity": "Operational Protocol",
        "query_decisions": "Operational Protocol",
        "update_focus": "Operational Protocol",
        "retire_focus": "Operational Protocol",
        "check_thermal_load": "Operational Protocol",
        
        "debug_analyze": "Debug Agent",
        "debug_bisect": "Debug Agent",
        "debug_trace": "Debug Agent",
        "debug_correlate": "Debug Agent",
        "debug_history": "Debug Agent",
        "debug_report": "Debug Agent",
    }
    return mapping

def generate_markdown(all_tools):
    cat_map = get_category_map()
    
    # Sort tools by category then name
    categorized = {}
    for tool in all_tools:
        name = tool['name']
        cat = cat_map.get(name, "Uncategorized")
        if cat not in categorized:
            categorized[cat] = []
        categorized[cat].append(tool)
    
    # Sort categories to match API_REFERENCE order roughly
    cat_order = [
        "Memory Operations", "Knowledge Graph", "Conversations", "Task Management",
        "Agent Registry & Notifications", "Multi-Agent Coordination", "Chat Log System",
        "Operational Protocol", "Debug Agent", "Lifecycle & Maintenance",
        "Data Governance", "Infrastructure Operations"
    ]
    
    md = "# MCP Tool Inventory\n\n"
    md += f"This document provides a comprehensive inventory of all {len(all_tools)} MCP tools available in the M3 Memory system.\n\n"
    
    # Summary Table
    md += "## Summary Table\n\n"
    md += "| Name | Category | Description |\n"
    md += "| --- | --- | --- |\n"
    
    all_sorted = sorted(all_tools, key=lambda x: (cat_order.index(cat_map.get(x['name'], "Uncategorized")) if cat_map.get(x['name']) in cat_order else 99, x['name']))
    
    for tool in all_sorted:
        name = tool['name']
        cat = cat_map.get(name, "Uncategorized")
        desc = tool['description'].split('\n')[0] # First line
        md += f"| `{name}` | {cat} | {desc} |\n"
    
    md += "\n---\n\n"
    
    # Detailed Sections
    for cat in cat_order:
        if cat not in categorized:
            continue
        md += f"## {cat}\n\n"
        for tool in sorted(categorized[cat], key=lambda x: x['name']):
            name = tool['name']
            desc = tool['description']
            params = tool['parameters'].get('properties', {})
            required = tool['parameters'].get('required', [])
            source = tool.get('source', 'mcp_tool_catalog.py')
            
            md += f"### `{name}`\n\n"
            md += f"{desc}\n\n"
            md += "**Source:** " + source + "\n\n"
            
            if params:
                md += "**Parameters:**\n\n"
                md += "| Parameter | Type | Required | Description | Default |\n"
                md += "| --- | --- | --- | --- | --- |\n"
                for p_name, p_info in params.items():
                    p_type = p_info.get('type', 'any')
                    p_req = "Yes" if p_name in required else "No"
                    p_desc = p_info.get('description', '').replace('|', '\\|')
                    p_def = p_info.get('default', '-')
                    if p_def is None: p_def = "null"
                    md += f"| `{p_name}` | `{p_type}` | {p_req} | {p_desc} | `{p_def}` |\n"
                md += "\n"
            else:
                md += "No parameters.\n\n"
        md += "---\n\n"
        
    return md

def main():
    # 1. Catalog tools
    catalog_tools = []
    for t in mcp_tool_catalog.TOOLS:
        catalog_tools.append({
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
            "source": "mcp_tool_catalog.py"
        })
    
    # 2. Proxy tools
    protocol_tools, debug_tools = extract_proxy_tools()
    
    all_tools = catalog_tools
    for t in protocol_tools:
        f = t['function']
        all_tools.append({
            "name": f['name'],
            "description": f['description'],
            "parameters": f['parameters'],
            "source": "mcp_proxy.py (PROTOCOL_TOOLS)"
        })
    for t in debug_tools:
        f = t['function']
        all_tools.append({
            "name": f['name'],
            "description": f['description'],
            "parameters": f['parameters'],
            "source": "mcp_proxy.py (DEBUG_TOOLS)"
        })
    
    # Sanity check — alerts if a tool was added/removed without updating the
    # category map (would land in "Uncategorized" otherwise). Update this
    # number when adding/removing tools as part of the regular tool-inventory
    # workflow. Per memory `feedback_tool_inventory`: every flag needs a default;
    # similarly every tool needs a category.
    EXPECTED_TOOL_COUNT = 72
    if len(all_tools) != EXPECTED_TOOL_COUNT:
        print(f"Warning: Expected {EXPECTED_TOOL_COUNT} tools, found {len(all_tools)} — update EXPECTED_TOOL_COUNT in gen_mcp_inventory.py if a tool was added/removed.")
    
    # 3. Render
    markdown = generate_markdown(all_tools)
    
    # 4. Save
    output_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "MCP_TOOLS.md")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(markdown)
    
    print(f"Successfully created {output_path} with {len(all_tools)} entries.")

if __name__ == "__main__":
    main()
