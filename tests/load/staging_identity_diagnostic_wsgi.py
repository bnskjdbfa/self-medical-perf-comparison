"""Diagnostic variant; original staging entrypoint enforces token/build/DB guards."""
from staging_perf_wsgi import application
from staging_identity_diagnostics import install

install()
