from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_has_no_production_access_or_publication() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "  contents: read" in workflow
    assert "push: false" in workflow
    for forbidden in (
        "write", "secrets.", "ssh ", "scp ", "environment:",
        "pull_request_target:", "docker login", "docker push", "deploy:",
    ):
        assert forbidden not in workflow
    assert list(WORKFLOW_PATH.parent.glob("*.yml")) == [WORKFLOW_PATH]
    assert not list(WORKFLOW_PATH.parent.glob("*.yaml"))


def test_ci_runs_database_and_javascript_regressions() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "postgres:16-alpine" in workflow
    assert "TEST_POSTGRES_URL=" in workflow
    assert "ALLOW_DESTRUCTIVE_POSTGRES_MIGRATION_TEST=1" in workflow
    assert "target: test" in workflow
    assert '"$IMAGE_NAME:ci" python -m pytest -q' in workflow
    assert "FROM node:24-bookworm-slim AS node" in dockerfile
    assert "COPY --from=node /usr/local/bin/node /usr/local/bin/node" in dockerfile
    assert "['File', 'FormData', 'Response', 'Headers']" in dockerfile
    assert "FROM app AS runtime\nENV APP_ENV=production" in dockerfile


def test_ci_keeps_secret_and_dependency_checks() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "fetch-depth: 0" in workflow
    assert "git /repo --redact=100 --no-banner" in workflow
    assert "--config /repo/.gitleaks.toml" in workflow
    assert "pip-audit" in workflow
    assert "pip list --format=freeze" in workflow
    assert "continue-on-error" not in workflow
    actions = re.findall(r"uses: (\S+)", workflow)
    assert actions
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) for action in actions)
