from rest_framework import serializers


class StrictSerializer(serializers.Serializer):

    def validate(self, attrs):
        unknown = set(self.initial_data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                f"정의되지 않은 필드입니다: {', '.join(sorted(unknown))}"
            )
        return attrs


class LoginSerializer(StrictSerializer):
    login_id = serializers.CharField(max_length=50)
    password = serializers.CharField(max_length=128, trim_whitespace=False)


class RefreshTokenSerializer(StrictSerializer):
    refresh_token = serializers.CharField()
