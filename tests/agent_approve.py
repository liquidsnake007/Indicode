"""
Approve or reject a pending agent action.
Usage: python3 agent_approve.py <thread_id> approve
       python3 agent_approve.py <thread_id> reject
       python3 agent_approve.py <thread_id> edit '{"write_file": {"filename": "changed.txt"}}'
"""
import urllib.request
import json
import sys

def main():
    if len(sys.argv) < 3:
        print("Usage: agent_approve.py <thread_id> <approve|reject|edit> [edits_json]")
        print("Example: agent_approve.py abc123 approve")
        sys.exit(1)

    thread_id = sys.argv[1]
    decision = sys.argv[2]
    edits = {}

    if decision == "edit" and len(sys.argv) > 3:
        try:
            edits = json.loads(sys.argv[3])
        except json.JSONDecodeError:
            print(f"✗ Invalid JSON: {sys.argv[3]}")
            sys.exit(1)

    payload = {
        "thread_id": thread_id,
        "decision": decision,
        "edits": edits,
    }

    req = urllib.request.Request(
        "http://127.0.0.1:8003/approve",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )

    print(f"─── Sending {decision} for thread {thread_id}")

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"✗ HTTP {e.code}: {e.read().decode()[:500]}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Connection error: {e}")
        sys.exit(1)

    print(f"\n{'═'*60}")
    print(f"  THREAD:     {result.get('thread_id', thread_id)}")
    print(f"  TOOLS USED: {', '.join(result.get('tool_calls_made', [])) or 'none'}")
    print(f"{'═'*60}\n")

    if result.get("pending_approval"):
        # Another approval gate hit (multi-step task)
        pa = result["pending_approval"]
        print("⚠️  ANOTHER APPROVAL REQUIRED")
        print(f"{'─'*60}")
        for i, call in enumerate(pa.get("pending_calls", []), 1):
            print(f"  [{i}] Tool: {call['tool']}")
            print(f"      Args: {call.get('args_preview', '')[:300]}...")
        print(f"{'─'*60}")
        print(f"\n  docker exec indicode-agent python3 /tmp/agent_approve.py {thread_id} approve")

    elif result.get("response"):
        print("RESPONSE:")
        print(f"{'─'*60}")
        print(result["response"])
        print(f"{'─'*60}")
    else:
        print("(no response text)")

if __name__ == "__main__":
    main()
