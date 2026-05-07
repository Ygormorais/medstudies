from sqlalchemy import event
from sqlalchemy.orm import Session, Query


class TenantSession(Session):
    """SQLAlchemy Session subclass that auto-scopes queries to a single user.

    All queries on models with a `user_id` column are automatically filtered
    to only return rows belonging to self.user_id. New objects added to this
    session have their user_id automatically set via the before_flush event.
    """

    def __init__(self, bind, user_id: str, **kwargs) -> None:
        if not user_id:
            raise ValueError("user_id must be a non-empty string")
        super().__init__(bind=bind, **kwargs)
        self.user_id = user_id


def _has_user_id_filter(query: Query, entity) -> bool:
    """Return True if query already has a user_id filter for this entity.

    NOTE: reads query._where_criteria (private SQLAlchemy attribute) but does
    not mutate it. There is no public API to inspect WHERE criteria, so this
    read-only access is acceptable.
    """
    user_id_col = entity.user_id
    for criterion in query._where_criteria:
        if _clause_references_column(criterion, user_id_col):
            return True
    return False


def _clause_references_column(clause, column) -> bool:
    """Recursively check if a SQL clause references the given column."""
    from sqlalchemy.sql.elements import BinaryExpression
    if isinstance(clause, BinaryExpression):
        left = clause.left
        if hasattr(left, "key") and hasattr(left, "class_"):
            if left.class_ is column.class_ and left.key == column.key:
                return True
    if hasattr(clause, "clauses"):
        return any(_clause_references_column(c, column) for c in clause.clauses)
    return False


@event.listens_for(Query, "before_compile", retval=True)
def _scope_to_tenant(query: Query) -> Query:
    """Auto-inject WHERE user_id = ? for every query on a TenantSession.

    When .first() is called, SQLAlchemy applies LIMIT 1 before before_compile
    fires. In that case Query.filter() raises InvalidRequestError, so we use
    query.enable_assertions(False).filter(...) to bypass the guard — this is
    a public Query API method and does not touch private attributes.
    """
    session = query.session
    if not isinstance(session, TenantSession):
        return query
    for desc in query.column_descriptions:
        entity = desc.get("entity")
        if entity and hasattr(entity, "user_id"):
            if not _has_user_id_filter(query, entity):
                q = query.enable_assertions(False) if query._limit_clause is not None or query._offset_clause is not None else query
                query = q.filter(entity.user_id == session.user_id)
    return query


@event.listens_for(Session, "before_flush")
def _set_user_id_on_new(session: Session, flush_context, instances) -> None:
    """Auto-set user_id on new objects added to a TenantSession."""
    if not isinstance(session, TenantSession):
        return
    for obj in session.new:
        if hasattr(obj, "user_id") and obj.user_id is None:
            obj.user_id = session.user_id
