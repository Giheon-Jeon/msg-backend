from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.common.jwt import issue_access_token
from apps.koth.models import KothChallenge, KothClub, KothScorePeriod, KothSolve
from apps.koth.services import ScoreFetchError


class ConnectionPeriodTests(TestCase):
    def test_checks_the_last_closed_period_without_finalizing_or_awarding(self):
        user=User.objects.create_user(login_id="connection-admin",nickname="admin",role="ADMIN")
        club=KothClub.objects.create(name="connection-club")
        challenge=KothChallenge.objects.create(club=club,title="connection",open_group=1,status="SCHEDULED",
            inbound_internal_token_hash="a"*64,score_api_url="https://score.example/internal/koth/scores",score_api_token_env="QA_SCORE_TOKEN")
        client=APIClient();client.credentials(HTTP_AUTHORIZATION="Bearer "+issue_access_token(user))
        current=datetime(2026,10,26,15,0,tzinfo=timezone.utc)
        seen=[]
        def provider(_challenge,period):
            seen.append(period)
            if period+timedelta(minutes=15)>current:
                raise ScoreFetchError("PERIOD_NOT_READY")
            return {"code":"SUCCESS","data":None}
        with patch("apps.koth.services.current_period",return_value=current),patch("apps.koth.services._request_scores",side_effect=provider):
            response=client.post(f"/api/v1/admin/koth/challenges/{challenge.pk}/connection_test",{},format="json")
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.data["data"]["score_api"]["ok"])
        self.assertEqual(seen,[current-timedelta(minutes=15)])
        self.assertEqual(KothScorePeriod.objects.count(),0)
        self.assertEqual(KothSolve.objects.count(),0)
