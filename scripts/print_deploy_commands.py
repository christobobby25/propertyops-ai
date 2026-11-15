"""Print (never run) the commands to deploy the web app to Cloud Run.

    python -m scripts.print_deploy_commands --oauth-client-id <id>.apps.googleusercontent.com

Review, then run them yourself. They create a dedicated service account with
least-privilege roles, an Artifact Registry repo, build with Cloud Build, and
deploy with production auth (Google ID tokens), creation off, and conservative
scaling limits.
"""

from __future__ import annotations

import argparse
import shlex
import sys

from scripts._cli import load_settings

SERVICE = "propertyops-web"
SA_NAME = "propertyops-web"
BUILD_SA_NAME = "propertyops-build"
REPO = "propertyops"
ROLES = (
    "roles/datastore.user",  # Firestore read/write (work orders, proposals)
    "roles/aiplatform.user",  # Gemini + embeddings on Vertex AI
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
)


def commands(s, client_id: str, max_instances: int, enable_creation: bool) -> list[tuple[str, str]]:
    p, r = shlex.quote(s.project), shlex.quote(s.region)
    sa = f"{SA_NAME}@{s.project}.iam.gserviceaccount.com"
    build_sa = f"{BUILD_SA_NAME}@{s.project}.iam.gserviceaccount.com"
    image = f"{s.region}-docker.pkg.dev/{s.project}/{REPO}/{SERVICE}:$(git rev-parse --short HEAD)"
    env = ",".join(
        [
            "APP_ENV=production",
            "AUTH_MODE=google_id_token",
            f"OAUTH_AUDIENCE={client_id}",
            "GOOGLE_GENAI_USE_VERTEXAI=true",
            f"GOOGLE_CLOUD_PROJECT={s.project}",
            f"GOOGLE_CLOUD_LOCATION={s.location}",
            f"GOOGLE_CLOUD_REGION={s.region}",
            f"GEMINI_MODEL={s.model}",
            f"GCS_BUCKET={s.gcs_bucket}",
            f"FIRESTORE_DATABASE={s.firestore_database}",
            f"EMBEDDING_MODEL={s.embedding_model}",
            f"EMBEDDING_DIMENSIONS={s.embedding_dimensions}",
            f"ENABLE_WORK_ORDER_CREATION={'true' if enable_creation else 'false'}",
            "FIRESTORE_TARGET=cloud",
        ]
    )
    out = [
        (
            "Enable APIs",
            f"gcloud services enable run.googleapis.com artifactregistry.googleapis.com "
            f"cloudbuild.googleapis.com --project={p}",
        ),
        (
            "Dedicated runtime service account",
            f"gcloud iam service-accounts create {SA_NAME} --project={p} "
            f"--display-name='PropertyOps web runtime'",
        ),
    ]
    out += [
        (
            f"Grant {role}",
            f"gcloud projects add-iam-policy-binding {p} "
            f"--member=serviceAccount:{sa} --role={role} --condition=None",
        )
        for role in ROLES
    ]
    out += [
        (
            "Artifact Registry repository",
            f"gcloud artifacts repositories create {REPO} --repository-format=docker "
            f"--location={r} --project={p}",
        ),
        (
            "Dedicated build service account (read build source, push to this repo, write logs)",
            f"gcloud iam service-accounts create {BUILD_SA_NAME} --project={p} "
            f"&& gcloud storage buckets add-iam-policy-binding gs://{s.project}_cloudbuild "
            f"--member=serviceAccount:{build_sa} --role=roles/storage.objectViewer "
            f"&& gcloud artifacts repositories add-iam-policy-binding {REPO} --location={r} "
            f"--project={p} --member=serviceAccount:{build_sa} "
            f"--role=roles/artifactregistry.writer "
            f"&& gcloud projects add-iam-policy-binding {p} --member=serviceAccount:{build_sa} "
            f"--role=roles/logging.logWriter --condition=None",
        ),
        (
            "Build image (Cloud Build, cloudbuild.yaml)",
            f"gcloud builds submit --config cloudbuild.yaml "
            f"--substitutions=_TAG=$(git rev-parse --short HEAD) "
            f"--service-account=projects/{s.project}/serviceAccounts/{build_sa} --project={p}",
        ),
        (
            "Deploy (sessions are in memory: keep ONE instance; see README)",
            f"gcloud run deploy {SERVICE} --image {image} --region={r} --project={p} "
            f"--service-account={sa} --allow-unauthenticated "
            f"--min-instances=0 --max-instances={max_instances} --concurrency=20 "
            f"--cpu=1 --memory=1Gi --timeout=300 --session-affinity "
            f"--set-env-vars={shlex.quote(env)}",
        ),
        (
            "Provision a user (after they sign in once and you know their google:<sub>)",
            "python -m scripts.provision_identity --subject google:<sub> --role resident "
            "--resident RES-002 --apply",
        ),
    ]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--oauth-client-id", required=True)
    ap.add_argument("--max-instances", type=int, default=1)
    ap.add_argument("--enable-creation", action="store_true")
    args = ap.parse_args(argv)
    s = load_settings()
    if s is None:
        return 2
    print("# Cloud Run deployment. Review, then run these yourself.\n")
    for i, (title, cmd) in enumerate(
        commands(s, args.oauth_client_id, args.max_instances, args.enable_creation), 1
    ):
        print(f"# {i}. {title}\n{cmd}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
