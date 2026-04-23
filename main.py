from rich.console import Console
from rich.prompt import Prompt
from core.chat import chat
from core.memory import initialize_db, load_memories, save_memory

console = Console()
history = []


def run():
    initialize_db()
    memories = load_memories()

    console.print("[bold cyan]Nexus en ligne.[/bold cyan] (tape 'exit' pour quitter)\n")
    console.print(f"[dim]{len(memories)} souvenir(s) chargé(s)[/dim]\n")

    while True:
        user_input = Prompt.ask("[bold green]Toi[/bold green]")

        if user_input.strip().lower() in ("exit", "quit", "quitter"):
            console.print("[bold cyan]Nexus hors ligne.[/bold cyan]")
            break

        if user_input.lower().startswith("souviens-toi:"):
            parts = user_input[13:].strip().split(":", 1)
            if len(parts) == 2:
                save_memory(parts[0].strip(), parts[1].strip())
                memories = load_memories()
                console.print("[dim]Souvenir sauvegardé.[/dim]\n")
                continue

        response, model_used = chat(history, user_input, memories)

        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": response})

        console.print(f"\n[bold cyan]Nexus[/bold cyan] [dim]({model_used})[/dim]: {response}\n")


if __name__ == "__main__":
    run()
