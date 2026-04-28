from rich.console import Console
from rich.prompt import Prompt
from core.chat import chat
from core.memory import initialize_db, load_memories, save_memory
from memory.store import list_memories as list_natural_memories, delete_memories_matching

console = Console()
history = []


def run():
    initialize_db()
    memories = load_memories()

    console.print("[bold cyan]Nova en ligne.[/bold cyan] (tape 'exit' pour quitter)\n")
    console.print(f"[dim]{len(memories)} souvenir(s) chargé(s)[/dim]\n")

    while True:
        user_input = Prompt.ask("[bold green]Toi[/bold green]")

        if user_input.strip().lower() in ("exit", "quit", "quitter"):
            console.print("[bold cyan]Nova hors ligne.[/bold cyan]")
            break

        if user_input.lower().startswith("souviens-toi:"):
            parts = user_input[13:].strip().split(":", 1)
            if len(parts) == 2:
                save_memory(parts[0].strip(), parts[1].strip())
                memories = load_memories()
                console.print("[dim]Souvenir sauvegardé.[/dim]\n")
                continue

        low = user_input.lower().strip()

        if low.startswith("forget that ") or low.startswith("oublie que "):
            query = user_input.split(" ", 2)[2].strip()
            count = delete_memories_matching(query)
            console.print(f"[dim]Removed {count} memory(ies) matching '{query}'.[/dim]\n")
            continue

        if low.startswith("forget everything about ") or low.startswith("oublie tout sur "):
            query = user_input.split(" ", 3)[-1].strip()
            count = delete_memories_matching(query)
            console.print(f"[dim]Removed {count} memory(ies) about '{query}'.[/dim]\n")
            continue

        if low in (
            "what do you remember about me?", "show my memories", "show memories",
            "what do you know about me?", "que sais-tu de moi ?", "que sais-tu de moi?",
            "montre mes souvenirs", "montre-moi mes souvenirs",
        ):
            mems = list_natural_memories()
            if not mems:
                console.print("[dim]No natural memories stored yet.[/dim]\n")
            else:
                console.print("\n[bold cyan]Memories:[/bold cyan]")
                for m in mems:
                    console.print(f"  [dim][{m.kind}/{m.topic}][/dim] {m.content}")
                console.print()
            continue

        response, model_used = chat(history, user_input, memories)

        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": response})

        console.print(f"\n[bold cyan]Nova[/bold cyan] [dim]({model_used})[/dim]: {response}\n")


if __name__ == "__main__":
    run()
