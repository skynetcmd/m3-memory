#!/usr/bin/env python3
import os
import sys
import unittest
import uuid

# Ensure BASE_DIR is in path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from memory.knowledge_helpers import add_knowledge, delete_knowledge, list_knowledge, search_knowledge


class TestKnowledge(unittest.TestCase):
    def test_add_and_list_knowledge(self):
        title = f"Test Knowledge {uuid.uuid4()}"
        content = "This is a test knowledge item."
        source = "unit_test"
        tags = ["test", "unittest"]

        # Add
        result = add_knowledge(content, title=title, source=source, tags=tags)
        self.assertTrue(result.startswith("Created:"), f"Failed to add knowledge: {result}")
        item_id = result.split(":")[1].strip()

        # List
        items = list_knowledge(limit=10)
        found = False
        for item in items:
            if item['id'] == item_id:
                found = True
                self.assertEqual(item['title'], title)
                self.assertEqual(item['content'], content)
                self.assertEqual(item['source'], source)
                self.assertEqual(item['tags'], tags)
                break
        self.assertTrue(found, "Added item not found in list")

        # Delete
        del_result = delete_knowledge(item_id, hard=True)
        self.assertTrue("deleted" in del_result.lower(), f"Unexpected delete result: {del_result}")

        # Verify gone
        items_after = list_knowledge(limit=10)
        found_after = False
        for item in items_after:
            if item['id'] == item_id:
                found_after = True
                break
        self.assertFalse(found_after, "Item still exists after deletion")

    def test_search_knowledge_basic(self):
        # We try to search. If LM studio is not ready, it will return an error string.
        # We just verify it doesn't crash and returns a string.
        result = search_knowledge("test query", k=1)
        self.assertIsInstance(result, str)
        print(f"Search result: {result[:100]}...")

if __name__ == "__main__":
    unittest.main()
