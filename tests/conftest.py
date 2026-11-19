"""Every new congregation gets the free trial (app/hear/db.py). The billing tests written before it test what
comes after the trial, so outside the trial's own tests (marked `free_trial`) congregations are made without one."""
import pytest

from app.hear import db


def pytest_configure(config):
    config.addinivalue_line("markers", "free_trial: new congregations get the free trial, as in production")


@pytest.fixture(autouse=True)
def _no_free_trial_unless_asked(request, monkeypatch):
    if request.node.get_closest_marker("free_trial") is None:
        monkeypatch.setattr(db, "NEW_CONGREGATIONS_GET_TRIAL", False)


@pytest.fixture(autouse=True)
def _no_trial_emails(monkeypatch):
    """Trial e-mails (app/hear/trial_emails.py) never go out from tests, whatever the shell has set."""
    monkeypatch.delenv("HC_TRIAL_EMAILS", raising=False)
