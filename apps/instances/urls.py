from django.urls import path

from apps.instances.admin_views import ReleaseActivateView, ReleaseApproveView, ReleaseDeriveView, ReleaseListCreateView
from apps.instances.image_preparation_views import (
    ImagePreparationListCreateView, ImagePreparationDetailView,
    ImagePreparationRetryView, ImagePreparationRefreshView,
)
from apps.instances.deployment_test_views import DeploymentTestCreateView, DeploymentTestDetailView, DeploymentTestListView
from apps.instances.secret_views import RuntimeSecretListCreateView
from apps.instances.release_file_views import ReleaseFilesConfigureView
from apps.instances.environment_file_views import EnvironmentFileImportView, EnvironmentFilePreviewView
from apps.instances.repository_environment_views import RepositoryEnvironmentView, RepositoryEnvironmentPreviewView, RepositoryEnvironmentImportView
from apps.instances.views import (
    InstanceCreateView,
    InstanceDeleteView,
    InstanceExtendView,
    InstanceResetView,
    MyInstanceView,
)

urlpatterns = [
    path("admin/challenges/<uuid:challenge_id>/releases/<uuid:release_id>/image_preparations",
         ImagePreparationListCreateView.as_view(), name="admin-image-preparations"),
    path("admin/challenges/<uuid:challenge_id>/image_preparations/<uuid:request_id>",
         ImagePreparationDetailView.as_view(), name="admin-image-preparation-detail"),
    path("admin/challenges/<uuid:challenge_id>/image_preparations/<uuid:request_id>/retry",
         ImagePreparationRetryView.as_view(), name="admin-image-preparation-retry"),
    path("admin/challenges/<uuid:challenge_id>/image_preparations/<uuid:request_id>/refresh",
         ImagePreparationRefreshView.as_view(), name="admin-image-preparation-refresh"),
    path("admin/challenges/<uuid:challenge_id>/deployment_tests", DeploymentTestListView.as_view(), name="admin-deployment-tests"),
    path("admin/challenges/<uuid:challenge_id>/releases/<uuid:release_id>/deployment_tests", DeploymentTestCreateView.as_view(), name="admin-deployment-test-create"),
    path("admin/challenges/<uuid:challenge_id>/deployment_tests/<uuid:test_id>", DeploymentTestDetailView.as_view(), name="admin-deployment-test-detail"),
    path("admin/challenges/<uuid:challenge_id>/releases/<uuid:release_id>/files",
         ReleaseFilesConfigureView.as_view(), name="admin-release-files-configure"),
    path("admin/challenges/<uuid:challenge_id>/releases/<uuid:release_id>/repository_environment",
         RepositoryEnvironmentView.as_view(), name="admin-repository-environment"),
    path("admin/challenges/<uuid:challenge_id>/releases/<uuid:release_id>/repository_environment/preview",
         RepositoryEnvironmentPreviewView.as_view(), name="admin-repository-environment-preview"),
    path("admin/challenges/<uuid:challenge_id>/releases/<uuid:release_id>/repository_environment/import",
         RepositoryEnvironmentImportView.as_view(), name="admin-repository-environment-import"),
    path(
        "admin/challenges/<uuid:challenge_id>/releases/<uuid:release_id>/environment_files/preview",
        EnvironmentFilePreviewView.as_view(), name="admin-environment-file-preview",
    ),
    path(
        "admin/challenges/<uuid:challenge_id>/releases/<uuid:release_id>/environment_files/import",
        EnvironmentFileImportView.as_view(), name="admin-environment-file-import",
    ),
    path(
        "admin/challenges/<uuid:challenge_id>/releases/<uuid:release_id>/derive",
        ReleaseDeriveView.as_view(),
        name="admin-challenge-release-derive",
    ),
    path(
        "admin/challenges/<uuid:challenge_id>/runtime-secrets",
        RuntimeSecretListCreateView.as_view(),
        name="admin-runtime-secrets",
    ),
    path(
        "admin/challenges/<uuid:challenge_id>/releases",
        ReleaseListCreateView.as_view(),
        name="admin-challenge-releases",
    ),
    path(
        "admin/challenges/<uuid:challenge_id>/releases/<uuid:release_id>/approve",
        ReleaseApproveView.as_view(),
        name="admin-challenge-release-approve",
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
