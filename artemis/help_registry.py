"""Generated command help (POLISH-1 `help`).

The command vocabulary is a DATA registry here, not a hand-maintained prose string
that goes stale — this weekend proved `close` existed and worked yet was
undiscoverable because no help text mentioned it. `help` / `commands` renders this
registry grouped by module; `help <word>` filters it.

The invariant we hold (enforced by test_polish1): a deterministic command cannot
be routable without a registry entry. The dispatch chain in `main._handle_mention`
routes by handler; every handler that owns a user-visible command is represented
here, and the test cross-checks the two so a new routable command with no help
entry fails CI. Adding a command therefore means adding its Command() row — the
same discipline as the migrate-first rule for schema.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    module: str        # group heading (email, commitments, vault, …)
    phrase: str        # the invocation pattern, human-readable
    description: str    # one line
    example: str        # a concrete example the user can copy


# Ordered so `help` reads top-to-bottom the way the day flows. `handler` in the
# comment ties each block to the dispatch handler that routes it (the coverage
# anchor the introspective test checks).
COMMANDS: list[Command] = [
    # ── brief ── (handler: morning_brief_command)
    Command("brief", "morning brief", "Your day, composed live (calendar, commitments, inbox)", "morning brief"),

    # ── email ── (handlers: inbox_listing, disposition_command, done_command, inbox_command)
    Command("email", "inbox", "List the inbox, numbered", "inbox"),
    Command("email", "more", "Next page of the current inbox listing", "more"),
    Command("email", "archive <#>", "Archive listed emails (single, range, or batch)", "archive 1-4"),
    Command("email", "file <#> as <label>", "File listed emails under @artemis/<label>", "file 5-7 as billing"),
    Command("email", "delete <#> / spam <#>", "Delete or mark-spam listed emails (confirmed)", "delete 2, 5"),
    Command("email", "done <thread-id>", "Mark an inbox thread done (hex id)", "done 18c9f0a2b3d4"),
    Command("email", "wait / snooze / noise <id>", "Set a thread's state", "snooze 18c9f0a2 3d"),
    Command("email", "waiting / snoozed", "List threads you're waiting on / snoozed", "waiting"),

    # ── commitments ── (handlers: done_command, inbox_command→close, dossier todos/remind)
    Command("commitments", "commitments", "List your open commitments (with ids)", "commitments"),
    Command("commitments", "close #<id>", "Close a commitment by id", "close #6"),
    Command("commitments", "close <title>", "Close a commitment by (fuzzy) title", "close renewal deck"),
    Command("commitments", "done <words>", "Close a commitment by title (same as close)", "done follow up with jennifer"),
    Command("commitments", "todos [today|this week|next week|tomorrow]", "Your to-dos for a window", "todos this week"),
    Command("commitments", "remind me to <task> <when>", "Log a reminder / commitment", "remind me to email Sam Friday"),

    # ── dossier / org ── (handler: dossier_command)
    Command("dossier", "brief <name> [about <topic>]", "Pre-meeting package on a person", "brief Jeremy about renewal"),
    Command("dossier", "dossier show <name> [--drafts]", "Show a colleague dossier", "dossier show Jeremy"),
    Command("dossier", "dossier review", "Review/approve extracted to-dos & notes", "dossier review"),
    Command("dossier", "dossier new <name>", "Start a new dossier", "dossier new Priya Rao"),
    Command("dossier", "dossier set <name> <field>: …", "Author a dossier field (confirmed)", "dossier set Jeremy position: VP Eng"),
    Command("dossier", "org <name> / org notes <name>", "Where a person/org fits; org notes", "org Acme"),
    Command("dossier", "org set <org> <field>: …", "Author an org-profile fact (confirmed)", "org set Acme tier: strategic"),

    # ── vault ── (handler: vault_command)
    Command("vault", "vault sync / vault status", "Sync the vault mirror / show status", "vault sync"),
    Command("vault", "digest / today's digest", "Review today's extraction proposals", "digest"),
    Command("vault", "proposals [expired]", "List pending (or expired) proposals", "proposals"),
    Command("vault", "approve <n> / approve all / reject <n>", "Adjudicate a live digest", "approve 1-3"),

    # ── health / training ── (handlers: health_conversation, nutrition, capture_propose, grocery_staples)
    Command("health", "<morning check-in>", "Log morning state", "slept 6.5 energy 3"),
    Command("health", "what's my plan today", "Show the training plan for a day", "what's my plan today"),
    Command("health", "done", "End the active workout session", "done"),
    Command("health", "fix <exercise> rpe <n>", "Correct a logged set", "fix squat rpe 8"),
    Command("health", "log <n> cal / set target …", "Log nutrition / set a target", "log 500 cal"),
    Command("health", "build grocery list", "Propose the week's staples from the meal plan", "build grocery list"),

    # ── rules ── (handler: rule_command)
    Command("rules", "rules", "List active playbook rules", "rules"),
    Command("rules", "rule add <spec>", "Add an inbox automation rule (confirmed)", 'rule add archive from:noreply@ci.example.com'),
    Command("rules", "rule off <id>", "Disable a rule", "rule off 3"),

    # ── calendar ── (handlers: calendar_view, scheduling, availability, delete_event)
    Command("calendar", "what's on my calendar", "Show calendar events for a window", "calendar this week"),
    Command("calendar", "availability / when am I free", "Show open slots", "availability next week"),
    Command("calendar", "delete event <id|name>", "Delete a calendar event (confirmed)", "delete event standup"),

    # ── ops ── (handlers: version_command, action_item_command, direct commands)
    Command("ops", "version", "Running version + commit + start time", "version"),
    Command("ops", "update check", "Compare the running commit to GitHub", "update check"),
    Command("ops", "help / commands [word]", "This command list (optionally filtered)", "help email"),
    Command("ops", "crm status / contacts / leads", "CRM summaries", "crm status"),
    Command("ops", "playbooks", "List loaded playbooks", "playbooks"),
    Command("ops", "quiet hours / override / timezone", "Quiet-hours + timezone controls", "quiet hours"),
    Command("ops", "approve|skip|snooze sched <id>", "Act on a pending scheduling action item", "approve sched 1a2b3c4d"),
]

# Module display order + emoji, so `help` groups read consistently.
_MODULE_ORDER = [
    ("brief", "☀️ Brief"),
    ("email", "\U0001f4ec Email"),
    ("commitments", "✅ Commitments"),
    ("dossier", "\U0001f4c7 Dossier / Org"),
    ("vault", "\U0001f5c4️ Vault"),
    ("health", "\U0001f3cb️ Health"),
    ("rules", "\U0001f4cb Rules"),
    ("calendar", "\U0001f4c5 Calendar"),
    ("ops", "\U0001f9ed Ops"),
]


def _render_group(module: str, title: str, cmds: list[Command]) -> str:
    lines = [f"**{title}**"]
    for c in cmds:
        lines.append(f"  • `{c.phrase}` — {c.description}  _(e.g. {c.example})_")
    return "\n".join(lines)


def render_help(query: str | None = None) -> str:
    """Render the command vocabulary. With `query`, filter to commands whose
    module/phrase/description/example contains the word (case-insensitive)."""
    cmds = COMMANDS
    if query:
        q = query.strip().lower()
        cmds = [c for c in COMMANDS
                if q in c.module.lower() or q in c.phrase.lower()
                or q in c.description.lower() or q in c.example.lower()]
        if not cmds:
            return (f"No commands match “{query}”. Say `help` for the full list, "
                    f"or try a module: {', '.join(m for m, _ in _MODULE_ORDER)}.")

    by_module: dict[str, list[Command]] = {}
    for c in cmds:
        by_module.setdefault(c.module, []).append(c)

    out = ["\U0001f9ed **Artemis commands**" if not query
           else f"\U0001f9ed **Artemis commands — “{query}”**"]
    for module, title in _MODULE_ORDER:
        if module in by_module:
            out.append("")
            out.append(_render_group(module, title, by_module[module]))
    # Any module not in the ordered list (defensive — keeps the invariant that
    # every registered command renders).
    for module in by_module:
        if module not in {m for m, _ in _MODULE_ORDER}:
            out.append("")
            out.append(_render_group(module, module.title(), by_module[module]))
    if not query:
        out.append("\n_Filter with_ `help <word>` _(e.g._ `help email`_)._")
    return "\n".join(out)
