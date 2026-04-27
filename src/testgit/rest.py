# ABOUTME: REST API routes mirroring the subset of GitHub's REST surface that gh needs.
# ABOUTME: For github.localhost gh hits these without an /api/v3 prefix, so they live at the root.

from __future__ import annotations

from fastapi import APIRouter, Response

from testgit.config import Settings


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/")
    def root() -> Response:
        # `gh auth status` issues a GET to the API root purely to read the
        # X-OAuth-Scopes header. It does not parse the body, so {} is enough.
        return Response(
            content="{}",
            media_type="application/json",
            headers={"X-OAuth-Scopes": ", ".join(settings.oauth_scopes)},
        )

    return router
