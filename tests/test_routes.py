"""End-to-end tests for the URL → handler routes index.

Covers the two extractors shipped in v1:

* **Django** — ``urls.py`` files with ``urlpatterns`` lists, including
  relative imports (``from . import views``) which resolve against the
  current package.
* **Next.js App Router** — ``app/**/route.{ts,tsx,js}`` files with
  exported HTTP-verb functions.

The motivation came from the biblereader benchmark: a fresh agent
spent 22 tool calls hunting for the Django view that handled a
specific URL because snapctx's symbol-graph layer was opaque to URL
routing. ``snapctx routes`` collapses that to one call.
"""

from __future__ import annotations

from pathlib import Path

from snapctx.api import index_root, list_routes, lookup_route


# ---------- Django ----------


def _build_django_repo(tmp_path: Path) -> Path:
    """Realistic Django shape: a project package + an app package, the
    app's urls.py imports views via ``from . import views``."""
    repo = tmp_path / "repo"
    (repo / "myapp").mkdir(parents=True)
    (repo / "myapp" / "__init__.py").write_text("")
    (repo / "myapp" / "views.py").write_text(
        "def list_users(request):\n    return None\n\n"
        "def get_user(request, pk):\n    return None\n"
    )
    (repo / "myapp" / "urls.py").write_text(
        "from django.urls import path\n"
        "from . import views\n"
        "\n"
        "urlpatterns = [\n"
        '    path("api/users/", views.list_users, name="user-list"),\n'
        '    path("api/users/<int:pk>/", views.get_user, name="user-detail"),\n'
        "]\n"
    )
    index_root(repo)
    return repo


def test_django_relative_import_resolves_to_view_qname(tmp_path: Path) -> None:
    repo = _build_django_repo(tmp_path)
    routes = list_routes(root=repo)["routes"]
    by_path = {r["path"]: r for r in routes}
    assert "api/users/" in by_path
    r = by_path["api/users/"]
    assert r["framework"] == "django"
    assert r["method"] == "ANY"
    assert r["handler_qname"] == "myapp.views:list_users"


def test_django_lookup_returns_exact_match(tmp_path: Path) -> None:
    repo = _build_django_repo(tmp_path)
    out = lookup_route("api/users/<int:pk>/", root=repo)
    assert out["matches"]
    assert out["matches"][0]["handler_qname"] == "myapp.views:get_user"


def test_django_lookup_misses_emit_actionable_hint(tmp_path: Path) -> None:
    repo = _build_django_repo(tmp_path)
    out = lookup_route("does/not/exist/", root=repo)
    assert out["matches"] == []
    assert "exact-match" in out["hint"].lower()


def test_django_include_emits_unresolved_row_with_framework_marker(
    tmp_path: Path,
) -> None:
    """``path("api/", include("myapp.urls"))`` — we don't follow
    cross-file include chains in v1, but we DO emit a row so the
    agent sees the include exists. Framework marker carries the
    target so a follow-up ``snapctx outline`` can drill in."""
    repo = tmp_path / "repo"
    (repo / "myapp").mkdir(parents=True)
    (repo / "myapp" / "__init__.py").write_text("")
    (repo / "myapp" / "views.py").write_text("def index(request):\n    return None\n")
    (repo / "myapp" / "urls.py").write_text(
        "from django.urls import path\n"
        "from . import views\n"
        "\n"
        "urlpatterns = [path('', views.index)]\n"
    )
    (repo / "project").mkdir()
    (repo / "project" / "__init__.py").write_text("")
    (repo / "project" / "urls.py").write_text(
        "from django.urls import path, include\n"
        "\n"
        "urlpatterns = [\n"
        '    path("api/", include("myapp.urls")),\n'
        "]\n"
    )
    index_root(repo)
    routes = list_routes(root=repo)["routes"]
    include_rows = [r for r in routes if r["framework"].startswith("django:include=")]
    assert any(r["framework"] == "django:include=myapp.urls" for r in include_rows)


# ---------- Next.js App Router ----------


def test_nextjs_route_extracts_per_verb_handlers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "app" / "api" / "users").mkdir(parents=True)
    (repo / "app" / "api" / "users" / "route.ts").write_text(
        "export async function GET(req: Request) {\n"
        '  return new Response("[]");\n'
        "}\n"
        "\n"
        "export async function POST(req: Request) {\n"
        '  return new Response("ok");\n'
        "}\n"
    )
    (repo / "package.json").write_text('{"name":"test"}\n')
    index_root(repo)
    routes = list_routes(root=repo)["routes"]
    by_method = {r["method"]: r for r in routes if r["framework"] == "nextjs"}
    assert "GET" in by_method
    assert "POST" in by_method
    assert by_method["GET"]["path"] == "/api/users"
    assert by_method["GET"]["handler_qname"] == "app/api/users/route:GET"
    assert by_method["POST"]["handler_qname"] == "app/api/users/route:POST"


def test_nextjs_dynamic_segment_preserved_in_url(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "app" / "api" / "users" / "[id]").mkdir(parents=True)
    (repo / "app" / "api" / "users" / "[id]" / "route.ts").write_text(
        "export async function GET(req: Request) {\n"
        "  return new Response();\n"
        "}\n"
    )
    (repo / "package.json").write_text('{"name":"test"}\n')
    index_root(repo)
    routes = list_routes(root=repo)["routes"]
    paths = [r["path"] for r in routes if r["framework"] == "nextjs"]
    assert "/api/users/[id]" in paths


def test_nextjs_non_route_file_does_not_extract(tmp_path: Path) -> None:
    """Files that aren't route.{ext} under app/ get nothing."""
    repo = tmp_path / "repo"
    (repo / "app" / "api" / "users").mkdir(parents=True)
    (repo / "app" / "api" / "users" / "helper.ts").write_text(
        "export async function GET() { return null; }\n"
    )
    (repo / "package.json").write_text('{"name":"test"}\n')
    index_root(repo)
    routes = list_routes(root=repo)["routes"]
    assert routes == []


# ---------- empty/missing ----------


def test_empty_repo_returns_hint(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("def f():\n    return 1\n")
    (repo / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\n')
    index_root(repo)
    out = list_routes(root=repo)
    assert out["routes"] == []
    assert "hint" in out
