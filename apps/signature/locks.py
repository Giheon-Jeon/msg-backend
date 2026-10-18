import hashlib

from django.db import connection
from django.db.transaction import TransactionManagementError


def lock_signature(signature_id, *, shared=False):
    if not connection.in_atomic_block:
        raise TransactionManagementError("Signature locks require an atomic transaction")
    key = int.from_bytes(
        hashlib.sha256(f"msg:signature:{signature_id}".encode("ascii")).digest()[:8],
        "big", signed=True,
    )
    # Queue pending admin writes ahead of new readers; row share locks alone can starve writers.
    sql = (
        "SELECT pg_advisory_xact_lock_shared(%s)" if shared
        else "SELECT pg_advisory_xact_lock(%s)"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, [key])
