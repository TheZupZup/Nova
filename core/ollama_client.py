import httpx
import ollama
from config import OLLAMA_HOST

# Short connect timeout so unreachable Ollama fails fast.
# No read timeout — large models can take minutes to generate.
client = ollama.Client(
    host=OLLAMA_HOST,
    timeout=httpx.Timeout(5.0, read=None),
)
