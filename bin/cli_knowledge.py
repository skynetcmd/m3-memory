#!/usr/bin/env python3
import argparse
import sys
import os

# Ensure we can import from the memory directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

sys.path.insert(0, os.path.join(BASE_DIR, "bin"))
from m3_sdk import resolve_venv_python

def ensure_venv():
    venv_python = resolve_venv_python()
    if os.path.exists(venv_python) and sys.executable != venv_python:
        os.execl(venv_python, venv_python, *sys.argv)

ensure_venv()

from memory.knowledge_helpers import add_knowledge, search_knowledge, list_knowledge, delete_knowledge, get_all_types, update_knowledge

def main():
    # Intercept old positional commands to maintain backwards compatibility 
    # while allowing the new flag-based behavior.
    for i, arg in enumerate(sys.argv):
        if arg in ("add", "search", "list", "delete", "update"):
            sys.argv[i] = f"--{arg}"
            break

    parser = argparse.ArgumentParser(description="Knowledge Base CLI")
    
    # Core Actions (Mutually Exclusive)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-a", "--add", type=str, metavar="CONTENT", help="Add a knowledge item with this content")
    group.add_argument("-u", "--update", type=str, metavar="ID", help="Update an existing knowledge item by ID")
    group.add_argument("-s", "--search", type=str, metavar="QUERY", help="Search knowledge items")
    group.add_argument("-l", "--list", action="store_true", help="List recent knowledge items")
    group.add_argument("-d", "--delete", type=str, metavar="ID", help="Delete a knowledge item by ID")
    
    # Modifiers
    parser.add_argument("-c", "--content", type=str, default="", help="Updated content for the item (with -u)")
    parser.add_argument("-t", "--type", type=str, default="", help="Filter or set item type (use 'all' or '?' to list types in DB)")
    parser.add_argument("-k", "--limit", type=int, default=5, help="Number of results for search/list (default: 5)")
    parser.add_argument("--title", type=str, default="", help="Title for added/updated item")
    parser.add_argument("--source", type=str, default="", help="Optional source for added item")
    parser.add_argument("--tags", type=str, default="", help="Comma-separated tags for added item")
    parser.add_argument("--metadata", type=str, default="", help="Raw JSON metadata string (overrides source/tags on add, appends/replaces on update)")
    parser.add_argument("--importance", type=float, default=-1.0, help="Importance score for update (0.0 to 1.0)")
    parser.add_argument("--reembed", action="store_true", help="Force vector re-embedding during update")
    parser.add_argument("--hard", type=str, metavar="WIPE", default=None, help="Permanently delete from database (requires exact string 'WIPE')")

    args = parser.parse_args()

    if args.type in ("?", "all"):
        types = get_all_types()
        print("Available knowledge base types in database:")
        for t in types:
            print(f"  - {t}")
        sys.exit(0)

    # Auto-append wildcard for search and list operations (unless quoted exactly)
    if args.type and args.add is None and args.delete is None:
        is_exact = (args.type.startswith('"') and args.type.endswith('"')) or (args.type.startswith("'") and args.type.endswith("'"))
        if not is_exact:
            args.type = args.type.replace("*", "%")
            if not args.type.endswith("%"):
                args.type += "%"

    if args.add is not None:
        tags = [t.strip() for t in args.tags.split(",")] if args.tags else []
        add_type = args.type if args.type else "knowledge"
        result = add_knowledge(args.add, title=args.title, source=args.source, tags=tags, item_type=add_type, metadata=args.metadata)
        print(result)

    elif args.update is not None:
        # Check if they are trying to update the type (not natively supported by memory_update_impl yet, 
        # so we notify them or just use the other fields)
        if args.type:
            print("Warning: Updating 'type' is not supported via the CLI update command yet. Use DB queries.")
            
        result = update_knowledge(
            item_id=args.update,
            content=args.content,
            title=args.title,
            metadata=args.metadata,
            importance=args.importance,
            reembed=args.reembed
        )
        print(result)

    elif args.search is not None:
        result = search_knowledge(args.search, k=args.limit, type_filter=args.type)
        
        # If search_knowledge returns a string instead of a structured result (like an error), print it directly
        if isinstance(result, str):
            print(result)
        else:
            # If it were returning dictionaries, we'd handle it here. 
            # However, memory_search in memory_bridge returns a formatted string.
            pass

    elif args.list:
        items = list_knowledge(limit=args.limit, type_filter=args.type)
        if not items:
            print("No knowledge items found.")
        for item in items:
            item_type = item.get('type') or 'unknown'
            tags = item.get('tags') or []
            source = item.get('source') or ''
            print(f"[{item['id']}]  type: {item_type}  |  {item['title'] or '(No Title)'}")
            print(f"  Created:    {item['created_at']}")
            if tags:
                print(f"  Tags:       {' · '.join(tags)}")
            if source:
                print(f"  Source:     {source}")
            print(f"  Content:\n{item['content']}\n")
            print("-" * 40)

    elif args.delete is not None:
        hard_delete = False
        if args.hard is not None:
            if args.hard == "WIPE":
                hard_delete = True
            else:
                print("Error: To permanently delete, you must use exactly: --hard WIPE")
                sys.exit(1)
        result = delete_knowledge(args.delete, hard=hard_delete)
        print(result)

    else:
        # If no command was specified but a type was provided, list by default
        if args.type:
            items = list_knowledge(limit=args.limit, type_filter=args.type)
            if not items:
                print(f"No knowledge items found for type '{args.type}'.")
            for item in items:
                item_type = item.get('type') or 'unknown'
                tags = item.get('tags') or []
                print(f"[{item['id']}]  type: {item_type}  |  {item['title'] or '(No Title)'}")
                print(f"  Created:    {item['created_at']}")
                if tags:
                    print(f"  Tags:       {' · '.join(tags)}")
                print(f"  Content:\n{item['content']}\n")
                print("-" * 40)
        else:
            parser.print_help()

if __name__ == "__main__":
    main()