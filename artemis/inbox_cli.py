"""Inbox zero CLI — manage thread states from the command line."""

import argparse

from artemis.inbox import (
    format_inbox_status,
    format_snoozed_list,
    format_waiting_list,
    get_counts,
    get_db,
    list_by_state,
    mark_done,
    mark_noise,
    mark_snoozed,
    mark_waiting,
    NEEDS_ACTION,
    SNOOZED,
    WAITING,
)


def _cli():
    parser = argparse.ArgumentParser(description="Artemis inbox zero tracker")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="Show all NEEDS_ACTION + WAITING threads")
    sub.add_parser("stats", help="Counts by state")

    done_p = sub.add_parser("done", help="Mark thread as done")
    done_p.add_argument("id", help="Gmail thread ID (or prefix)")

    snooze_p = sub.add_parser("snooze", help="Snooze a thread")
    snooze_p.add_argument("id", help="Gmail thread ID (or prefix)")
    snooze_p.add_argument("period", choices=["1d", "3d", "1w", "2w"], help="Snooze period")

    wait_p = sub.add_parser("waiting", help="Mark as waiting or list waiting threads")
    wait_p.add_argument("id", nargs="?", help="Gmail thread ID (or prefix)")
    wait_p.add_argument("--on", default="", help="Who we're waiting on")

    noise_p = sub.add_parser("noise", help="Mark thread as noise")
    noise_p.add_argument("id", help="Gmail thread ID (or prefix)")

    args = parser.parse_args()
    db = get_db()

    if args.command == "list":
        na = list_by_state(NEEDS_ACTION, db=db)
        w = list_by_state(WAITING, db=db)
        if na:
            print("\n== NEEDS ACTION ==")
            for t in na:
                print(f"  {t['id'][:12]}  {t['subject']}  (from {t['sender']})")
        if w:
            print("\n== WAITING ==")
            for t in w:
                who = t.get("waiting_on") or "?"
                print(f"  {t['id'][:12]}  {t['subject']}  (waiting on {who})")
        if not na and not w:
            print("Inbox zero! No threads need attention.")

    elif args.command == "stats":
        counts = get_counts(db=db)
        print(format_inbox_status(counts))

    elif args.command == "done":
        from artemis.inbox import resolve_thread_id
        tid = resolve_thread_id(args.id, db=db)
        if tid:
            mark_done(tid, db=db)
            print(f"Marked {args.id} as DONE")
        else:
            print(f"Thread not found: {args.id}")

    elif args.command == "snooze":
        from artemis.inbox import resolve_thread_id
        tid = resolve_thread_id(args.id, db=db)
        if tid:
            mark_snoozed(tid, args.period, db=db)
            print(f"Snoozed {args.id} for {args.period}")
        else:
            print(f"Thread not found: {args.id}")

    elif args.command == "waiting":
        if args.id:
            from artemis.inbox import resolve_thread_id
            tid = resolve_thread_id(args.id, db=db)
            if tid:
                mark_waiting(tid, waiting_on=args.on, db=db)
                print(f"Marked {args.id} as WAITING (on: {args.on or 'unspecified'})")
            else:
                print(f"Thread not found: {args.id}")
        else:
            threads = list_by_state(WAITING, db=db)
            print(format_waiting_list(threads))

    elif args.command == "noise":
        from artemis.inbox import resolve_thread_id
        tid = resolve_thread_id(args.id, db=db)
        if tid:
            mark_noise(tid, db=db)
            print(f"Marked {args.id} as NOISE")
        else:
            print(f"Thread not found: {args.id}")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
