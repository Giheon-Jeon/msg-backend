import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models


class Team(models.Model):
    team_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team_name = models.CharField(max_length=100, unique=True)
    team_score = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    mileage = models.IntegerField(default=0)
    is_banned = models.BooleanField(default=False)
    ban_reason = models.CharField(max_length=500, null=True, blank=True)
    banned_at = models.DateTimeField(null=True, blank=True)
    banned_by = models.CharField(max_length=50, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "teams"

    def __str__(self):
        return self.team_name


class Role(models.TextChoices):
    PARTICIPANT = "PARTICIPANT"
    ADMIN = "ADMIN"


class UserManager(BaseUserManager):
    def create_user(self, login_id, password=None, **extra):
        if not login_id:
            raise ValueError("login_id는 필수입니다.")
        user = self.model(login_id=login_id, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, login_id, password=None, **extra):
        extra.setdefault("nickname", login_id)
        extra.setdefault("role", Role.ADMIN)
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_leader", False)
        return self.create_user(login_id, password, **extra)


class User(AbstractBaseUser):
    user_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    login_id = models.CharField(max_length=50, unique=True)
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        db_column="team_id",
        related_name="members",
        null=True,
        blank=True,
    )
    password = models.CharField(max_length=255, db_column="password_hash")
    nickname = models.CharField(max_length=50)
    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.PARTICIPANT
    )
    is_leader = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    last_login = None

    USERNAME_FIELD = "login_id"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        db_table = "users"
        indexes = [
            models.Index(fields=["team"]),
            models.Index(fields=["team", "is_leader"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team"],
                condition=models.Q(is_leader=True),
                name="uq_users_one_leader_per_team",
            ),
        ]

    def __str__(self):
        return self.login_id

    def has_perm(self, perm, obj=None):
        return self.is_superuser

    def has_module_perms(self, app_label):
        return self.is_superuser


class RefreshToken(models.Model):
    token_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_column="user_id")
    token_hash = models.CharField(max_length=255, unique=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "refresh_tokens"
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        return f"{self.user.login_id} ({self.expires_at})"
