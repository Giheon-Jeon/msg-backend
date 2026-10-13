from rest_framework import serializers

from apps.board.models import (
    Cell,
    ChanceCard,
    DiceRoll,
    TeamBoardState,
    TeamChallengeAccess,
)
from apps.challenge.models import Challenge


class CellSerializer(serializers.ModelSerializer):
    class Meta:
        model = Cell
        fields = ["cell_index", "type", "difficulty", "line_number", "name"]


class ChanceCardSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChanceCard
        fields = ["card_id", "name", "description", "effect", "usage_timing"]


class ChallengeCandidateSerializer(serializers.ModelSerializer):
    club_name = serializers.CharField(source="board_meta.club_name", read_only=True)
    score = serializers.DecimalField(
        source="current_score", max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = Challenge
        fields = ["challenge_id", "title", "category", "club_name", "score"]


class ChallengeSerializer(serializers.ModelSerializer):
    club_name = serializers.CharField(source="board_meta.club_name", read_only=True)
    score = serializers.DecimalField(
        source="current_score", max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = Challenge
        fields = [
            "challenge_id",
            "title",
            "category",
            "club_name",
            "difficulty",
            "description",
            "score",
        ]


class TeamChallengeAccessSerializer(serializers.ModelSerializer):
    access_id = serializers.UUIDField(source="id", read_only=True)
    challenge = ChallengeSerializer(read_only=True)
    source_cell_index = serializers.IntegerField(
        source="source_cell_id", read_only=True
    )

    class Meta:
        model = TeamChallengeAccess
        fields = [
            "access_id",
            "challenge",
            "source_cell_index",
            "status",
            "opened_at",
            "cleared_at",
        ]


class TeamBoardStateSerializer(serializers.ModelSerializer):
    position = serializers.IntegerField(source="position_id", read_only=True)
    current_cell = CellSerializer(source="position", read_only=True)
    active_challenge = TeamChallengeAccessSerializer(
        source="active_challenge_access", read_only=True
    )

    class Meta:
        model = TeamBoardState
        fields = [
            "position",
            "current_cell",
            "dice_rolls_left",
            "active_challenge",
            "next_dice_reset_at",
            "airport_move_used",
            "has_passed_start",
            "updated_at",
        ]


class DiceRollSerializer(serializers.ModelSerializer):
    dice_roll_id = serializers.UUIDField(source="id", read_only=True)

    class Meta:
        model = DiceRoll
        fields = [
            "dice_roll_id",
            "dice_a",
            "dice_b",
            "rolled_number",
            "previous_position",
            "current_position",
            "created_at",
        ]
