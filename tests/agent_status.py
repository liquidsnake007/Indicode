"""
Check the current state of an agent thread.
Usage: python3 agent_status.py <thread_id>
       python3 agent_status.py (uses last saved thread)
"""
import urllib.request
import json
import sys
import os

def main():
    thread_id = sys.argv[1] if len(sys.argv) > 1 else None

    if not thread_id and os.path.exists("/tmp/last_thread_id"):
        with open("/tmp/last_thread_id") as f:
            thread_id = f.read().strip()

    if not thread_id:
        print("No thread ID provided or saved. Usage: agent_status.py <thread_id>")
        sys.exit(1)

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8003/state/{thread_id}", timeout=10) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        print(f"✗ Error: {e}")
        sys.exit(1)

    print(f"Thread:     {result['thread_id']}")
    print(f"Next node:  {result.get('next', []) or '(finished)'}")
    print(f"Task type:  {result.get('task_type', 'unknown')}")
    print(f"Messages:   {result.get('message_count', 0)}")

    if "approval" in (result.get("next") or []):
        print("\n⚠️  Waiting for approval at the gate node")

if __name__ == "__main__":
    main()
