from rest_framework import serializers


class ContestTimerSerializer(serializers.Serializer):
    name = serializers.CharField()
    status = serializers.CharField()
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    remaining_seconds = serializers.IntegerField()
    remaining_display = serializers.CharField()
    time_until_start = serializers.IntegerField()
    server_time = serializers.DateTimeField()
