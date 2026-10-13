from django.contrib import admin

from apps.board.models import (
    BoardChallenge,
    Cell,
    ChanceCard,
    DiceRoll,
    PendingDiceRoll,
    TeamBoardState,
    TeamCellCandidate,
    TeamCellConsumption,
    TeamChallengeAccess,
    TeamChanceCard,
)


@admin.register(Cell)
class CellAdmin(admin.ModelAdmin):
    list_display = ["cell_index", "type", "difficulty", "line_number", "name"]
    readonly_fields = ["cell_index", "type", "line_number"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ChanceCard)
class ChanceCardAdmin(admin.ModelAdmin):
    list_display = ["card_id", "name", "effect", "usage_timing", "weight"]


@admin.register(TeamCellConsumption)
class TeamCellConsumptionAdmin(admin.ModelAdmin):
    list_display = ["id", "team", "cell", "consumed_at"]


@admin.register(TeamChanceCard)
class TeamChanceCardAdmin(admin.ModelAdmin):
    list_display = ["id", "team", "card", "source_cell", "drawn_at", "used_at"]


@admin.register(PendingDiceRoll)
class PendingDiceRollAdmin(admin.ModelAdmin):
    list_display = ["team", "previous_position", "candidate_position", "created_at"]


@admin.register(BoardChallenge)
class BoardChallengeAdmin(admin.ModelAdmin):
    list_display = ["challenge_number", "challenge", "club_name"]
    list_filter = ["club_name"]


@admin.register(TeamChallengeAccess)
class TeamChallengeAccessAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "team",
        "challenge",
        "source_cell",
        "status",
        "opened_at",
        "cleared_at",
    ]
    list_filter = ["status"]


@admin.register(TeamCellCandidate)
class TeamCellCandidateAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "team",
        "cell",
        "challenge",
        "display_order",
        "status",
        "offered_at",
        "selected_at",
    ]
    list_filter = ["status"]


@admin.register(TeamBoardState)
class TeamBoardStateAdmin(admin.ModelAdmin):
    list_display = [
        "team",
        "position",
        "dice_rolls_left",
        "active_challenge_access",
        "updated_at",
    ]


@admin.register(DiceRoll)
class DiceRollAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "team",
        "dice_a",
        "dice_b",
        "rolled_number",
        "previous_position",
        "current_position",
        "created_at",
    ]
