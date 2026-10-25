"""Optional report hook: a yield fixture does not receive call-phase failures."""
import pytest
import json
from . import _context
from .client import Client, _json

_REPORTS = pytest.StashKey[dict]()
_TOKEN = pytest.StashKey[object]()
_EVIDENCE = pytest.StashKey[list]()
_EVIDENCE_TOKEN = pytest.StashKey[object]()


def pytest_addoption(parser):
    group = parser.getgroup("lisem")
    group.addoption("--lisem-executable", help="Lisem native RPC v1 CLI")
    group.addoption("--lisem-data-dir", help="Explicit SDK test library")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    reports = {}
    item.stash[_REPORTS] = reports
    evidence = []
    item.stash[_EVIDENCE] = evidence
    item.stash[_EVIDENCE_TOKEN] = _context.evidence_recorder.set(evidence.append)

    def failure():
        failed = [f"{phase}: {report.longrepr}" for phase, report in reports.items() if report.failed]
        return "\n".join(failed) if failed else None

    item.stash[_TOKEN] = _context.failure_provider.set(failure)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    item.stash.setdefault(_REPORTS, {})[report.when] = report
    if report.failed:
        # teardown reports arrive after yield fixtures have already closed.
        # Retag evidence saved on exit without needing the deleted instance.
        for path in item.stash.get(_EVIDENCE, []):
            try:
                value = json.loads(path.read_text())
                value["outcome"] = "failed"
                value["failure"] = "\n".join(
                    f"{phase}: {entry.longrepr}" for phase, entry in item.stash[_REPORTS].items()
                    if entry.failed)
                _json(path, value)
            except (OSError, ValueError) as exc:
                report.sections.append(("Lisem evidence update", str(exc)))


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    try:
        yield
    finally:
        token = item.stash.get(_TOKEN, None)
        if token is not None:
            _context.failure_provider.reset(token)
        evidence_token = item.stash.get(_EVIDENCE_TOKEN, None)
        if evidence_token is not None:
            _context.evidence_recorder.reset(evidence_token)


@pytest.fixture
def lisem_client(request, tmp_path):
    """Independent library by default; tests still choose their firmware/UID."""
    with Client(executable=request.config.getoption("--lisem-executable"),
                data_dir=request.config.getoption("--lisem-data-dir") or tmp_path / "library") as client:
        yield client
