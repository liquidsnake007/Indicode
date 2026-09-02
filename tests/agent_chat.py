"""
Send a chat message to the Indicode agent.
Usage: python3 agent_chat.py "your message here"
       python3 agent_chat.py "your message" --thread <thread_id>
"""
import urllib.request
import json
import sys
import argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("message", help="The message to send")
    parser.add_argument("--thread", default=None, help="Continue an existing thread")
    parser.add_argument("--host", default="http://127.0.0.1:8003")
    args = parser.parse_args()

    payload = {
        "message": args.message,
        "thread_id": args.thread,
    }

    req = urllib.request.Request(
        f"{args.host}/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )

    print(f"─── Sending: {args.message[:80]}{'...' if len(args.message) > 80 else ''}")
    if args.thread:
        print(f"─── Thread: {args.thread}")

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"✗ HTTP {e.code}: {e.read().decode()[:500]}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Connection error: {e}")
        sys.exit(1)

    # Display results
    print(f"\n{'═'*60}")
    print(f"  THREAD ID:  {result['thread_id']}")
    print(f"  TASK TYPE:  {result.get('task_type', 'unknown')}")
    print(f"  TOOLS USED: {', '.join(result.get('tool_calls_made', [])) or 'none'}")
    print(f"{'═'*60}\n")

    if result.get("pending_approval"):
        # Agent is waiting for approval
        pa = result["pending_approval"]
        print("⚠️  APPROVAL REQUIRED")
        print(f"{'─'*60}")
        print(f"  Agent wants to run: {len(pa.get('pending_calls', []))} sensitive action(s)")
        for i, call in enumerate(pa.get("pending_calls", []), 1):
            print(f"\n  [{i}] Tool: {call['tool']}")
            preview = call.get('args_preview', '')[:300]
            print(f"      Args: {preview}...")
        print(f"{'─'*60}")
        print(f"\n  To APPROVE: python3 agent_approve.py {result['thread_id']} approve")
        print(f"  To REJECT:  python3 agent_approve.py {result['thread_id']} reject")
        print(f"\n  (Run these inside the agent container)")
        print(f"  docker exec indicode-agent python3 /tmp/agent_approve.py {result['thread_id']} approve")

    elif result.get("response"):
        print("RESPONSE:")
        print(f"{'─'*60}")
        print(result["response"])
        print(f"{'─'*60}")
    else:
        print("(no response text — possibly tool-only turn)")

    # Save thread_id for easy continuation
    with open("/tmp/last_thread_id", "w") as f:
        f.write(result["thread_id"])
    print(f"\n(Thread ID saved to /tmp/last_thread_id: {result['thread_id']})")

if __name__ == "__main__":
    main()
