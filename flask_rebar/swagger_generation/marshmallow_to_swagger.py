"""
    Marshmallow to Swagger Conversion
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Utilities for converting Marshmallow objects to their
    corresponding Swagger representation.

    :copyright: Copyright 2018 PlanGrid, Inc., see AUTHORS.
    :license: MIT, see LICENSE for details.
"""
import copy
import inspect
import logging
import sys
from collections import namedtuple
from typing import (
    overload,
    Any,
    Callable,
    Dict,
    Generic,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from typing_extensions import ParamSpec
import marshmallow as m
from marshmallow import Schema
from marshmallow.validate import Range
from marshmallow.validate import OneOf
from marshmallow.validate import Length
from marshmallow.validate import Validator

from flask_rebar import compat
from flask_rebar.authenticators import Authenticator
from flask_rebar.validation import QueryParamList
from flask_rebar.validation import CommaSeparatedList
from flask_rebar.swagger_generation import swagger_words as sw

# for easier type hinting
MarshmallowObject = Union[Schema, m.fields.Field, Validator]
T = TypeVar("T", bound=MarshmallowObject)
TField = TypeVar("TField", bound=m.fields.Field)

# for type hinting decorators
S = TypeVar("S")
P = ParamSpec("P")

LoadDumpOptions = None
try:
    EnumField: Any = m.fields.Enum
except AttributeError:
    try:
        from marshmallow_enum import EnumField, LoadDumpOptions  # type: ignore
    except ImportError:
        EnumField = None


# Special value to signify that a JSONSchema field should be left unset
class UNSET:
    pass


# We'll use this to mark methods as JSONSchema attribute setters
_method_marker = "__sets_jsonschema_attr__"

# Holds attributes that we can pass around in these recursive
# calls to converters. Bit messy, but :shrug:
_Context = namedtuple(
    "_Context",
    [
        # This will hold a reference to a convert method that can be used
        # to make recursive calls
        "convert",
        # Only really using this for validators at the moment. It will hold the
        # JSONSchema object that's been converter so far, so that the validator
        # can be converted based on the type of the schema.
        "memo",
        # The current schema being converted.
        "schema",
        # The major version of OpenAPI being converter for
        "openapi_version",
    ],
)


class UnregisteredType(Exception):
    pass


@overload
def _normalize_validate(validate: None) -> None:
    ...


@overload
def _normalize_validate(
    validate: Union[Callable, Iterable[Callable]]
) -> Iterable[Callable]:
    ...


def _normalize_validate(
    validate: Optional[Union[Callable, Iterable[Callable]]]
) -> Optional[Iterable[Callable]]:
    """
    Coerces the validate attribute on a Marshmallow field to a consistent type.

    The validate attribute on a Marshmallow field can either be a single
    Validator or a collection of Validators.

    :param Validator|list[Validator] validate:
    :rtype: list[Validator]
    """
    if callable(validate):
        return [validate]
    else:
        return validate


def get_swagger_title(obj: MarshmallowObject) -> str:
    """
    Gets a title for the given object. This title will be used
    as a name/key for the object in swagger.

    :param obj:
    :rtype: str
    """
    if hasattr(obj, "__swagger_title__"):
        return obj.__swagger_title__
    elif hasattr(obj, "__name__"):
        return obj.__name__
    else:
        return obj.__class__.__name__


def sets_swagger_attr(attr: str) -> Callable:
    """
    Decorates a `MarshmallowConverter` method, marking it as an JSONSchema
    attribute setter.

    Example usage::

        class Converter(MarshmallowConverter):
            MARSHMALLOW_TYPE = String()

            @sets_swagger_attr('type')
            def get_type(obj, context):
                return 'string'

    This converter receives instances of `String` and converts it to a
    JSONSchema object that looks like `{'type': 'string'}`.

    :param str attr: The attribute to set
    """

    def wrapper(f: Callable[P, T]) -> Callable[P, T]:
        setattr(f, _method_marker, attr)
        return f

    return wrapper


def get_schema_fields(schema: Schema) -> List[Tuple[str, m.fields.Field]]:
    """Retrieve all the names and field objects for a marshmallow Schema

    :param m.Schema schema:
    :returns: Yields tuples of the field name and the field itself
    :rtype: typing.Iterator[typing.Tuple[str, m.fields.Field]]
    """
    fields: List[Tuple[str, m.fields.Field]] = []
    for name, field in schema.fields.items():
        prop = compat.get_data_key(field)
        fields.append((prop, field))
    return sorted(fields)


class MarshmallowConverter(Generic[T]):
    """
    Abstract class for objects that convert Marshmallow objects to
    JSONSchema dictionaries.
    """

    MARSHMALLOW_TYPE: Any = None

    def convert(self, obj: T, context: _Context) -> Dict[str, Union[str, bool]]:
        """
        Converts a Marshmallow object to a JSONSchema dictionary.

        This inspects the converter instance for methods that have been
        marked as attribute setters, calling them to set attributes on the
        resulting JSONSchema dictionary.

        :param m.Schema|m.fields.Field|Validator obj:
            The Marshmallow object to be converted
        :param _Context context:
            Various information to help the converter understand how to
            convert the object.
        :rtype: dict
        """
        jsonschema_obj = {}

        for _, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if hasattr(method, _method_marker):
                val = method(obj, context)
                if val is not UNSET:
                    jsonschema_obj[getattr(method, _method_marker)] = val

        return jsonschema_obj


class SchemaConverter(MarshmallowConverter[Schema]):
    """Converts Marshmallow Schema objects."""

    MARSHMALLOW_TYPE = m.Schema

    @sets_swagger_attr(sw.type_)
    def get_type(self, obj: Schema, context: _Context) -> str:
        if obj.many:
            return sw.array
        else:
            return sw.object_

    @sets_swagger_attr(sw.items)
    def get_items(
        self, obj: Schema, context: _Context
    ) -> Union[Type[UNSET], MarshmallowObject]:
        if not obj.many:
            return UNSET

        singular_obj = copy.deepcopy(obj)
        singular_obj.many = False

        return context.convert(singular_obj, context)

    @sets_swagger_attr(sw.properties)
    def get_properties(
        self, obj: Schema, context: _Context
    ) -> Union[Type[UNSET], Dict[str, Any]]:
        if obj.many:
            return UNSET

        properties = {}

        for prop, field in get_schema_fields(obj):
            properties[prop] = context.convert(field, context)

        return properties

    @sets_swagger_attr(sw.required)
    def get_required(
        self, obj: Schema, context: _Context
    ) -> Union[Type[UNSET], List[str]]:
        if obj.many or obj.partial is True:
            return UNSET

        required: List[str] = []
        obj_partial_is_collection = m.utils.is_collection(obj.partial)

        for name, field in obj.fields.items():
            if field.required:
                prop = compat.get_data_key(field)
                if obj_partial_is_collection and obj.partial and prop in obj.partial:
                    continue
                required.append(prop)

        if required and not obj.ordered:
            required = sorted(required)
        return required if required else UNSET

    @sets_swagger_attr(sw.description)
    def get_description(
        self, obj: Schema, context: _Context
    ) -> Union[Type[UNSET], str]:
        if obj.many:
            return UNSET
        elif obj.__doc__:
            return obj.__doc__
        else:
            return UNSET

    @sets_swagger_attr(sw.title)
    def get_title(self, obj: Schema, context: _Context) -> Union[Type[UNSET], str]:
        if not obj.many:
            return get_swagger_title(obj)
        else:
            return UNSET

    @sets_swagger_attr(sw.additional_properties)
    def get_additional_properties(self, obj: Schema, context: _Context) -> bool:
        if obj.unknown in (m.RAISE, m.EXCLUDE):
            return False
        elif obj.unknown is m.INCLUDE:
            return True
        else:
            raise ValueError(
                f"Unexpected Schema.unknown value {obj.unknown} for {obj} "
            )


class FieldConverter(MarshmallowConverter, Generic[TField]):
    """
    Base Converter for Marshmallow Field objects.

    This should be extended for specific Field types.
    """

    MARSHMALLOW_TYPE: Type[m.fields.Field] = m.fields.Field

    def convert(self, obj: TField, context: _Context) -> Dict[str, Union[str, bool]]:
        jsonschema_obj = super().convert(obj, context)

        if obj.dump_only:
            jsonschema_obj["readOnly"] = True

        if obj.validate:
            validators = _normalize_validate(obj.validate)
            for validator in validators:
                try:
                    jsonschema_obj.update(
                        context.convert(
                            obj=validator,
                            context=_Context(
                                convert=context.convert,
                                memo=jsonschema_obj,
                                schema=context.schema,
                                openapi_version=context.openapi_version,
                            ),
                        )
                    )
                except UnregisteredType as e:
                    logging.debug(
                        "Unable to convert validator {validator}: {err}".format(
                            validator=validator, err=e
                        )
                    )

        return jsonschema_obj

    # With OpenApi 3.1 nullable has been removed entirely
    # and allowing 'none' means we return an array of allowed types that includes sw.null
    def null_type_determination(
        self, obj: TField, context: _Context, sw_type: str
    ) -> Union[str, List[str]]:
        if context.openapi_version == 3 and obj.allow_none:
            return [sw_type, sw.null]
        else:
            return sw_type

    @sets_swagger_attr(sw.default)
    def get_default(self, obj: TField, context: _Context) -> Any:
        if (
            obj.load_default is not m.missing
            # Marshmallow accepts a callable for the default. This is tricky
            # to handle, so let's just ignore this for now.
            and not callable(obj.load_default)
        ):
            return obj.load_default
        else:
            return UNSET

    @sets_swagger_attr(sw.nullable)
    def get_nullable(self, obj: TField, context: _Context) -> Union[Type[UNSET], bool]:
        if context.openapi_version == 2 and obj.allow_none is not False:
            return True
        else:
            return UNSET

    @sets_swagger_attr(sw.description)
    def get_description(
        self, obj: TField, context: _Context
    ) -> Union[Type[UNSET], str]:
        if "description" in obj.metadata:
            return obj.metadata["description"]
        else:
            return UNSET


class ValidatorConverter(MarshmallowConverter[Validator]):
    """
    Base Converter for Marshmallow Validator objects.

    This should be extended for specific Validator types.
    """

    MARSHMALLOW_TYPE: Union[Type[Validator], Type[OneOf]] = Validator


class NestedConverter(FieldConverter[m.fields.Nested]):
    MARSHMALLOW_TYPE = m.fields.Nested

    def convert(self, obj: m.fields.Nested, context: _Context) -> Dict[str, Any]:
        return context.convert(obj.schema, context)


class ListConverter(FieldConverter[m.fields.List]):
    MARSHMALLOW_TYPE = m.fields.List

    @sets_swagger_attr(sw.type_)
    def get_type(self, obj: m.fields.List, context: _Context) -> Union[str, List[str]]:
        return self.null_type_determination(obj, context, sw.array)

    @sets_swagger_attr(sw.items)
    def get_items(
        self, obj: m.fields.List, context: _Context
    ) -> Union[Type[UNSET], m.fields.List]:
        return context.convert(obj.inner, context)


class DictConverter(FieldConverter[m.fields.Dict]):
    MARSHMALLOW_TYPE = m.fields.Dict

    @sets_swagger_attr(sw.type_)
    def get_type(self, obj: m.fields.Dict, context: _Context) -> Union[str, List[str]]:
        return self.null_type_determination(obj, context, sw.object_)


class IntegerConverter(FieldConverter[m.fields.Integer]):
    MARSHMALLOW_TYPE = m.fields.Integer

    @sets_swagger_attr(sw.type_)
    def get_type(
        self, obj: m.fields.Integer, context: _Context
    ) -> Union[str, List[str]]:
        return self.null_type_determination(obj, context, sw.integer)


class StringConverter(FieldConverter[m.fields.String]):
    MARSHMALLOW_TYPE = m.fields.String

    @sets_swagger_attr(sw.type_)
    def get_type(
        self, obj: m.fields.String, context: _Context
    ) -> Union[str, List[str]]:
        return self.null_type_determination(obj, context, sw.string)


class NumberConverter(FieldConverter[m.fields.Number]):
    MARSHMALLOW_TYPE = m.fields.Number

    @sets_swagger_attr(sw.type_)
    def get_type(
        self, obj: m.fields.Number, context: _Context
    ) -> Union[str, List[str]]:
        return self.null_type_determination(obj, context, sw.number)


class BooleanConverter(FieldConverter[m.fields.Boolean]):
    MARSHMALLOW_TYPE = m.fields.Boolean

    @sets_swagger_attr(sw.type_)
    def get_type(
        self, obj: m.fields.Boolean, context: _Context
    ) -> Union[str, List[str]]:
        return self.null_type_determination(obj, context, sw.boolean)


class DateTimeConverter(FieldConverter[m.fields.DateTime]):
    MARSHMALLOW_TYPE = m.fields.DateTime

    @sets_swagger_attr(sw.type_)
    def get_type(
        self, obj: m.fields.DateTime, context: _Context
    ) -> Union[str, List[str]]:
        return self.null_type_determination(obj, context, sw.string)

    @sets_swagger_attr(sw.format_)
    def get_format(self, obj: m.fields.DateTime, context: _Context) -> str:
        return sw.date_time


class UUIDConverter(FieldConverter[m.fields.UUID]):
    MARSHMALLOW_TYPE = m.fields.UUID

    @sets_swagger_attr(sw.type_)
    def get_type(self, obj: m.fields.UUID, context: _Context) -> Union[str, List[str]]:
        return self.null_type_determination(obj, context, sw.string)

    @sets_swagger_attr(sw.format_)
    def get_format(self, obj: m.fields.UUID, context: _Context) -> str:
        return sw.uuid


class DateConverter(FieldConverter[m.fields.Date]):
    MARSHMALLOW_TYPE = m.fields.Date

    @sets_swagger_attr(sw.type_)
    def get_type(self, obj: m.fields.Date, context: _Context) -> Union[str, List[str]]:
        return self.null_type_determination(obj, context, sw.string)

    @sets_swagger_attr(sw.format_)
    def get_format(self, obj: m.fields.Date, context: _Context) -> str:
        return sw.date


class MethodConverter(FieldConverter[m.fields.Method]):
    MARSHMALLOW_TYPE = m.fields.Method

    @sets_swagger_attr(sw.type_)
    def get_type(
        self, obj: m.fields.Method, context: _Context
    ) -> Union[str, List[str]]:
        if "swagger_type" in obj.metadata:
            return self.null_type_determination(
                obj, context, obj.metadata["swagger_type"]
            )
        else:
            raise ValueError(
                'Must include "swagger_type" ' "keyword argument in Method field"
            )


class FunctionConverter(FieldConverter[m.fields.Function]):
    MARSHMALLOW_TYPE = m.fields.Function

    @sets_swagger_attr(sw.type_)
    def get_type(
        self, obj: m.fields.Function, context: _Context
    ) -> Union[str, List[str]]:
        if "swagger_type" in obj.metadata:
            return self.null_type_determination(
                obj, context, obj.metadata["swagger_type"]
            )
        else:
            raise ValueError(
                'Must include "swagger_type" ' "keyword argument in Function field"
            )


class ConstantConverter(FieldConverter[m.fields.Constant]):
    MARSHMALLOW_TYPE = m.fields.Constant

    @sets_swagger_attr(sw.enum)
    def get_enum(self, obj: m.fields.Constant, context: _Context) -> List[str]:
        return [obj.constant]


class CsvArrayConverter(ListConverter):
    MARSHMALLOW_TYPE = CommaSeparatedList

    @sets_swagger_attr(sw.collection_format)
    def get_collection_format(
        self, obj: CommaSeparatedList, context: _Context
    ) -> Union[Type[UNSET], str]:
        return sw.csv if context.openapi_version == 2 else UNSET

    @sets_swagger_attr(sw.style)
    def get_style(
        self, obj: CommaSeparatedList, context: _Context
    ) -> Union[Type[UNSET], str]:
        return sw.form if context.openapi_version == 3 else UNSET

    @sets_swagger_attr(sw.explode)
    def get_explode(
        self, obj: CommaSeparatedList, context: _Context
    ) -> Union[Type[UNSET], bool]:
        return False if context.openapi_version == 3 else UNSET


class MultiArrayConverter(ListConverter):
    MARSHMALLOW_TYPE = QueryParamList

    @sets_swagger_attr(sw.collection_format)
    def get_collection_format(
        self, obj: QueryParamList, context: _Context
    ) -> Union[Type[UNSET], str]:
        return sw.multi if context.openapi_version == 2 else UNSET

    @sets_swagger_attr(sw.explode)
    def get_explode(
        self, obj: QueryParamList, context: _Context
    ) -> Union[Type[UNSET], bool]:
        return True if context.openapi_version == 3 else UNSET


class RangeConverter(ValidatorConverter):
    MARSHMALLOW_TYPE = Range

    @sets_swagger_attr(sw.minimum)
    def get_minimum(
        self, obj: Range, context: _Context
    ) -> Union[Type[UNSET], int, float]:
        if obj.min is not None:
            return obj.min
        else:
            return UNSET

    @sets_swagger_attr(sw.maximum)
    def get_maximum(
        self, obj: Range, context: _Context
    ) -> Union[Type[UNSET], int, float]:
        if obj.max is not None:
            return obj.max
        else:
            return UNSET


class OneOfConverter(ValidatorConverter):
    MARSHMALLOW_TYPE = OneOf

    @sets_swagger_attr(sw.enum)
    def get_enum(self, obj: OneOf, context: _Context) -> List[str]:
        return list(obj.choices)


class LengthConverter(ValidatorConverter):
    MARSHMALLOW_TYPE = Length

    @sets_swagger_attr(sw.min_items)
    def get_minimum_items(
        self, obj: Length, context: _Context
    ) -> Union[Type[UNSET], int]:
        if context.memo[sw.type_] == sw.array:
            if obj.min is not None:
                return obj.min
        return UNSET

    @sets_swagger_attr(sw.max_items)
    def get_maximum_items(
        self, obj: Length, context: _Context
    ) -> Union[Type[UNSET], int]:
        if context.memo[sw.type_] == sw.array:
            if obj.max is not None:
                return obj.max
        return UNSET

    @sets_swagger_attr(sw.min_length)
    def get_minimum_length(
        self, obj: Length, context: _Context
    ) -> Union[Type[UNSET], int]:
        if context.memo[sw.type_] == sw.string:
            if obj.min is not None:
                return obj.min
        return UNSET

    @sets_swagger_attr(sw.max_length)
    def get_maximum_length(
        self, obj: Length, context: _Context
    ) -> Union[Type[UNSET], int]:
        if context.memo[sw.type_] == sw.string:
            if obj.max is not None:
                return obj.max
        return UNSET


class ConverterRegistry:
    """
    Registry for MarshmallowConverters.

    Schemas for responses, query strings, request bodies, etc. need to
    be converted differently. For example, response schemas as "dump"ed and
    request body schemas are "loaded". For another example, query strings
    don't support nesting.

    This registry also allows for additional converters to be added for custom
    Marshmallow types.
    """

    def __init__(self) -> None:
        self._type_map: Dict[
            Union[Type[MarshmallowObject], Type[Authenticator]], MarshmallowConverter
        ] = {}
        # self._validator_map = {}

    def register_type(self, converter: MarshmallowConverter) -> None:
        """
        Registers a converter.

        :param MarshmallowConverter converter:
        """
        self._type_map[converter.MARSHMALLOW_TYPE] = converter

    def register_types(self, converters: Iterable[MarshmallowConverter]) -> None:
        """
        Registers multiple converters.

        :param iterable[MarshmallowConverter] converters:
        """
        for converter in converters:
            self.register_type(converter)

    def _get_converter_for_type(self, obj: MarshmallowObject) -> MarshmallowConverter:
        """
        Locates the registered converter for a given type.
        :param obj: instance to convert
        :return: converter for type of instance
        """
        method_resolution_order = obj.__class__.__mro__

        for cls in method_resolution_order:
            if cls in self._type_map:
                return self._type_map[cls]
        else:
            raise UnregisteredType(
                "No registered type found in method resolution order: {mro}\n"
                "Registered types: {types}".format(
                    mro=method_resolution_order, types=list(self._type_map.keys())
                )
            )

    def _convert(
        self, obj: MarshmallowObject, context: _Context
    ) -> Dict[str, Union[str, bool]]:
        """
        Converts a Marshmallow object to a JSONSchema dictionary.

        :param m.Schema|m.fields.Field|Validator obj:
            The Marshmallow object to be converted
        :param _Context context:
            Various information to help the converter understand how to
            convert the given object.

            This helps with all the recursive nonsense.
        :rtype: dict
        """
        return self._get_converter_for_type(obj).convert(obj=obj, context=context)

    def convert(
        self, obj: MarshmallowObject, openapi_version: int = 2
    ) -> Dict[str, Any]:
        """
        Converts a Marshmallow object to a JSONSchema dictionary.

        :param m.Schema|m.fields.Field|Validator obj:
            The Marshmallow object to be converted
        :param int openapi_version: major version of OpenAPI to convert obj for
        :rtype: dict
        """
        return self._convert(
            obj=obj,
            context=_Context(
                convert=self._convert,
                memo={},
                schema=obj,
                openapi_version=openapi_version,
            ),
        )


class EnumConverter(FieldConverter):
    MARSHMALLOW_TYPE = EnumField  # type: ignore
    # Note that `obj` is typed as Any in this converter because mypy has great difficulty
    # trying to sort out type hints for EnumField, since it could be either m.fields.Enum
    # (marshmallow >= 3.18) or marshmallow_enum.EnumField

    @sets_swagger_attr(sw.type_)
    def get_type(self, obj: Any, context: _Context) -> Union[str, List[str]]:
        # Note: we don't (yet?) support mix-and-match between load_by and dump_by. Pick one.
        if obj.by_value or (
            LoadDumpOptions is not None
            and obj.load_by == obj.dump_by == LoadDumpOptions.value
        ):
            # I'm going out on a limb and assuming your enum uses same type for all vals, else caveat emptor:
            value_type = type(next(iter(obj.enum)).value)
            if value_type is int:
                return self.null_type_determination(obj, context, sw.integer)
            elif value_type is float:
                return self.null_type_determination(obj, context, sw.number)
            else:
                return self.null_type_determination(obj, context, sw.string)
        else:
            return self.null_type_determination(obj, context, sw.string)

    @sets_swagger_attr(sw.enum)
    def get_enum(self, obj: Any, context: _Context) -> List[str]:
        if obj.by_value or (
            LoadDumpOptions is not None
            and obj.load_by == obj.dump_by == LoadDumpOptions.value
        ):
            return [entry.value for entry in obj.enum]
        else:
            return [entry.name for entry in obj.enum]


ALL_CONVERTERS = tuple(
    [
        klass()
        for _, klass in inspect.getmembers(sys.modules[__name__], inspect.isclass)
        if issubclass(klass, MarshmallowConverter)
    ]
)


def _common_converters() -> List[MarshmallowConverter]:
    """Instantiates the converters we use in ALL of the registries below"""
    converters: List[MarshmallowConverter] = [
        BooleanConverter(),
        DateConverter(),
        DateTimeConverter(),
        FunctionConverter(),
        IntegerConverter(),
        LengthConverter(),
        ListConverter(),
        MethodConverter(),
        NumberConverter(),
        OneOfConverter(),
        RangeConverter(),
        SchemaConverter(),
        StringConverter(),
        UUIDConverter(),
        ConstantConverter(),
    ]
    if EnumConverter.MARSHMALLOW_TYPE is not None:  # type: ignore
        converters.append(EnumConverter())

    return converters


query_string_converter_registry: ConverterRegistry = ConverterRegistry()
query_string_converter_registry.register_types(
    _common_converters() + [CsvArrayConverter(), MultiArrayConverter()]
)

headers_converter_registry: ConverterRegistry = ConverterRegistry()
headers_converter_registry.register_types(
    _common_converters() + [CsvArrayConverter(), MultiArrayConverter()]
)

request_body_converter_registry: ConverterRegistry = ConverterRegistry()
request_body_converter_registry.register_types(
    _common_converters() + [DictConverter(), NestedConverter()]
)

response_converter_registry: ConverterRegistry = ConverterRegistry()
response_converter_registry.register_types(
    _common_converters() + [DictConverter(), NestedConverter()]
)
