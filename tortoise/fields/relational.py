from typing import TYPE_CHECKING, Awaitable, Generic, Optional, TypeVar, Union

from pypika import Table
from typing_extensions import Literal

from tortoise.exceptions import ConfigurationError, NoValuesFetched, OperationalError
from tortoise.fields.base import CASCADE, RESTRICT, SET_NULL, Field

if TYPE_CHECKING:  # pragma: nocoverage
    from typing import Type
    from tortoise.models import Model
    from tortoise.queryset import QuerySet

MODEL = TypeVar("MODEL", bound="Model")

OneToOneNullableRelation = Union[Awaitable[Optional[MODEL]], Optional[MODEL]]
"""
Type hint for the result of accessing the :func:`.OneToOneField` field in the model
when obtained model can be nullable.
"""

OneToOneRelation = Union[Awaitable[MODEL], MODEL]
"""
Type hint for the result of accessing the :func:`.OneToOneField` field in the model.
"""

ForeignKeyNullableRelation = Union[Awaitable[Optional[MODEL]], Optional[MODEL]]
"""
Type hint for the result of accessing the :func:`.ForeignKeyField` field in the model
when obtained model can be nullable.
"""

ForeignKeyRelation = Union[Awaitable[MODEL], MODEL]
"""
Type hint for the result of accessing the :func:`.ForeignKeyField` field in the model.
"""


class ReverseRelation(Generic[MODEL]):
    """
    Relation container for :func:`.ForeignKeyField`.
    """

    def __init__(self, model, relation_field: str, instance) -> None:
        self.model = model
        self.relation_field = relation_field
        self.instance = instance
        self._fetched = False
        self._custom_query = False
        self.related_objects: list = []

    @property
    def _query(self):
        if not self.instance._saved_in_db:
            raise OperationalError(
                "This objects hasn't been instanced, call .save() before calling related queries"
            )
        return self.model.filter(**{self.relation_field: self.instance.pk})

    def __contains__(self, item) -> bool:
        if not self._fetched:
            raise NoValuesFetched(
                "No values were fetched for this relation, first use .fetch_related()"
            )
        return item in self.related_objects

    def __iter__(self):
        if not self._fetched:
            raise NoValuesFetched(
                "No values were fetched for this relation, first use .fetch_related()"
            )
        return self.related_objects.__iter__()

    def __len__(self) -> int:
        if not self._fetched:
            raise NoValuesFetched(
                "No values were fetched for this relation, first use .fetch_related()"
            )
        return len(self.related_objects)

    def __bool__(self) -> bool:
        if not self._fetched:
            raise NoValuesFetched(
                "No values were fetched for this relation, first use .fetch_related()"
            )
        return bool(self.related_objects)

    def __getitem__(self, item):
        if not self._fetched:
            raise NoValuesFetched(
                "No values were fetched for this relation, first use .fetch_related()"
            )
        return self.related_objects[item]

    def __await__(self):
        return self._query.__await__()

    async def __aiter__(self):
        if not self._fetched:
            self.related_objects = await self
            self._fetched = True

        for val in self.related_objects:
            yield val

    def filter(self, *args, **kwargs) -> "QuerySet[MODEL]":
        """
        Returns QuerySet with related elements filtered by args/kwargs.
        """
        return self._query.filter(*args, **kwargs)

    def all(self) -> "QuerySet[MODEL]":
        """
        Returns QuerySet with all related elements.
        """
        return self._query

    def order_by(self, *args, **kwargs) -> "QuerySet[MODEL]":
        """
        Returns QuerySet related elements in order.
        """
        return self._query.order_by(*args, **kwargs)

    def limit(self, *args, **kwargs) -> "QuerySet[MODEL]":
        """
        Returns a QuerySet with at most «limit» related elements.
        """
        return self._query.limit(*args, **kwargs)

    def offset(self, *args, **kwargs) -> "QuerySet[MODEL]":
        """
        Returns aQuerySet with all related elements offset by «offset».
        """
        return self._query.offset(*args, **kwargs)

    def _set_result_for_query(self, sequence) -> None:
        self._fetched = True
        self.related_objects = sequence


class ManyToManyRelation(ReverseRelation[MODEL]):
    """
    Many to many relation container for :func:`.ManyToManyField`.
    """

    def __init__(self, model, instance, m2m_field: "ManyToManyFieldInstance") -> None:
        super().__init__(model, m2m_field.related_name, instance)
        self.field = m2m_field
        self.model = m2m_field.model_class
        self.instance = instance

    async def add(self, *instances, using_db=None) -> None:
        """
        Adds one or more of ``instances`` to the relation.

        If it is already added, it will be silently ignored.
        """
        if not instances:
            return
        if not self.instance._saved_in_db:
            raise OperationalError(f"You should first call .save() on {self.instance}")
        db = using_db if using_db else self.model._meta.db
        pk_formatting_func = type(self.instance)._meta.pk.to_db_value
        related_pk_formatting_func = type(instances[0])._meta.pk.to_db_value
        through_table = Table(self.field.through)
        select_query = (
            db.query_class.from_(through_table)
            .where(
                getattr(through_table, self.field.backward_key)
                == pk_formatting_func(self.instance.pk, self.instance)
            )
            .select(self.field.backward_key, self.field.forward_key)
        )
        query = db.query_class.into(through_table).columns(
            getattr(through_table, self.field.forward_key),
            getattr(through_table, self.field.backward_key),
        )

        if len(instances) == 1:
            criterion = getattr(
                through_table, self.field.forward_key
            ) == related_pk_formatting_func(instances[0].pk, instances[0])
        else:
            criterion = getattr(through_table, self.field.forward_key).isin(
                [related_pk_formatting_func(i.pk, i) for i in instances]
            )

        select_query = select_query.where(criterion)

        # TODO: This is highly inefficient. Should use UNIQUE index by default.
        #  And optionally allow duplicates.
        already_existing_relations_raw = await db.execute_query(str(select_query))
        already_existing_relations = {
            (
                pk_formatting_func(r[self.field.backward_key], self.instance),
                related_pk_formatting_func(r[self.field.forward_key], self.instance),
            )
            for r in already_existing_relations_raw
        }

        insert_is_required = False
        for instance_to_add in instances:
            if not instance_to_add._saved_in_db:
                raise OperationalError(f"You should first call .save() on {instance_to_add}")
            pk_f = related_pk_formatting_func(instance_to_add.pk, instance_to_add)
            pk_b = pk_formatting_func(self.instance.pk, self.instance)
            if (pk_b, pk_f) in already_existing_relations:
                continue
            query = query.insert(pk_f, pk_b)
            insert_is_required = True
        if insert_is_required:
            await db.execute_query(str(query))

    async def clear(self, using_db=None) -> None:
        """
        Clears ALL relations.
        """
        db = using_db if using_db else self.model._meta.db
        through_table = Table(self.field.through)
        pk_formatting_func = type(self.instance)._meta.pk.to_db_value
        query = (
            db.query_class.from_(through_table)
            .where(
                getattr(through_table, self.field.backward_key)
                == pk_formatting_func(self.instance.pk, self.instance)
            )
            .delete()
        )
        await db.execute_query(str(query))

    async def remove(self, *instances, using_db=None) -> None:
        """
        Removes one or more of ``instances`` from the relation.
        """
        db = using_db if using_db else self.model._meta.db
        if not instances:
            raise OperationalError("remove() called on no instances")
        through_table = Table(self.field.through)
        pk_formatting_func = type(self.instance)._meta.pk.to_db_value
        related_pk_formatting_func = type(instances[0])._meta.pk.to_db_value

        if len(instances) == 1:
            condition = (
                getattr(through_table, self.field.forward_key)
                == related_pk_formatting_func(instances[0].pk, instances[0])
            ) & (
                getattr(through_table, self.field.backward_key)
                == pk_formatting_func(self.instance.pk, self.instance)
            )
        else:
            condition = (
                getattr(through_table, self.field.backward_key)
                == pk_formatting_func(self.instance.pk, self.instance)
            ) & (
                getattr(through_table, self.field.forward_key).isin(
                    [related_pk_formatting_func(i.pk, i) for i in instances]
                )
            )
        query = db.query_class.from_(through_table).where(condition).delete()
        await db.execute_query(str(query))


class ForeignKeyFieldInstance(Field):
    has_db_field = False

    def __init__(
        self,
        model_name: str,
        related_name: Union[Optional[str], Literal[False]] = None,
        on_delete=CASCADE,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        if len(model_name.split(".")) != 2:
            raise ConfigurationError('Foreign key accepts model name in format "app.Model"')
        self.model_class: "Type[Model]" = None  # type: ignore
        self.model_name = model_name
        self.related_name = related_name
        if on_delete not in {CASCADE, RESTRICT, SET_NULL}:
            raise ConfigurationError("on_delete can only be CASCADE, RESTRICT or SET_NULL")
        if on_delete == SET_NULL and not bool(kwargs.get("null")):
            raise ConfigurationError("If on_delete is SET_NULL, then field must have null=True set")
        self.on_delete = on_delete


class BackwardFKRelation(Field):
    has_db_field = False

    def __init__(
        self, field_type: "Type[Model]", relation_field: str, null: bool, description: Optional[str]
    ) -> None:
        super().__init__(null=null)
        self.model_class: "Type[Model]" = field_type
        self.relation_field: str = relation_field
        self.description: Optional[str] = description


class OneToOneFieldInstance(Field):
    has_db_field = False

    def __init__(
        self,
        model_name: str,
        related_name: Union[Optional[str], Literal[False]] = None,
        on_delete=CASCADE,
        **kwargs,
    ) -> None:
        kwargs["unique"] = True
        super().__init__(**kwargs)
        if len(model_name.split(".")) != 2:
            raise ConfigurationError('OneToOneField accepts model name in format "app.Model"')
        self.model_class: "Type[Model]" = None  # type: ignore
        self.model_name = model_name
        self.related_name = related_name
        if on_delete not in {CASCADE, RESTRICT, SET_NULL}:
            raise ConfigurationError("on_delete can only be CASCADE, RESTRICT or SET_NULL")
        if on_delete == SET_NULL and not bool(kwargs.get("null")):
            raise ConfigurationError("If on_delete is SET_NULL, then field must have null=True set")
        self.on_delete = on_delete


class BackwardOneToOneRelation(BackwardFKRelation):
    pass


class ManyToManyFieldInstance(Field):
    has_db_field = False
    field_type = ManyToManyRelation

    def __init__(
        self,
        model_name: str,
        through: Optional[str] = None,
        forward_key: Optional[str] = None,
        backward_key: str = "",
        related_name: str = "",
        field_type: "Type[Model]" = None,  # type: ignore
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.model_class: "Type[Model]" = field_type
        if len(model_name.split(".")) != 2:
            raise ConfigurationError('Foreign key accepts model name in format "app.Model"')
        self.model_name: str = model_name
        self.related_name: str = related_name
        self.forward_key: str = forward_key or f"{model_name.split('.')[1].lower()}_id"
        self.backward_key: str = backward_key
        self.through: Optional[str] = through
        self._generated: bool = False


def OneToOneField(
    model_name: str,
    related_name: Union[Optional[str], Literal[False]] = None,
    on_delete=CASCADE,
    **kwargs,
) -> OneToOneRelation:
    """
    OneToOne relation field.

    This field represents a foreign key relation to another model.

    See :ref:`one_to_one` for usage information.

    You must provide the following:

    ``model_name``:
        The name of the related model in a :samp:`'{app}.{model}'` format.

    The following is optional:

    ``related_name``:
        The attribute name on the related model to reverse resolve the foreign key.
    ``on_delete``:
        One of:
            ``field.CASCADE``:
                Indicate that the model should be cascade deleted if related model gets deleted.
            ``field.RESTRICT``:
                Indicate that the related model delete will be restricted as long as a
                foreign key points to it.
            ``field.SET_NULL``:
                Resets the field to NULL in case the related model gets deleted.
                Can only be set if field has ``null=True`` set.
            ``field.SET_DEFAULT``:
                Resets the field to ``default`` value in case the related model gets deleted.
                Can only be set is field has a ``default`` set.
    """

    return OneToOneFieldInstance(model_name, related_name, on_delete, **kwargs)


def ForeignKeyField(
    model_name: str,
    related_name: Union[Optional[str], Literal[False]] = None,
    on_delete=CASCADE,
    **kwargs,
) -> ForeignKeyRelation:
    """
    ForeignKey relation field.

    This field represents a foreign key relation to another model.

    See :ref:`foreign_key` for usage information.

    You must provide the following:

    ``model_name``:
        The name of the related model in a :samp:`'{app}.{model}'` format.

    The following is optional:

    ``related_name``:
        The attribute name on the related model to reverse resolve the foreign key.
    ``on_delete``:
        One of:
            ``field.CASCADE``:
                Indicate that the model should be cascade deleted if related model gets deleted.
            ``field.RESTRICT``:
                Indicate that the related model delete will be restricted as long as a
                foreign key points to it.
            ``field.SET_NULL``:
                Resets the field to NULL in case the related model gets deleted.
                Can only be set if field has ``null=True`` set.
            ``field.SET_DEFAULT``:
                Resets the field to ``default`` value in case the related model gets deleted.
                Can only be set is field has a ``default`` set.
    """

    return ForeignKeyFieldInstance(model_name, related_name, on_delete, **kwargs)


def ManyToManyField(
    model_name: str,
    through: Optional[str] = None,
    forward_key: Optional[str] = None,
    backward_key: str = "",
    related_name: str = "",
    **kwargs,
) -> "ManyToManyRelation":
    """
    ManyToMany relation field.

    This field represents a many-to-many between this model and another model.

    See :ref:`many_to_many` for usage information.

    You must provide the following:

    ``model_name``:
        The name of the related model in a :samp:`'{app}.{model}'` format.

    The following is optional:

    ``through``:
        The DB table that represents the trough table.
        The default is normally safe.
    ``forward_key``:
        The forward lookup key on the through table.
        The default is normally safe.
    ``backward_key``:
        The backward lookup key on the through table.
        The default is normally safe.
    ``related_name``:
        The attribute name on the related model to reverse resolve the many to many.
    """

    return ManyToManyFieldInstance(  # type: ignore
        model_name, through, forward_key, backward_key, related_name, **kwargs
    )
