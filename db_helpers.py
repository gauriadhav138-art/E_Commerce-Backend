# db_helpers.py  -  small helper functions on top of YOUR existing database.py.
# It uses your get_connection(), so your MySQL settings stay in database.py.

from database import get_connection


def fetch_all(sql, params=()):
    """Run a SELECT and return ALL rows as a list of dictionaries."""
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, params)
        return cursor.fetchall()
    finally:
        conn.close()


def fetch_one(sql, params=()):
    """Run a SELECT and return the FIRST row (or None if nothing found)."""
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    """Run INSERT / UPDATE / DELETE. Returns the new id for INSERT."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def call_procedure(name, args=()):
    """
    Call a MySQL stored procedure.
    Returns two things:
      1) args  -> the argument list; OUT parameters are filled in
      2) result_sets -> any SELECT results the procedure produced
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        out_args = cursor.callproc(name, list(args))

        result_sets = []
        for result in cursor.stored_results():
            columns = result.column_names
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
            result_sets.append(rows)

        conn.commit()
        return out_args, result_sets
    finally:
        conn.close()