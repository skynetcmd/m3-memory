"""Print functions for plan bodies, dry-run, and run summaries."""
from __future__ import annotations


def _print_plan_body(plan: dict) -> None:
    """Shared plan summary used by both dry-run and real-run banners."""
    print(f"  Profile:             {plan['profile_name']}")
    print(f"  Model:               {plan['model']}")
    print(f"  Endpoint:            {plan['url']}")
    print(f"  Backend:             {plan['backend']}")
    print(f"  Target variant:      {plan['target_variant']}")
    src_label = plan.get('source_variant') or "(all)"
    if src_label == "__none__":
        src_label = "__none__ (variant IS NULL)"
    print(f"  Source variant:      {src_label}")
    print(f"  Type allowlist:      {plan['types']}")
    print()
    for db_label, db_info in plan["dbs"].items():
        print(f"  -- {db_label} " + "-" * 13)
        print(f"     path:          {db_info['path']}")
        print(f"     conversations: {db_info['n_groups']}")
        print(f"     turns total:   {db_info['n_turns']}")
        if db_info.get('cost_estimate'):
            print(f"     est cost:      {db_info['cost_estimate']}")
        if db_info.get('wall_estimate'):
            print(f"     est wall:      {db_info['wall_estimate']}")
        print()


def _print_dry_run(plan: dict) -> None:
    """Friendly summary of what would happen, without doing it."""
    bar = "=" * 62
    print()
    print(bar)
    print("  m3-enrich DRY RUN -- no writes will happen")
    print(bar)
    print()
    _print_plan_body(plan)
    print("To run for real, drop --dry-run.")
    print(bar)


def _print_run_summary(plan: dict) -> None:
    """Banner for an actual enrichment run (writes will happen)."""
    bar = "=" * 62
    print()
    print(bar)
    print("  m3-enrich RUN -- writing observations")
    print(bar)
    print()
    _print_plan_body(plan)
    print(bar)
