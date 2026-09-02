# ─── SERVICE ENDPOINTS (all on the sovereign network) ──────
LITELLM_BASE_URL = "http://litellm:4000/v1"
LITELLM_API_KEY = "sk-sovereign-local-only"

RAG_SERVICE_URL = "http://rag:8001"
DOCLING_SERVICE_URL = "http://docling:8000"

# ─── MODEL ALIASES ──────────────────────────────────────────
MODEL_CLASSIFIER = "sovereign-guard"      # fast, 1.7B — just routes
MODEL_GENERAL = "sovereign-general"        # qwen3:8b — drafting, analysis
MODEL_CODER = "sovereign-coder"            # qwen3:8b — code generation
MODEL_VISION = "sovereign-vision"          # qwen3-vl-nothink — images
MODEL_GUARD = "sovereign-guard"            # NeMo-style checks

# ─── PATHS ──────────────────────────────────────────────────
WORKSPACE_DIR = "/workspace"
OUTPUT_DIR = "/workspace/outputs"
INPUT_DIR = "/workspace/inputs"

# ─── GENERATION DEFAULTS ────────────────────────────────────
MAX_TOKENS_GENERAL = 2000
MAX_TOKENS_CODER = 3000
MAX_TOKENS_VISION = 2000
MAX_TOKENS_GUARD = 500

# ─── SECURITY ───────────────────────────────────────────────
SENSITIVE_TOOLS = {"write_file", "run_code"}  # require human approval