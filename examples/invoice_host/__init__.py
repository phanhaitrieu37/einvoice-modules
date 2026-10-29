"""Example host for invoice-module on top of payment-module.

``main`` builds both modules on one database, onboards them and runs an end-to-end smoke;
``server`` mounts the payment-module webhook router behind a real HTTP listener. Tables come
from one ``MetaData`` with ``create_all`` (the dev path); a host in production runs the
migrations of both packages instead.
"""
