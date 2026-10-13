from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from apps.challenge.models import Challenge


class ChallengeCreateSerializer(serializers.Serializer):
    challenge_slug = serializers.RegexField(
        regex=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        max_length=100,
        validators=[
            UniqueValidator(
                queryset=Challenge.objects.all(),
                message="이미 사용 중인 challenge_slug입니다.",
            )
        ],
        error_messages={
            "invalid": "challenge_slug는 소문자 영문, 숫자, 하이픈만 사용할 수 있습니다.",
        },
    )
    title = serializers.CharField(max_length=200)
    category = serializers.ChoiceField(choices=Challenge.CategoryType.choices)
    difficulty = serializers.ChoiceField(choices=Challenge.DifficultyType.choices)
    description = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    flag = serializers.CharField(trim_whitespace=False, write_only=True)
    initial_score = serializers.IntegerField(
        min_value=0,
        max_value=9_999_999_999,
        default=1000,
    )
    minimum_score = serializers.IntegerField(
        min_value=0,
        max_value=9_999_999_999,
        default=600,
    )
    decay = serializers.IntegerField(min_value=1, default=70)

    def validate(self, attrs):
        unknown_fields = set(self.initial_data) - set(self.fields)
        if unknown_fields:
            raise serializers.ValidationError(
                f"정의되지 않은 필드입니다: {', '.join(sorted(unknown_fields))}"
            )
        if attrs["initial_score"] < attrs["minimum_score"]:
            raise serializers.ValidationError(
                "initial_score는 minimum_score 이상이어야 합니다"
            )
        return attrs


class ChallengeUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, required=False)
    category = serializers.ChoiceField(
        choices=Challenge.CategoryType.choices,
        required=False,
    )
    difficulty = serializers.ChoiceField(
        choices=Challenge.DifficultyType.choices,
        required=False,
    )
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    flag = serializers.CharField(
        trim_whitespace=False,
        write_only=True,
        required=False,
    )
    initial_score = serializers.IntegerField(
        min_value=0,
        max_value=9_999_999_999,
        required=False,
    )
    minimum_score = serializers.IntegerField(
        min_value=0,
        max_value=9_999_999_999,
        required=False,
    )
    decay = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs):
        unknown_fields = set(self.initial_data) - set(self.fields)
        if unknown_fields:
            raise serializers.ValidationError(
                f"정의되지 않은 필드입니다: {', '.join(sorted(unknown_fields))}"
            )
        if not attrs:
            raise serializers.ValidationError("수정할 항목이 하나 이상 필요합니다")

        initial_score = attrs.get("initial_score", self.instance.initial_score)
        minimum_score = attrs.get("minimum_score", self.instance.minimum_score)
        if initial_score < minimum_score:
            raise serializers.ValidationError(
                "initial_score는 minimum_score 이상이어야 합니다"
            )
        return attrs
