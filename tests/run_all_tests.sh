#!/bin/bash
# Indicode Phase 3 — Full test suite
# Run this AFTER the agent container is up and healthy

echo "═══════════════════════════════════════════════════════════════"
echo "  INDICODE PHASE 3 — TEST SUITE"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Helper to run chat inside the agent container
agent_chat() {
    docker exec indicode-agent python3 /tmp/agent_chat.py "$1"
}

agent_approve() {
    docker exec indicode-agent python3 /tmp/agent_approve.py "$1" "$2"
}

# ─── Test 0: Health ──────────────────────────────────────────
echo "─── Test 0: Agent health"
docker exec indicode-agent python -c \
    "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8003/health').read().decode())" \
    && echo "  ✓ PASS" || { echo "  ✗ FAIL — agent not healthy, aborting"; exit 1; }
echo ""

# ─── Test 1: Classification (5 types) ────────────────────────
echo "─── Test 1: Task classification"
for MSG in "write a Python script" "read the inspection report" "analyze this image" "search the SOPs" "hello"; do
    TYPE=$(docker exec indicode-agent python3 -c "
import urllib.request, json
payload = json.dumps({'message': '$MSG'}).encode()
req = urllib.request.Request('http://127.0.0.1:8003/chat', data=payload, headers={'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req, timeout=180) as resp:
        r = json.loads(resp.read().decode())
        print(r.get('task_type', 'error'))
except Exception as e:
    print('error')
" 2>/dev/null)
    echo "  '$MSG' → $TYPE"
done
echo ""

# ─── Test 2: Knowledge search (RAG) ──────────────────────────
echo "─── Test 2: Knowledge search"
agent_chat "What is the approval process for equipment repairs?"
echo ""

# ─── Test 3: Vision (handwritten note) ───────────────────────
echo "─── Test 3: Vision analysis"
agent_chat "Read the handwritten site note and tell me what equipment problems were observed."
echo ""

# ─── Test 4: End-to-end approval note (the money demo) ───────
echo "─── Test 4: Approval note demo"
echo "  This will hit the approval gate. Approve when prompted."
agent_chat "Read the inspection report for HX-301 from inputs/samples, extract the key findings, and draft an approval note as approval_note_hx301.docx"
echo ""

# ─── Test 5: Check output file ───────────────────────────────
echo "─── Test 5: Verify output file"
if [ -f ~/indicode/workspace/outputs/approval_note_hx301.docx ]; then
    SIZE=$(ls -la ~/indicode/workspace/outputs/approval_note_hx301.docx | awk '{print $5}')
    echo "  ✓ approval_note_hx301.docx exists ($SIZE bytes)"
else
    echo "  ✗ approval_note_hx301.docx NOT found"
    echo "  Did you approve Test 4's pending action?"
fi
echo ""

# ─── Test 6: Airgap still intact ─────────────────────────────
echo "─── Test 6: Airgap verification"
docker run --rm --network indicode-sovereign alpine wget -T3 -q -O- http://google.com 2>&1 | grep -q "can't connect\|unreachable\|timed out" \
    && echo "  ✓ Egress blocked" || echo "  ✗ EGRESS NOT BLOCKED — CHECK NOW"
echo ""

echo "═══════════════════════════════════════════════════════════════"
echo "  TEST SUITE COMPLETE"
echo "═══════════════════════════════════════════════════════════════"
