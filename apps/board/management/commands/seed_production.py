from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.board.line_rules import CELL_LINE_NUMBERS
from apps.board.models import Cell, ChanceCard
from apps.board.seed_data import (
    BOARD_SIZE,
    CELL_DIFFICULTY,
    CHANCE_CARDS,
    DIFFICULTY_LABELS,
    FIRST_CELL_INDEX,
    SPECIAL_CELLS,
)


class Command(BaseCommand):
    help = "Create missing production board cells and chance cards without resetting existing data."

    @transaction.atomic
    def handle(self, *args, **options):
        # Serialize seed runs even when there are no rows to lock yet.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [20261024, 1])
        cells = {cell.pk: cell for cell in Cell.objects.select_for_update().order_by("pk")}
        cards = {card.pk: card for card in ChanceCard.objects.select_for_update().order_by("pk")}
        expected_indexes = set(range(FIRST_CELL_INDEX, FIRST_CELL_INDEX + BOARD_SIZE))
        if set(cells) - expected_indexes:
            raise CommandError("Unexpected board cell indexes; no data was changed.")

        missing_cells = []
        for index in sorted(expected_indexes):
            special = SPECIAL_CELLS.get(index)
            difficulty = None if special else CELL_DIFFICULTY[index]
            cell_type, name = special or (
                Cell.CellType.CHALLENGE, f"문제({DIFFICULTY_LABELS[difficulty]})"
            )
            line = CELL_LINE_NUMBERS.get(index)
            existing = cells.get(index)
            valid_difficulties = (None, "") if special else Cell.Difficulty.values
            if existing is None:
                missing_cells.append(Cell(
                    cell_index=index, type=cell_type, name=name,
                    difficulty=difficulty, line_number=line,
                ))
            elif (
                existing.type != cell_type
                or existing.line_number != line
                or existing.difficulty not in valid_difficulties
            ):
                raise CommandError(f"Cell {index} has an incompatible structure or difficulty; no data was changed.")

        missing_cards = []
        for defaults in CHANCE_CARDS:
            existing = cards.get(defaults["card_id"])
            if existing is None:
                missing_cards.append(ChanceCard(**defaults))
            elif any(getattr(existing, key) != defaults[key] for key in ("effect", "usage_timing")):
                raise CommandError(f"Card {existing.pk} has an incompatible effect or timing; no data was changed.")

        Cell.objects.bulk_create(missing_cells)
        ChanceCard.objects.bulk_create(missing_cards)
        self.stdout.write(self.style.SUCCESS(
            f"Production seed complete: created {len(missing_cells)} cells and {len(missing_cards)} cards; existing data preserved."
        ))
