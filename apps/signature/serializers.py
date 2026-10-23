from collections.abc import Mapping
from decimal import Decimal

from rest_framework import serializers


class SignatureCreateSerializer(serializers.Serializer):
    club_id = serializers.UUIDField()
    title = serializers.CharField(max_length=200, allow_blank=False)
    description = serializers.CharField(allow_blank=False)
    flag = serializers.CharField(
        min_length=1,
        max_length=512,
        trim_whitespace=False,
        write_only=True,
    )
    score = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        default=Decimal("300.00"),
    )


class SignatureUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, allow_blank=False, required=False)
    description = serializers.CharField(allow_blank=True, required=False)
    flag = serializers.CharField(
        min_length=1,
        max_length=512,
        trim_whitespace=False,
        write_only=True,
        required=False,
    )
    score = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        required=False,
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("수정할 항목이 하나 이상 필요합니다")
        if self.context.get("is_published") and "description" in attrs and not attrs["description"].strip():
            raise serializers.ValidationError("공개 중인 문제의 설명은 비울 수 없습니다")
        return attrs


class SignaturePublishSerializer(serializers.Serializer):
    is_published = serializers.BooleanField()


class SignatureSubmitSerializer(serializers.Serializer):
    flag = serializers.CharField(
        min_length=1,
        max_length=512,
        trim_whitespace=False,
    )

    def to_internal_value(self, data):
        if (
            isinstance(data, Mapping)
            and "flag" in data
            and not isinstance(data["flag"], str)
        ):
            raise serializers.ValidationError({"flag": "플래그는 문자열이어야 합니다"})
        return super().to_internal_value(data)
