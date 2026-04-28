import re
from memory.schema import Memory

# Maps topic labels to representative keywords found in content.
_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "linux_distribution": ["linux", "fedora", "ubuntu", "debian", "arch", "mint", "kde", "gnome", "wayland", "x11", "distro"],
    "editor": ["vim", "neovim", "nvim", "vscode", "emacs", "sublime", "nano", "helix", "cursor"],
    "terminal": ["terminal", "alacritty", "kitty", "konsole", "tmux", "wezterm", "bash", "zsh", "fish"],
    "language": ["python", "javascript", "typescript", "rust", "golang", "java", "php", "ruby", "swift", "kotlin", "c++", "c#"],
    "framework": ["fastapi", "django", "flask", "react", "vue", "angular", "svelte", "nextjs", "express", "rails", "spring"],
    "hardware": ["cpu", "gpu", "ram", "ssd", "nvme", "hdd", "monitor", "keyboard", "mouse", "nvidia", "amd", "intel", "ryzen"],
    "workflow": ["workflow", "setup", "config", "configuration", "automate", "script", "pipeline", "makefile"],
    "coding_style": ["indent", "tabs", "spaces", "formatting", "linter", "formatter", "black", "prettier", "pylint", "eslint"],
    "ai_tools": ["ollama", "llm", "gpt", "claude", "gemini", "llama", "mistral", "openai"],
}

# Each entry: (compiled regex, kind, confidence, human-readable prefix for content)
_TRIGGERS: list[tuple[re.Pattern, str, float, str]] = [
    # Explicit memory requests
    (re.compile(r"(?:remember that|note that|souviens-toi que|n'oublie pas que)\s+(.+)", re.I), "general", 0.85, "User noted:"),

    # Workflow / habits
    (re.compile(r"(?:from now on|dorénavant|à partir de maintenant)\s*,?\s*(.+)", re.I), "workflow", 0.85, "User prefers:"),

    # Positive preferences
    (re.compile(r"i (?:really )?(?:prefer|love|enjoy|like)\s+(.+?)(?=\s+and\s+i|\s+but\s+|[.!?]|$)", re.I), "preference", 0.9, "User prefers"),
    (re.compile(r"je (?:préfère|adore|aime|aime bien)\s+(.+?)(?=\s+et\s+je|\s+mais\s+|[.!?]|$)", re.I), "preference", 0.9, "User prefers"),
    (re.compile(r"i(?:'m a)? (?:big )?fan of\s+(.+?)(?=\s+and\s+|\s+but\s+|[.!?]|$)", re.I), "preference", 0.85, "User is a fan of"),

    # Negative preferences / avoidances
    (re.compile(r"i (?:don't like|hate|dislike|can't stand|avoid)\s+(.+?)(?=\s+and\s+i|\s+but\s+|[.!?]|$)", re.I), "avoid", 0.9, "User dislikes"),
    (re.compile(r"(?:j'aime pas|je déteste|je n'aime pas|j'évite|je supporte pas)\s+(.+?)(?=\s+et\s+je|\s+mais\s+|[.!?]|$)", re.I), "avoid", 0.9, "User dislikes"),

    # Project descriptions  ("my project Nova uses FastAPI and Ollama")
    (re.compile(r"my project\s+(\w+)\s+(?:uses?|is built with|runs? on)\s+(.+?)(?=[.!?]|$)", re.I), "project", 0.9, None),
    (re.compile(r"mon projet\s+(\w+)\s+(?:utilise|est fait avec|tourne sur)\s+(.+?)(?=[.!?]|$)", re.I), "project", 0.9, None),

    # Hardware
    (re.compile(r"my (?:pc|computer|machine|laptop|desktop)\s+(?:has|runs?|is running)\s+(.+?)(?=[.!?]|$)", re.I), "hardware", 0.85, "User's machine has"),
    (re.compile(r"mon (?:pc|ordinateur|machine|laptop)\s+(?:a|tourne|fait tourner)\s+(.+?)(?=[.!?]|$)", re.I), "hardware", 0.85, "User's machine has"),
    (re.compile(r"i have\s+(?:a\s+)?(\d+\s*(?:gb|tb|mb)\s+(?:of\s+)?(?:ram|ssd|storage|vram).+?)(?=[.!?]|$)", re.I), "hardware", 0.85, "User has"),

    # Software / tool usage
    (re.compile(r"i use\s+(.+?)(?:\s+for\s+.+)?(?=[.!?]|$)", re.I), "software", 0.8, "User uses"),
    (re.compile(r"j'utilise\s+(.+?)(?:\s+pour\s+.+)?(?=[.!?]|$)", re.I), "software", 0.8, "User uses"),
]


def _infer_topic(text: str, fallback: str) -> str:
    """Returns a topic label based on keyword matches in text, or the fallback."""
    lowered = text.lower()
    for topic, keywords in _TOPIC_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return topic
    return fallback


def extract_memories(message: str) -> list[Memory]:
    """
    Scans `message` for known trigger phrases and returns a list of Memory
    objects. No LLM is involved — purely rule-based for speed and reliability.
    """
    results: list[Memory] = []

    for pattern, kind, confidence, prefix in _TRIGGERS:
        for match in pattern.finditer(message):
            groups = [g.strip() for g in match.groups() if g]
            if not groups:
                continue

            # Project pattern has two groups: name + stack
            if kind == "project" and len(groups) == 2:
                project_name, stack = groups
                content = f"{project_name} uses {stack}."
                topic = project_name.lower()
            elif prefix:
                raw = groups[0]
                content = f"{prefix} {raw}."
                topic = _infer_topic(raw, kind)
            else:
                content = groups[0]
                topic = _infer_topic(content, kind)

            results.append(Memory(kind=kind, topic=topic, content=content, confidence=confidence))

    return results
