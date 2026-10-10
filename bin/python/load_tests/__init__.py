"""Load-testing modules (locustfile + pure helpers). Package-ified
so the web app and tests can import step_profile / identity_pool directly;
the locustfile itself is still loaded by path (locust -f)."""
