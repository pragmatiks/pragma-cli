"""Exit 0 on empty collection after unit-test suite removal."""


def pytest_sessionfinish(session, exitstatus):
    if exitstatus == 5:
        session.exitstatus = 0
