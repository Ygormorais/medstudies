from sqlalchemy import event
from sqlalchemy.orm import Session, Query
from sqlalchemy.sql.elements import BinaryExpression


class TenantSession(Session):
    def __init__(self, bind, user_id: str, **kwargs):
        super().__init__(bind=bind, **kwargs)
        self.user_id = user_id


def _has_user_id_filter(criteria_tuple, user_id: str) -> bool:
    """Check whether the WHERE criteria already contain a user_id equality filter."""
    for clause in criteria_tuple:
        if _clause_has_user_id(clause, user_id):
            return True
    return False


def _clause_has_user_id(clause, user_id: str) -> bool:
    """Recursively check a single clause for user_id = <user_id>."""
    if isinstance(clause, BinaryExpression):
        left, right = clause.left, clause.right
        if (
            hasattr(left, "key")
            and left.key == "user_id"
            and hasattr(right, "value")
            and right.value == user_id
        ):
            return True
    if hasattr(clause, "clauses"):
        return any(_clause_has_user_id(c, user_id) for c in clause.clauses)
    return False


@event.listens_for(Query, "before_compile", retval=True)
def _scope_to_tenant(query):
    """Auto-inject WHERE user_id = ? for every query on a TenantSession."""
    session = query.session
    if not isinstance(session, TenantSession):
        return query
    # Skip if the user_id filter is already present (idempotency guard)
    if _has_user_id_filter(query._where_criteria, session.user_id):
        return query
    for desc in query.column_descriptions:
        entity = desc.get("entity")
        if entity and hasattr(entity, "user_id"):
            crit = entity.user_id == session.user_id
            if query._limit_clause is not None or query._offset_clause is not None:
                # LIMIT/OFFSET is already applied (e.g. re-compile after .first());
                # mutate _where_criteria directly to bypass the guard in .filter()
                query._where_criteria += (crit,)
            else:
                query = query.filter(crit)
    return query


@event.listens_for(Session, "before_flush")
def _set_user_id_on_new(session, flush_context, instances):
    """Auto-set user_id on new objects added to a TenantSession."""
    if not isinstance(session, TenantSession):
        return
    for obj in session.new:
        if hasattr(obj, "user_id") and obj.user_id is None:
            obj.user_id = session.user_id
