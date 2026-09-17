# Service endpoints on the sovereign network
LITELLM_BASE_URL = "http://litellm:4000/v1"
LITELLM_API_KEY = "sk-sovereign-local-only"

RAG_SERVICE_URL = "http://rag:8001"
DOCLING_SERVICE_URL = "http://docling:8000"

# Model aliases
MODEL_GENERAL = "sovereign-general"
MODEL_CLASSIFIER = MODEL_GENERAL
MODEL_CODER = "sovereign-coder"
MODEL_VISION = "sovereign-vision"
MODEL_GUARD = "sovereign-guard"

# Workspace paths
WORKSPACE_DIR = "/workspace"
OUTPUT_DIR = "/workspace/outputs"
INPUT_DIR = "/workspace/inputs"

# Generation limits
MAX_TOKENS_DOCUMENT = 6000
MAX_TOKENS_GENERAL = 2000
MAX_TOKENS_CODER = 3000
MAX_TOKENS_VISION = 2000
MAX_TOKENS_GUARD = 500

# Tools that require human approval
SENSITIVE_TOOLS = {"write_file", "run_code"}  # require human approval