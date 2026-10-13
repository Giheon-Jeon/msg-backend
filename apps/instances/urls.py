from django.urls import path

from apps.instances.admin_views import ReleaseActivateView, ReleaseListCreateView
from apps.instances.views import (
    InstanceCreateView,
    InstanceDeleteView,
    InstanceExtendView,
    InstanceResetView,
    MyInstanceView,
)

urlpatterns = [
    path(
        "admin/challenges/<uuid:challenge_id>/releases",
        ReleaseListCreateView.as_view(),
        name="admin-challenge-releases",
    ),
    path(
        "admin/challenges/<uuid:challenge_id>/releases/<uuid:release_id>/activate",
        ReleaseActivateView.as_view(),
        name="admin-challenge-release-activate",
    ),
    path("instances", InstanceCreateView.as_view(), name="instance-create"),
    path(
        "instances/<uuid:instance_id>",
        InstanceDeleteView.as_view(),
        name="instance-delete",
    ),
    path(
        "instances/<uuid:instance_id>/reset",
        InstanceResetView.as_view(),
        name="instance-reset",
    ),
    path(
        "instances/<uuid:instance_id>/extend",
        InstanceExtendView.as_view(),
        name="instance-extend",
    ),
    path("teams/me/instances", MyInstanceView.as_view(), name="my-instances"),
]
